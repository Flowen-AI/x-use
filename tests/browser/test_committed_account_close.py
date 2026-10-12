"""Committed configuration cleanup survives tool cancellation; no real browsers."""
import asyncio
import copy
from types import SimpleNamespace

import pytest
import pytest_asyncio

from test_playwright_sessions import FakeRuntime, FakeXBrowser
from xuse.browser.errors import SessionError as BrowserSessionError
from xuse.browser.sessions import PatchrightSessionPool, PlaywrightSessionPool
from xuse.mcp.sessions import SessionError as LegacySessionError, SessionPool


@pytest_asyncio.fixture(params=[PatchrightSessionPool, PlaywrightSessionPool, SessionPool],
                      ids=["patchright", "playwright", "selenium"])
async def committed_pool(request, make_config_loader, tmp_path, monkeypatch):
    import xuse.browser.page
    monkeypatch.setattr(xuse.browser.page, "XBrowser", FakeXBrowser)
    account = {"account_id": "a", "cookies": [
        {"name": "auth_token", "value": "synthetic-original", "secure": True, "domain": ".x.com"},
        {"name": "ct0", "value": "synthetic-csrf", "secure": True, "domain": ".x.com"},
    ]}
    loader = make_config_loader(settings={"mcp": {}}, accounts=[account])
    if request.param is SessionPool:
        def factory(config):
            manager = SimpleNamespace(account=copy.deepcopy(config), closed=False, get_driver=lambda: None)
            manager.close_driver = lambda: setattr(manager, "closed", True)
            return manager
        pool = SessionPool(loader, browser_factory=factory)
    else:
        runtime = FakeRuntime()
        pool = request.param(loader, playwright_factory=lambda: runtime, lock_directory=tmp_path / "locks")
    yield pool
    await pool.close_all()


def closed(entry):
    return entry.context.closed if hasattr(entry, "context") else entry.browser_manager.closed


async def begin_committed_close(pool):
    task = asyncio.create_task(pool.close_after_config_change("a"))
    await asyncio.sleep(0)
    assert "a" in pool._config_close_tasks
    return task, pool._config_close_tasks["a"]


@pytest.mark.asyncio
async def test_cancelled_committed_update_still_closes_previous_context(committed_pool):
    pool = committed_pool
    old = await pool.acquire("a")
    await old.lock.acquire()
    owns_action_lock = True
    caller = arrival = None
    try:
        pool.config_loader.accounts[0]["cookies"][0]["value"] = "synthetic-replacement"
        caller, retained = await begin_committed_close(pool)
        caller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await caller
        assert not retained.done() and not closed(old)
        arrival = asyncio.create_task(pool.acquire("a"))
        await asyncio.sleep(0)
        assert not arrival.done()
        old.lock.release()
        owns_action_lock = False
        await asyncio.wait_for(asyncio.shield(retained), 5)
        fresh = await asyncio.wait_for(arrival, 5)
        assert fresh is not old and closed(old)
        actual = fresh.context.cookies if hasattr(fresh, "context") else fresh.browser_manager.account["cookies"]
        assert actual[0]["value"] == "synthetic-replacement"
        assert not pool._config_close_tasks
    finally:
        if owns_action_lock:
            old.lock.release()
        await asyncio.gather(*(task for task in (caller, arrival) if task is not None), return_exceptions=True)


@pytest.mark.asyncio
async def test_committed_close_publishes_barrier_before_cleanup_task_runs(committed_pool):
    pool = committed_pool
    old = await pool.acquire("a")
    await old.lock.acquire()
    owns_action_lock = True
    caller = asyncio.create_task(pool.close_after_config_change("a"))
    # This acquisition is queued before the close's inner cleanup task exists.
    # Generation invalidation alone would let it return the old warm entry.
    arrival = asyncio.create_task(pool.acquire("a"))
    try:
        await asyncio.sleep(0)
        assert not arrival.done()
        old.lock.release()
        owns_action_lock = False
        await asyncio.wait_for(caller, 5)
        assert await asyncio.wait_for(arrival, 5) is not old
        assert closed(old)
    finally:
        if owns_action_lock:
            old.lock.release()
        await asyncio.gather(caller, arrival, return_exceptions=True)


