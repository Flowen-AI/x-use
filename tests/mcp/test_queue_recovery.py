"""Recover an interrupted queue action through the existing MCP tools."""
import asyncio
import sqlite3
import subprocess
import sys

import pytest

from helpers import call_tool
from test_playwright_integration import async_server  # noqa: F401
from xuse.browser.errors import BrowserActionError
from xuse.queue import QueueStore
from xuse.mcp.safety import SafetyStore


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["succeeded", "not_sent"])
@pytest.mark.parametrize("cancelled", [False, True])
async def test_queue_reconciliation_survives_restart_without_replay(async_server, outcome, cancelled):
    ctx = async_server.xuse_ctx
    calls = []
    async def post(text, media=None, community=None):
        calls.append(text)
        raise BrowserActionError("send_unconfirmed")
    ctx.session_pool.browser.post = post
    queued = await call_tool(async_server, "queue_post", {"account": "acc1", "text": "reviewed text"})
    report = await call_tool(async_server, "process_queue", {"account": "acc1"})
    assert report["failed"] == 1
    action_id = report["executed"][0]["action_id"]
    assert report["executed"][0]["reason"] == "send_unconfirmed"
    if cancelled:
        await call_tool(async_server, "cancel_queued_action", {"queue_id": queued["queue_id"]})
    # Reload only the queue journal; policy reservations persist independently.
    ctx.queue_store = QueueStore(ctx.queue_store._path)
    ctx.queue_runner.store = ctx.queue_store
    recovery = await call_tool(async_server, "resolve_action_outcome", {
        "account": "acc1", "action_id": action_id, "observed_outcome": outcome})
    assert recovery["ok"]
    assert recovery["queue_ids"] == [queued["queue_id"]]
    item = ctx.queue_store.get(queued["queue_id"])
    assert item.status == ("done" if outcome == "succeeded" else "cancelled")
    assert item.action_id == action_id
    if outcome == "succeeded":
        assert item.completed_at
    assert not (await call_tool(async_server, "process_queue", {"account": "acc1"}))["executed"]
    assert calls == ["reviewed text"]


@pytest.mark.asyncio
async def test_reconciliation_never_updates_another_accounts_queue(async_server):
    ctx = async_server.xuse_ctx
    other = ctx.queue_store.add(account="other", action="post", payload={"text": "private"}, dedup_key="other")
    action_id = ctx.safety_store.reserve("acc1", "post", "synthetic", {"queue_id": other.queue_id})
    ctx.safety_store.finish("acc1", action_id, "uncertain")
    ctx.queue_store.set_status(other.queue_id, "failed", action_id=action_id)
    result = await call_tool(async_server, "resolve_action_outcome", {
        "account": "acc1", "action_id": action_id, "observed_outcome": "succeeded"})
    assert result["ok"] and not result.get("queue_ids")
    assert other.status == "failed"


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["post", "reply", "like", "retweet"])
async def test_each_queue_write_reserves_identity_before_browser_execution(async_server, action):
    ctx = async_server.xuse_ctx
    async def write(*args, **kwargs):
        # This runs at the browser seam: the queue reference must already be
        # committed and visible to a newly opened policy store.
        policy = SafetyStore(ctx.safety_store.path)
        recent = policy.status("acc1")["recent_actions"][0]
        assert recent["queue_id"] == queued["queue_id"]
        assert policy.reference("acc1", recent["action_id"])["queue_id"] == queued["queue_id"]
        return {"success": True}
    setattr(ctx.session_pool.browser, action, write)
    if action == "post":
        queued = await call_tool(async_server, "queue_post", {"account": "acc1", "text": "reviewed"})
    else:
        queued = await call_tool(async_server, "queue_engagement", {
            "account": "acc1", "action": action, "tweet_url": "https://x.com/user/status/123", "text": "reviewed"})
    assert (await call_tool(async_server, "process_queue", {"account": "acc1"}))["succeeded"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["succeeded", "not_sent"])
async def test_ledger_reference_repairs_queue_after_journal_write_failure(async_server, monkeypatch, outcome):
    ctx = async_server.xuse_ctx
    append = ctx.queue_store._append
    def fail_append(item):
        raise OSError("synthetic queue journal failure")
    async def write(*args, **kwargs):
        monkeypatch.setattr(ctx.queue_store, "_append", fail_append)
        raise BrowserActionError("send_unconfirmed")
    ctx.session_pool.browser.post = write
    queued = await call_tool(async_server, "queue_post", {"account": "acc1", "text": "reviewed"})
    report = await call_tool(async_server, "process_queue", {"account": "acc1"})
    assert not report["ok"] and report["error"]["reason"] == "queue_journal_failure"
    action_id = report["error"]["action_id"]
    assert ctx.queue_store.get(queued["queue_id"]).action_id is None
    monkeypatch.setattr(ctx.queue_store, "_append", append)
    ctx.queue_store = QueueStore(ctx.queue_store._path)
    ctx.queue_runner.store = ctx.queue_store
    assert ctx.queue_store.get(queued["queue_id"]).status == "failed"
    result = await call_tool(async_server, "resolve_action_outcome", {
        "account": "acc1", "action_id": action_id, "observed_outcome": outcome})
    assert result["ok"] and result["queue_ids"] == [queued["queue_id"]]
    item = ctx.queue_store.get(queued["queue_id"])
    assert item.action_id == action_id
    assert item.status == ("done" if outcome == "succeeded" else "cancelled")


