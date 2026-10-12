"""Retained anonymous proxy probes remain owned through server shutdown."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from xuse.mcp.server import shutdown


@pytest.mark.asyncio
@pytest.mark.parametrize("session_failure", [False, True])
async def test_shutdown_drains_retained_probe_even_after_session_error(session_failure):
    sessions = SimpleNamespace(close_all=AsyncMock(side_effect=RuntimeError("synthetic") if session_failure else None))
    probe = SimpleNamespace(close_all=AsyncMock(), _process_guard=None, _runtime=None)
    ctx = SimpleNamespace(runs={}, session_pool=sessions, _proxy_probe_pool=probe)
    await shutdown(SimpleNamespace(xuse_ctx=ctx))
    sessions.close_all.assert_awaited_once()
    probe.close_all.assert_awaited_once()
    assert ctx._proxy_probe_pool is None


@pytest.mark.asyncio
async def test_shutdown_keeps_uncertain_probe_owner_and_does_not_log_private_errors(caplog):
    secret = "synthetic-proxy-password-must-not-appear"
    probe = SimpleNamespace(close_all=AsyncMock(side_effect=RuntimeError(secret)),
                            _process_guard=object(), _runtime=object())
    ctx = SimpleNamespace(runs={}, session_pool=SimpleNamespace(close_all=AsyncMock()), _proxy_probe_pool=probe)
    await shutdown(SimpleNamespace(xuse_ctx=ctx))
    probe.close_all.assert_awaited_once()
    assert ctx._proxy_probe_pool is probe
    assert secret not in caplog.text