@pytest.mark.asyncio
async def test_preupdate_queued_action_never_replays_after_committed_close(committed_pool):
    pool = committed_pool
    old = await pool.acquire("a")
    invoked = []
    async def action():
        async with pool.session("a"):
            invoked.append(True)
    async with old.lock:
        waiter = asyncio.create_task(action())
        await asyncio.sleep(0)
        caller, retained = await begin_committed_close(pool)
        caller.cancel()
        await asyncio.gather(caller, return_exceptions=True)
    result = (await asyncio.gather(waiter, return_exceptions=True))[0]
    assert isinstance(result, (BrowserSessionError, LegacySessionError))
    assert not invoked
    await asyncio.wait_for(asyncio.shield(retained), 5)
    assert closed(old)


@pytest.mark.asyncio
async def test_preupdate_queued_acquisition_cannot_adopt_new_credentials(committed_pool):
    pool = committed_pool
    if pool._create_lock is None:
        pool._create_lock = asyncio.Lock()
    async with pool._create_lock:
        waiter = asyncio.create_task(pool.acquire("a"))
        await asyncio.sleep(0)
        await pool.close_after_config_change("a")
    result = (await asyncio.gather(waiter, return_exceptions=True))[0]
    assert isinstance(result, (BrowserSessionError, LegacySessionError))
    assert pool.entry_for("a") is None


@pytest.mark.asyncio
async def test_shutdown_drains_retained_close_without_waiting_for_active_action(committed_pool):
    pool = committed_pool
    old = await pool.acquire("a")
    await old.lock.acquire()
    try:
        caller, retained = await begin_committed_close(pool)
        # Let the retained close park on the action before shutdown forces it.
        await asyncio.sleep(0)
        caller.cancel()
        await asyncio.gather(caller, return_exceptions=True)
        await asyncio.wait_for(pool.close_all(), 5)
        assert retained.done() and closed(old)
        assert not pool._config_close_tasks
    finally:
        old.lock.release()


@pytest.mark.asyncio
async def test_cancelled_update_disposes_late_cold_start_before_barrier_opens(committed_pool, monkeypatch):
    pool = committed_pool
    original = pool._cold_start
    entered, release = asyncio.Event(), asyncio.Event()
    entries = []
    async def delayed(account):
        entry = await original(account)
        entries.append(entry)
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            await release.wait()
        return entry
    monkeypatch.setattr(pool, "_cold_start", delayed)
    starter = asyncio.create_task(pool.acquire("a"))
    caller = None
    try:
        await asyncio.wait_for(entered.wait(), 5)
        caller, retained = await begin_committed_close(pool)
        caller.cancel()
        await asyncio.gather(caller, return_exceptions=True)
        assert not retained.done()
        release.set()
        await asyncio.wait_for(asyncio.shield(retained), 5)
        result = (await asyncio.gather(starter, return_exceptions=True))[0]
        assert isinstance(result, (BrowserSessionError, LegacySessionError, asyncio.CancelledError))
        assert pool.entry_for("a") is None and closed(entries[0])
    finally:
        release.set()
        await asyncio.gather(*(task for task in (starter, caller) if task is not None), return_exceptions=True)


@pytest.mark.asyncio
async def test_cancelled_shutdown_keeps_committed_startup_cleanup_owned(committed_pool, monkeypatch):
    pool = committed_pool
    original = pool._cold_start
    entered, release = asyncio.Event(), asyncio.Event()
    entries = []
    async def delayed(account):
        entry = await original(account)
        entries.append(entry)
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            await release.wait()
        return entry
    monkeypatch.setattr(pool, "_cold_start", delayed)
    starter = asyncio.create_task(pool.acquire("a"))
    caller = shutdown = None
    try:
        await asyncio.wait_for(entered.wait(), 5)
        # Remove the unrelated timer wait so shutdown's first suspension is
        # its owned cleanup wait (the only resource is the pending startup).
        reaper, pool._reaper_task = pool._reaper_task, None
        if reaper is not None:
            reaper.cancel()
            await asyncio.gather(reaper, return_exceptions=True)
        caller, retained = await begin_committed_close(pool)
        caller.cancel()
        await asyncio.gather(caller, return_exceptions=True)
        shutdown = asyncio.create_task(pool.close_all())
        await asyncio.sleep(0)
        shutdown.cancel()
        with pytest.raises(asyncio.CancelledError):
            await shutdown
        assert not retained.done(), "Cancelling shutdown discarded committed startup cleanup"
        release.set()
        await asyncio.wait_for(asyncio.shield(retained), 5)
        await asyncio.gather(starter, return_exceptions=True)
        assert pool.entry_for("a") is None and closed(entries[0])
        assert not pool._config_close_tasks
    finally:
        release.set()
        await asyncio.wait_for(asyncio.gather(
            *(task for task in (starter, caller, shutdown) if task is not None),
            return_exceptions=True), 5)
