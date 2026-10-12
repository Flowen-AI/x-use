"""One stdio server owns a data home while its cached journals are in use."""
import hashlib
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def server_home_lock(root):
    """Fail closed before loading config/drafts/queue in a second server.

    SQLite budgets alone cannot protect a rejection/cancellation held in a
    different process's stale JSONL cache. The OS releases this lock on exit,
    including abnormal termination. No PID or credential is persisted.
    """
    from xuse.browser.sessions import AccountOwnerLock
    from xuse.browser.errors import SessionError

    key = hashlib.sha256(os.path.normcase(str(Path(root).resolve())).encode()).hexdigest()
    lock = AccountOwnerLock(key, Path(tempfile.gettempdir()) / "xuse-server-locks")
    try:
        lock.acquire()
    except SessionError as exc:
        if exc.reason == "account_in_use":
            raise RuntimeError(
                "Another x-use MCP server owns this X_USE_HOME. Close that server "
                "before reconnecting; concurrent servers cannot share cached draft/queue state."
            ) from None
        raise RuntimeError("Cannot acquire the x-use data-home lock. Check local permissions.") from None
    try:
        yield
    finally:
        lock.release()
