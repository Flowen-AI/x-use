"""Isolated lifecycle regressions: fake credentials, no network or X writes."""
import asyncio
import errno
import json
import math
import os
import time
from types import SimpleNamespace

import pytest

from xuse.browser.errors import BrowserBlocked, SessionError
from xuse.browser.sessions import AccountOwnerLock, PatchrightSessionPool, PlaywrightSessionPool, _OwnedBrowserProcesses


class Context:
    def __init__(self):
        self.closed = False
        self.page = SimpleNamespace(is_closed=lambda: self.closed)

    def set_default_timeout(self, value):
        pass

    def set_default_navigation_timeout(self, value):
        pass

    async def new_page(self):
        return self.page

    async def add_cookies(self, cookies):
        pass

    async def close(self):
        self.closed = True


class Runtime:
    def __init__(self):
        self.chromium = self
        self.browser = self
        self.closed = self.stopped = False
        self.contexts = []

    async def launch(self, **options):
        return self

    async def new_context(self, **options):
        context = Context()
        self.contexts.append(context)
        return context

    def is_connected(self):
        return not self.closed

    async def close(self):
        self.closed = True

    async def stop(self):
        self.stopped = True


class Manager:
    def __init__(self, page, account):
        self.page = page

    async def navigate(self, url):
        assert url == "https://x.com/home"

    async def ensure_ready(self):
        return {"success": True}


@pytest.fixture(params=[PatchrightSessionPool, PlaywrightSessionPool], ids=["patchright", "playwright"])
def lifecycle_pool(request, make_config_loader, tmp_path, monkeypatch):
    import xuse.browser.page
    monkeypatch.setattr(xuse.browser.page, "XBrowser", Manager)
    accounts = [{"account_id": key, "cookies": [
        {"name": "auth_token", "value": "synthetic-" + key, "domain": ".x.com", "secure": True},
        {"name": "ct0", "value": "synthetic-csrf", "domain": ".x.com", "secure": True},
    ]} for key in ("a", "b")]
    runtimes = []

    def factory():
        runtime = Runtime()
        runtimes.append(runtime)
        return runtime

    pool = request.param(make_config_loader(settings={"mcp": {}}, accounts=accounts),
                         playwright_factory=factory, lock_directory=tmp_path / "owners",
                         idle_timeout_seconds=0.02, reap_interval_seconds=0.005)
    yield pool, runtimes
    assert pool._closed


async def eventually(predicate):
    # Short asyncio timers can be serviced early on a coarse Windows clock.
    # Bound elapsed time, not a count that assumes every 5ms sleep really waits.
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.005)
    pytest.fail("Lifecycle operation did not finish within the bounded fixture wait")


@pytest.mark.asyncio
@pytest.mark.parametrize('coarse_clock', [False, True], ids=['native-clock', 'coarse-windows-clock'])
async def test_last_idle_context_reclaims_runtime_and_next_explicit_use_is_fresh(lifecycle_pool, monkeypatch, coarse_clock):
    pool, runtimes = lifecycle_pool
    if coarse_clock:
        # Reproduce the 15.625ms event-loop clock used by older Windows Python
        # versions, including their look-ahead when servicing short timers.
        loop = asyncio.get_running_loop()
        monkeypatch.setattr(loop, '_clock_resolution', 1 / 64)
        monkeypatch.setattr(loop, 'time', lambda: math.floor(time.monotonic() * 64) / 64)
    try:
        original = await pool.acquire("a")
        await eventually(lambda: pool._runtime is None)
        assert original.context.closed and original.owner_lock._file is None
        assert runtimes[0].closed and runtimes[0].stopped and not pool._closed
        assert pool.session_status("a")["state"] == "cold"
        fresh = await pool.acquire("a")
        assert fresh is not original and len(runtimes) == 2
        assert not fresh.context.closed
    finally:
        await pool.close_all()


@pytest.mark.asyncio
async def test_failed_authentication_still_reaps_launched_runtime(lifecycle_pool, monkeypatch):
    pool, runtimes = lifecycle_pool

    async def denied(self):
        raise BrowserBlocked("login_required")

    monkeypatch.setattr(Manager, "ensure_ready", denied)
    try:
        with pytest.raises(BrowserBlocked, match="login_required"):
            await pool.acquire("a")
        assert not tuple(pool.active_accounts)
        await eventually(lambda: pool._runtime is None)
        assert runtimes[0].closed and runtimes[0].stopped
        assert not pool._closed
    finally:
        await pool.close_all()


