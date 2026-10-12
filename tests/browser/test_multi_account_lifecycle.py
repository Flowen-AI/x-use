"""Multi-account isolation and explicit update barriers; no live browser or X."""
import asyncio

import pytest
import pytest_asyncio

from test_playwright_sessions import FakeContext, FakeRuntime, FakeXBrowser
from xuse.browser.errors import SessionError
from xuse.browser.sessions import PatchrightSessionPool, PlaywrightSessionPool


@pytest_asyncio.fixture(params=[PatchrightSessionPool, PlaywrightSessionPool], ids=["patchright", "playwright"])
async def multi_pool(request, make_config_loader, tmp_path, monkeypatch):
    import xuse.browser.page
    monkeypatch.setattr(xuse.browser.page, "XBrowser", FakeXBrowser)
    accounts = [{"account_id": key, "cookies": [
        {"name": "auth_token", "value": "synthetic-" + key, "secure": True, "domain": ".x.com"},
        {"name": "ct0", "value": "synthetic-csrf", "secure": True, "domain": ".x.com"},
    ]} for key in ("a", "b")]
    runtime = FakeRuntime()
    pool = request.param(make_config_loader(settings={"mcp": {}}, accounts=accounts),
                         playwright_factory=lambda: runtime, lock_directory=tmp_path / "locks")
    yield pool, runtime
    await pool.close_all()


@pytest.mark.asyncio
async def test_twenty_same_account_requests_share_one_start_and_owner(multi_pool):
    pool, runtime = multi_pool
    entries = await asyncio.gather(*(pool.acquire("a") for _ in range(20)))
    assert all(entry is entries[0] for entry in entries)
    assert len(runtime.browser.contexts) == 1 and runtime.launches == 1


@pytest.mark.asyncio
async def test_close_drains_account_startup_instead_of_publishing_stale_credentials(multi_pool, monkeypatch):
    pool, runtime = multi_pool
    entered, release = asyncio.Event(), asyncio.Event()

    async def waiting(self):
        entered.set()
        await release.wait()

    monkeypatch.setattr(FakeXBrowser, "ensure_ready", waiting)
    starter = asyncio.create_task(pool.acquire("a"))
    try:
        await entered.wait()
        pool.config_loader.accounts[0]["cookies"][0]["value"] = "replacement-synthetic"
        await pool.close("a")
        release.set()
        await asyncio.gather(starter, return_exceptions=True)
        assert pool.entry_for("a") is None
        assert runtime.browser.contexts[0].closed
    finally:
        release.set()
        await asyncio.gather(starter, return_exceptions=True)


@pytest.mark.asyncio
async def test_late_start_success_after_cancellation_is_disposed_before_close_returns(multi_pool, monkeypatch):
    pool, runtime = multi_pool
    entered, release = asyncio.Event(), asyncio.Event()

    async def late_ready(self):
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            await release.wait()

    monkeypatch.setattr(FakeXBrowser, "ensure_ready", late_ready)
    starter = asyncio.create_task(pool.acquire("a"))
    closer = None
    try:
        await entered.wait()
        closer = asyncio.create_task(pool.close("a"))
        await asyncio.sleep(0)
        assert not closer.done()
        release.set()
        await closer
        result = (await asyncio.gather(starter, return_exceptions=True))[0]
        assert isinstance(result, SessionError) and result.reason == "session_expired"
        assert pool.entry_for("a") is None and runtime.browser.contexts[0].closed
    finally:
        release.set()
        await asyncio.gather(starter, return_exceptions=True)
        if closer is not None:
            await asyncio.gather(closer, return_exceptions=True)


@pytest.mark.asyncio
async def test_queued_acquisition_cannot_silently_switch_to_updated_account(multi_pool):
    pool, runtime = multi_pool
    queued = None
    try:
        async with pool._create_lock:
            queued = asyncio.create_task(pool.acquire("b"))
            await asyncio.sleep(0)
            pool.config_loader.accounts[1]["cookies"][0]["value"] = "new-identity-synthetic"
            await pool.close("b")
        result = (await asyncio.gather(queued, return_exceptions=True))[0]
        assert isinstance(result, SessionError) and result.reason == "session_expired"
        assert runtime.browser.contexts == []
        fresh = await pool.acquire("b")
        assert fresh.context.cookies[0]["value"] == "new-identity-synthetic"
    finally:
        if queued is not None:
            await asyncio.gather(queued, return_exceptions=True)


