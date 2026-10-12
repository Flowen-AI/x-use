"""Queue outcomes retain evidence needed to avoid replay after interruption."""
import asyncio

import pytest

from xuse.queue import QueueConfig, QueueRunner, QueueStore
from xuse.queue.store import QueueJournalError


def make_queue(tmp_path, execute):
    store = QueueStore(tmp_path / "queue.jsonl")
    runner = QueueRunner(store, QueueConfig(min_delay_seconds=0, max_delay_seconds=0), execute)
    item = store.add(account="a", action="post", payload={"text": "reviewed"}, dedup_key="one")
    return store, runner, item


@pytest.mark.asyncio
@pytest.mark.parametrize("result", [None, {}, {"success": False}, {"success": 1}])
async def test_missing_confirmed_success_is_not_recorded_done(tmp_path, result):
    async def execute(item):
        return result
    store, runner, item = make_queue(tmp_path, execute)
    report = await runner.drain("a")
    assert report.failed == 1 and report.succeeded == 0
    assert store.get(item.queue_id).status == "failed"
    assert "confirmed success" in report.executed[0]["error"]
    assert not (await runner.drain("a")).executed


@pytest.mark.asyncio
async def test_failed_action_reference_survives_report_and_restart(tmp_path):
    async def execute(item):
        error = RuntimeError("unconfirmed send")
        error.action_id, error.reason = "ledger-action", "send_unconfirmed"
        raise error
    store, runner, item = make_queue(tmp_path, execute)
    report = await runner.drain("a")
    outcome = report.executed[0]
    assert outcome["action_id"] == "ledger-action"
    assert outcome["reason"] == "send_unconfirmed"
    assert outcome["status"] == "failed"
    restored = QueueStore(tmp_path / "queue.jsonl")
    assert restored.get(item.queue_id).action_id == "ledger-action"


@pytest.mark.asyncio
async def test_success_reference_is_persisted(tmp_path):
    async def execute(item):
        return {"success": True, "action_id": "confirmed-action"}
    store, runner, item = make_queue(tmp_path, execute)
    assert (await runner.drain("a")).succeeded == 1
    assert QueueStore(tmp_path / "queue.jsonl").get(item.queue_id).action_id == "confirmed-action"


@pytest.mark.asyncio
async def test_cancelled_action_reference_survives_restart(tmp_path):
    async def execute(item):
        error = asyncio.CancelledError()
        error.action_id = "interrupted-action"
        raise error
    store, runner, item = make_queue(tmp_path, execute)
    with pytest.raises(asyncio.CancelledError):
        await runner.drain("a")
    restored = QueueStore(tmp_path / "queue.jsonl").get(item.queue_id)
    assert restored.status == "failed" and restored.action_id == "interrupted-action"


@pytest.mark.asyncio
@pytest.mark.parametrize("cancelled", [False, True])
async def test_outcome_journal_failure_preserves_recovery_metadata(tmp_path, monkeypatch, cancelled):
    def fail_append(item):
        raise OSError("journal unavailable")
    async def execute(item):
        monkeypatch.setattr(store, "_append", fail_append)
        error = asyncio.CancelledError() if cancelled else RuntimeError("send unconfirmed")
        error.action_id = "interrupted-action"
        raise error
    store, runner, item = make_queue(tmp_path, execute)
    with pytest.raises(asyncio.CancelledError if cancelled else QueueJournalError) as caught:
        await runner.drain("a")
    assert caught.value.action_id == "interrupted-action"
    assert store.get(item.queue_id).status == "processing"
    assert not QueueStore(tmp_path / "queue.jsonl").list(status="pending")


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["pause", "dedup"])
async def test_pacing_window_rechecks_account_and_dedup(tmp_path, change):
    calls, processed = [], set()
    active = True
    async def execute(item):
        calls.append(item.queue_id)
        return {"success": True}
    store, runner, first = make_queue(tmp_path, execute)
    second = store.add(account="a", action="post", payload={"text": "two"}, dedup_key="two")
    runner.account_filter = lambda account: active
    runner.already_done = lambda key: key in processed
    async def sleep(seconds):
        nonlocal active
        if change == "pause":
            active = False
        else:
            processed.add("two")
    runner.sleep_fn = sleep
    report = await runner.drain("a")
    assert calls == [first.queue_id]
    assert store.get(second.queue_id).status == ("pending" if change == "pause" else "done")
    assert report.skipped_inactive == (1 if change == "pause" else 0)