@pytest.mark.asyncio
async def test_reaper_never_stops_runtime_while_action_holds_account(lifecycle_pool):
    pool, runtimes = lifecycle_pool
    try:
        async with pool.session("a"):
            await asyncio.sleep(0.06)
            assert len(runtimes) == 1 and not runtimes[0].closed
            assert pool.session_status("a")["state"] == "busy"
        assert pool.session_status("a")["state"] == "ready"
        await eventually(lambda: pool._runtime is None)
        assert runtimes[0].stopped
    finally:
        await pool.close_all()


@pytest.mark.asyncio
async def test_idle_teardown_serializes_new_start_even_after_reaper_cancellation(lifecycle_pool):
    pool, runtimes = lifecycle_pool
    entered, release = asyncio.Event(), asyncio.Event()
    waiter = None
    try:
        await pool.acquire("a")
        runtime = runtimes[0]
        original_close = runtime.close

        async def slow_close():
            entered.set()
            await release.wait()
            await original_close()

        runtime.close = slow_close
        await asyncio.wait_for(entered.wait(), 1)
        pool._reaper_task.cancel()
        await pool._reaper_task
        waiter = asyncio.create_task(pool.acquire("b"))
        await asyncio.sleep(0)
        assert not waiter.done() and pool._create_lock.locked() and len(runtimes) == 1
        release.set()
        fresh = await asyncio.wait_for(waiter, 1)
        assert runtime.stopped and len(runtimes) == 2
        assert not fresh.context.closed
    finally:
        release.set()
        if waiter is not None:
            await asyncio.gather(waiter, return_exceptions=True)
        await pool.close_all()


@pytest.mark.asyncio
async def test_unconfirmed_idle_runtime_cleanup_cannot_overwrite_owned_handles(lifecycle_pool):
    pool, runtimes = lifecycle_pool
    try:
        await pool.acquire("a")
        owner = pool.entry_for("a").owner_lock
        await pool.close("a")
        # A stranded owner represents a previous failed close. Idle cleanup
        # must apply the same process-proof rule as explicit shutdown.
        owner.acquire()
        pool._stranded_owners.add(owner)

        class Guard:
            complete = True

            def refresh(self):
                pass

            def exited(self):
                return False

            def terminate(self):
                raise OSError("synthetic unconfirmed process")

        guard = pool._process_guard = Guard()
        pool._runtime_last_used -= 10
        await pool._retire_idle_runtime()
        assert pool._closed and pool._process_guard is guard
        assert owner._file is not None and pool._runtime is runtimes[0]
        with pytest.raises(SessionError, match="pool_closed"):
            await pool.acquire("b")
        assert len(runtimes) == 1
    finally:
        pool._process_guard = None
        await pool.close_all()
    assert owner._file is None


@pytest.mark.asyncio
async def test_runtime_retirement_rechecks_new_entries_inside_creation_lock(lifecycle_pool):
    pool, runtimes = lifecycle_pool
    pool.idle_timeout_seconds = 100
    task = None
    try:
        first = await pool.acquire("a")
        await pool.close("a")
        pool._runtime_last_used -= 200
        async with pool._create_lock:
            task = asyncio.create_task(pool._retire_idle_runtime())
            await asyncio.sleep(0)
            # Simulate a startup that completed before the retiring task won
            # the lock; its stale idle decision must not close the new entry.
            fresh = await pool._cold_start("b")
            pool._entries["b"] = fresh
        await task
        assert first.context.closed and not fresh.context.closed
        assert not runtimes[0].closed and not runtimes[0].stopped
    finally:
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)
        await pool.close_all()


@pytest.mark.asyncio
async def test_shutdown_drains_queued_idle_task_before_taking_its_creation_lock(lifecycle_pool):
    pool, _ = lifecycle_pool
    pool.idle_timeout_seconds = 100
    release = asyncio.Event()
    closer = None
    try:
        await pool.acquire("a")
        await pool.close("a")
        pool._runtime_last_used -= 200

        async def delayed_idle_cleanup():
            await release.wait()
            async with pool._create_lock:
                await pool._close_runtime()

        # Delay the queued cleanup before it acquires the creation lock. A
        # shutdown that gathers it while holding that lock deadlocks here.
        pool._retire_task = asyncio.create_task(delayed_idle_cleanup())
        closer = asyncio.create_task(pool.close_all())
        await asyncio.sleep(0)
        release.set()
        await asyncio.wait_for(closer, 1)
        assert pool._runtime is None and pool._retire_task.done()
    finally:
        release.set()
        if closer is not None:
            await asyncio.gather(closer, return_exceptions=True)
        await pool.close_all()