@pytest.mark.asyncio
async def test_queued_action_aborts_on_explicit_close_without_replaying_on_new_context(multi_pool):
    pool, runtime = multi_pool
    entry = await pool.acquire("a")
    writes = []

    async def queued_action():
        async with pool.session("a"):
            writes.append("must not run")

    async with entry.lock:
        queued = asyncio.create_task(queued_action())
        await asyncio.sleep(0)
        closer = asyncio.create_task(pool.close("a"))
        await asyncio.sleep(0)
    await closer
    result = (await asyncio.gather(queued, return_exceptions=True))[0]
    assert isinstance(result, SessionError) and result.reason == "session_expired"
    assert writes == [] and len(runtime.browser.contexts) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("change,reason", [("disable", "inactive_account"), ("remove", "unknown_account")])
async def test_warm_account_checks_administrative_status_without_reloading_cookies(multi_pool, change, reason):
    pool, _ = multi_pool
    original = await pool.acquire("a")
    other = await pool.acquire("b")
    if change == "disable":
        pool.config_loader.accounts[0]["is_active"] = False
    else:
        pool.config_loader.accounts.pop(0)
    with pytest.raises(SessionError, match=reason):
        async with pool.session("a"):
            pytest.fail("An administratively unavailable account ran an action")
    assert not original.context.closed and pool.entry_for("b") is other
    async with pool.session("b") as manager:
        assert manager is other.browser_manager


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", ["cookies", "proxy", "inactive", "unknown", "alias"])
async def test_rejected_account_never_evicts_other_healthy_context(multi_pool, invalid):
    pool, runtime = multi_pool
    pool.max_sessions = 1
    original = await pool.acquire("a")
    target = "b"
    if invalid == "cookies":
        pool.config_loader.accounts[1]["cookies"] = []
    elif invalid == "proxy":
        pool.config_loader.accounts[1]["proxy"] = "http://proxy.invalid:0"
    elif invalid == "inactive":
        pool.config_loader.accounts[1]["is_active"] = False
    elif invalid == "unknown":
        target = "missing"
    else:
        pool.config_loader.accounts[1]["cookies"][0]["value"] = "synthetic-a"
    with pytest.raises(SessionError):
        await pool.acquire(target)
    assert pool.entry_for("a") is original and not original.context.closed
    assert len(runtime.browser.contexts) == 1


@pytest.mark.asyncio
async def test_closing_one_account_preserves_another_active_context_and_owner(multi_pool):
    pool, runtime = multi_pool
    first, second = await pool.acquire("a"), await pool.acquire("b")
    async with pool.session("b") as manager:
        await pool.close("a")
        assert manager is second.browser_manager and not second.context.closed
        assert second.owner_lock._file is not None
        assert pool.session_status("b")["state"] == "busy"
    assert first.owner_lock._file is None and first.context.closed
    assert not runtime.browser.closed


@pytest.mark.asyncio
async def test_close_during_caller_cancellation_waits_for_the_same_startup_disposal(multi_pool, monkeypatch):
    pool, runtime = multi_pool
    entered, closing, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original_close = FakeContext.close

    async def waiting(self):
        entered.set()
        await asyncio.Event().wait()

    async def slow_close(self):
        closing.set()
        await release.wait()
        await original_close(self)

    monkeypatch.setattr(FakeXBrowser, "ensure_ready", waiting)
    monkeypatch.setattr(FakeContext, "close", slow_close)
    starter = asyncio.create_task(pool.acquire("a"))
    closer = None
    try:
        await entered.wait()
        starter.cancel()
        await closing.wait()
        closer = asyncio.create_task(pool.close("a"))
        for _ in range(10):
            await asyncio.sleep(0)
        assert not closer.done() and not runtime.browser.contexts[0].closed
        release.set()
        await closer
        result = (await asyncio.gather(starter, return_exceptions=True))[0]
        assert isinstance(result, asyncio.CancelledError)
        assert runtime.browser.contexts[0].closed and pool.entry_for("a") is None
    finally:
        release.set()
        await asyncio.gather(starter, return_exceptions=True)
        if closer is not None:
            await asyncio.gather(closer, return_exceptions=True)