@pytest.mark.asyncio
async def test_old_reconciliation_cannot_replace_newer_queue_outcome(async_server):
    ctx = async_server.xuse_ctx
    item = ctx.queue_store.add(account="acc1", action="post", payload={}, dedup_key="test")
    older = ctx.safety_store.reserve("acc1", "post", "older", {"queue_id": item.queue_id})
    ctx.safety_store.finish("acc1", older, "failed")
    newer = ctx.safety_store.reserve("acc1", "post", "newer", {"queue_id": item.queue_id})
    ctx.safety_store.finish("acc1", newer, "uncertain")
    ctx.queue_store.set_status(item.queue_id, "failed", action_id=newer)
    result = await call_tool(async_server, "resolve_action_outcome", {
        "account": "acc1", "action_id": older, "observed_outcome": "not_sent"})
    assert result["ok"] and result["queue_update_skipped"]
    assert item.status == "failed" and item.action_id == newer


def test_old_policy_schema_migrates_without_losing_draft_references(tmp_path):
    path = tmp_path / "policy.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE action_refs(action_id TEXT PRIMARY KEY, account TEXT NOT NULL, draft_id TEXT, campaign_id TEXT, lead_id TEXT)")
        db.execute("INSERT INTO action_refs VALUES('old-action','a','old-draft','campaign','lead')")
    policy = SafetyStore(path)
    action_id = policy.reserve("a", "post", "new", {"queue_id": "new-queue"})
    assert policy.reference("a", action_id)["queue_id"] == "new-queue"
    with sqlite3.connect(path) as db:
        row = db.execute("SELECT draft_id,campaign_id,lead_id,queue_id FROM action_refs WHERE action_id='old-action'").fetchone()
    assert row == ("old-draft", "campaign", "lead", None)
    assert SafetyStore(path).latest_queue_action("a", "new-queue")["action_id"] == action_id
    assert SafetyStore(path).latest_queue_action("other", "new-queue") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["succeeded", "not_sent"])
async def test_process_exit_during_executor_retains_queue_reservation(async_server, outcome):
    ctx = async_server.xuse_ctx
    item = ctx.queue_store.add(account="acc1", action="post", payload={"text": "synthetic"}, dedup_key="interrupted")
    script = """
import asyncio, os, sys
from xuse.queue import QueueStore, QueueRunner
from xuse.mcp.safety import SafetyStore
queue = QueueStore(sys.argv[1])
policy = SafetyStore(sys.argv[2])
async def execute(item):
    policy.reserve(item.account, item.action, item.dedup_key, {"queue_id": item.queue_id})
    os._exit(77)  # A process exit prevents all executor/finally bookkeeping.
asyncio.run(QueueRunner(queue, executor=execute).drain("acc1"))
"""
    completed = await asyncio.to_thread(subprocess.run,
        [sys.executable, "-c", script, str(ctx.queue_store._path), str(ctx.safety_store.path)],
        capture_output=True, text=True, timeout=30)
    assert completed.returncode == 77, completed.stderr
    ctx.queue_store = QueueStore(ctx.queue_store._path)
    ctx.queue_runner.store = ctx.queue_store
    ctx.safety_store = SafetyStore(ctx.safety_store.path)
    interrupted = ctx.queue_store.get(item.queue_id)
    assert interrupted.status == "failed" and interrupted.action_id is None
    action_id = ctx.safety_store.latest_queue_action("acc1", item.queue_id)["action_id"]
    assert ctx.safety_store.reference("acc1", action_id)["status"] == "started"
    assert not (await call_tool(async_server, "process_queue", {"account": "acc1"}))["executed"]
    result = await call_tool(async_server, "resolve_action_outcome", {
        "account": "acc1", "action_id": action_id, "observed_outcome": outcome})
    assert result["ok"] and result["queue_ids"] == [item.queue_id]
    assert interrupted.status == ("done" if outcome == "succeeded" else "cancelled")
    assert interrupted.action_id == action_id
    assert ctx.session_pool.started == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["succeeded", "not_sent"])
async def test_reconciliation_journal_failure_keeps_id_and_allows_repair(async_server, monkeypatch, outcome):
    ctx = async_server.xuse_ctx
    item = ctx.queue_store.add(account="acc1", action="post", payload={}, dedup_key="repair")
    action_id = ctx.safety_store.reserve("acc1", "post", "repair", {"queue_id": item.queue_id})
    ctx.safety_store.finish("acc1", action_id, "uncertain")
    ctx.queue_store.set_status(item.queue_id, "failed")
    append = ctx.queue_store._append
    def fail_append(item):
        raise OSError("synthetic journal failure")
    monkeypatch.setattr(ctx.queue_store, "_append", fail_append)
    arguments = {"account": "acc1", "action_id": action_id, "observed_outcome": outcome}
    failed = await call_tool(async_server, "resolve_action_outcome", arguments)
    assert not failed["ok"]
    assert failed["error"]["action_id"] == action_id
    assert failed["error"]["queue_id"] == item.queue_id
    assert item.status == "failed"
    monkeypatch.setattr(ctx.queue_store, "_append", append)
    assert (await call_tool(async_server, "resolve_action_outcome", arguments))["ok"]
    assert item.status == ("done" if outcome == "succeeded" else "cancelled")