@pytest.mark.asyncio
async def test_lifecycle_status_is_local_private_and_does_not_keep_session_alive(lifecycle_pool):
    pool, _ = lifecycle_pool
    pool.idle_timeout_seconds = 100
    try:
        cold = pool.session_status("a")
        assert cold == {"state": "cold", "warm": False, "browser_connected": None,
                        "tab_closed": None, "idle_seconds": None, "live_authentication_checked": False}
        entry = await pool.acquire("a")
        entry.last_used -= 7
        last_used = entry.last_used
        status = pool.session_status("a")
        assert status["state"] == "ready" and status["warm"]
        assert status["browser_connected"] and status["tab_closed"] is False
        assert status["idle_seconds"] >= 7 and not status["live_authentication_checked"]
        assert entry.last_used == last_used
        payload = json.dumps(status)
        assert not any(value in payload for value in ("synthetic", "x.com", "auth_token", "owners"))
        async with entry.lock:
            assert pool.session_status("a")["state"] == "busy"
        entry.closing = True
        assert pool.session_status("a")["state"] == "closing"
        entry.closing = False
        entry.context.closed = True
        assert pool.session_status("a")["state"] == "disconnected"
    finally:
        await pool.close_all()
    assert pool.session_status("a")["state"] == "closed"


@pytest.mark.asyncio
async def test_status_reports_starting_before_account_page_is_ready(lifecycle_pool, monkeypatch):
    pool, _ = lifecycle_pool
    entered, release = asyncio.Event(), asyncio.Event()

    async def slow_ready(self):
        entered.set()
        await release.wait()

    monkeypatch.setattr(Manager, "ensure_ready", slow_ready)
    task = asyncio.create_task(pool.acquire("a"))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        status = pool.session_status("a")
        assert status["state"] == "starting" and not status["warm"]
        assert pool.session_status("b")["state"] == "cold"
        release.set()
        await task
        assert pool.session_status("a")["state"] == "ready"
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        await pool.close_all()


@pytest.mark.asyncio
@pytest.mark.parametrize("probe_failure", [False, True])
async def test_disconnected_or_failed_transport_probe_never_reports_ready(lifecycle_pool, probe_failure):
    pool, runtimes = lifecycle_pool
    try:
        await pool.acquire("a")

        def probe():
            if probe_failure:
                raise RuntimeError("synthetic sensitive transport error")
            return False

        runtimes[0].is_connected = probe
        status = pool.session_status("a")
        assert status["state"] == "disconnected" and status["warm"]
        assert "sensitive" not in json.dumps(status)
        assert len(runtimes) == 1
    finally:
        await pool.close_all()


@pytest.mark.asyncio
async def test_failed_capture_does_not_cache_a_stopped_runtime(lifecycle_pool):
    pool, runtimes = lifecycle_pool
    original = pool._capture_owned_processes

    async def fail_capture(browser):
        raise SessionError("browser_unavailable")

    pool._capture_owned_processes = fail_capture
    try:
        with pytest.raises(SessionError, match="browser_unavailable"):
            await pool.acquire("a")
        assert runtimes[0].stopped and pool._runtime is None and pool._browser is None
        pool._capture_owned_processes = original
        await pool.acquire("a")
        assert len(runtimes) == 2
    finally:
        await pool.close_all()


@pytest.mark.parametrize("error_number,reason", [(errno.EACCES, "account_in_use"),
                                                (errno.EAGAIN, "account_in_use"),
                                                (errno.EIO, "session_unavailable"),
                                                (errno.ENOSPC, "session_unavailable")])
def test_owner_lock_distinguishes_contention_from_storage_errors(tmp_path, monkeypatch, error_number, reason):
    def fail(*args):
        raise OSError(error_number, "synthetic private path")

    if os.name == "nt":
        import msvcrt
        monkeypatch.setattr(msvcrt, "locking", fail)
    else:
        import fcntl
        monkeypatch.setattr(fcntl, "flock", fail)
    owner = AccountOwnerLock("synthetic-key", tmp_path)
    with pytest.raises(SessionError) as caught:
        owner.acquire()
    assert caught.value.reason == reason and owner._file is None
    assert "private" not in str(caught.value)
    # Windows cannot remove an open file, so this also catches leaked streams.
    owner.path.unlink()


def test_initial_lock_file_io_error_is_not_account_contention(tmp_path, monkeypatch):
    owner = AccountOwnerLock("synthetic-key", tmp_path)
    stream = SimpleNamespace(seek=lambda *args: (_ for _ in ()).throw(OSError(errno.EACCES, "synthetic")),
                             close=lambda: setattr(stream, "closed", True), closed=False)
    monkeypatch.setattr(type(owner.path), "open", lambda *args, **kwargs: stream)
    with pytest.raises(SessionError, match="session_unavailable"):
        owner.acquire()
    assert stream.closed and owner._file is None


def test_empty_windows_job_is_not_exit_proof_until_pinned_handles_signal():
    live = {10, 11}
    accounting = {"empty": True}
    inspector = SimpleNamespace(job_exited=lambda job: accounting["empty"])
    driver = SimpleNamespace(pid=10, alive=lambda: 10 in live)
    renderer = SimpleNamespace(pid=11, alive=lambda: 11 in live)
    guard = _OwnedBrowserProcesses(inspector, driver)
    guard.processes[11] = renderer
    guard.job = object()
    assert not guard.exited()
    live.remove(10)
    assert not guard.exited()
    live.clear()
    assert guard.exited()
    accounting["empty"] = False
    assert not guard.exited()  # A later child still accounted for by the Job.
    accounting["empty"] = True
    guard.complete = False
    assert not guard.exited()  # Unpinned pending identities are not exit proof.


@pytest.mark.asyncio
@pytest.mark.parametrize("exit_after,expected", [(4.0, True), (9.0, True), (100.0, False)])
async def test_process_drain_allows_slow_exit_but_keeps_a_finite_deadline(lifecycle_pool, monkeypatch, exit_after, expected):
    import xuse.browser.sessions as sessions
    pool, _ = lifecycle_pool
    elapsed = [0.0]
    calls = []

    class Guard:
        complete = True

        def refresh(self):
            calls.append("refresh")

        def exited(self):
            return elapsed[0] >= exit_after

        def terminate(self):
            calls.append("terminate")

    async def advance(delay):
        elapsed[0] += 1.0

    # Replace only this module's clocks, not asyncio's event-loop clock or
    # scheduling. A 100s non-exit is checked without actually waiting 100s.
    monkeypatch.setattr(sessions, "time", SimpleNamespace(monotonic=lambda: elapsed[0]))
    monkeypatch.setattr(sessions, "asyncio", SimpleNamespace(
        to_thread=asyncio.to_thread, sleep=advance))
    pool._process_guard = guard = Guard()
    try:
        assert await pool._drain_owned_processes() is expected
        assert calls == ["refresh", "terminate"]
        assert elapsed[0] <= 10.0
        if not expected:
            assert elapsed[0] == 10.0 and pool._process_guard is guard
    finally:
        monkeypatch.undo()
        pool._process_guard = None
        await pool.close_all()


@pytest.mark.asyncio
async def test_process_drain_refreshes_pending_identity_after_its_natural_exit(lifecycle_pool, monkeypatch):
    import xuse.browser.sessions as sessions
    pool, _ = lifecycle_pool
    elapsed = [0.0]

    class Guard:
        complete = False
        pending = {11: (10, 0)}

        def refresh(self):
            if elapsed[0] >= 2.0:
                self.pending.clear()
                self.complete = True

        def exited(self):
            return self.complete and not self.pending

        def terminate(self):
            pass

    async def advance(delay):
        elapsed[0] += 1.0

    monkeypatch.setattr(sessions, "time", SimpleNamespace(monotonic=lambda: elapsed[0]))
    monkeypatch.setattr(sessions, "asyncio", SimpleNamespace(to_thread=asyncio.to_thread, sleep=advance))
    pool._process_guard = guard = Guard()
    try:
        assert await pool._drain_owned_processes()
        assert guard.complete and not guard.pending
        assert elapsed[0] < 10.0
    finally:
        monkeypatch.undo()
        pool._process_guard = None
        await pool.close_all()
