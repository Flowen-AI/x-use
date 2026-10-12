"""Local diagnostics use the same credential rules as the browser runtime."""
import json

import pytest

from xuse.browser.sessions import PatchrightSessionPool
from helpers import (accounts, browser_factory, call_tool, config_loader, draft_store,
                     drafts_path, mcp_server, mcp_settings, queue_store, session_pool)


def cookies():
    return [{"name": "auth_token", "value": "synthetic-auth", "secure": True, "domain": ".x.com"},
            {"name": "ct0", "value": "synthetic-csrf", "secure": True, "domain": ".x.com"}]


def use_async_pool(server, monkeypatch):
    def forbidden_browser():
        pytest.fail("local diagnostics started a browser")
    pool = PatchrightSessionPool(server.xuse_ctx.config_loader, playwright_factory=forbidden_browser)
    monkeypatch.setattr(server.xuse_ctx, "session_pool", pool)
    return pool


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [{"domain": ".evil.test"}, {"secure": False}, {"expires": "future"}])
async def test_async_health_rejects_same_credentials_as_runtime(mcp_server, config_loader, monkeypatch, change):
    payload = cookies()
    payload[0].update(change)
    config_loader.accounts[0]["cookies"] = payload
    use_async_pool(mcp_server, monkeypatch)
    result = await call_tool(mcp_server, "get_account_health", {"account": "acc1"})
    assert result["ok"] and result["cookies"]["configured"]
    assert result["cookies"]["valid"] is False
    assert result["cookies"]["problems"] == ["invalid_cookies"]
    assert "synthetic-auth" not in json.dumps(result)
    assert result["cookies"]["live_authentication_checked"] is False


@pytest.mark.asyncio
async def test_async_health_uses_account_relative_cookie_path(mcp_server, config_loader, tmp_path, monkeypatch):
    (tmp_path / "private-session.json").write_text(json.dumps(cookies()), encoding="utf-8")
    config_loader.accounts[0]["cookie_file_path"] = "private-session.json"
    use_async_pool(mcp_server, monkeypatch)
    result = await call_tool(mcp_server, "get_account_health", {"account": "acc1"})
    assert result["cookies"]["valid"] is True
    assert result["cookies"]["validation"] == "async_cookie_import"
    assert str(tmp_path) not in json.dumps(result)
    assert "file" not in result["cookies"]


@pytest.mark.asyncio
async def test_empty_inline_source_cannot_be_hidden_by_valid_file(mcp_server, config_loader, tmp_path, monkeypatch):
    path = tmp_path / "private-session.json"
    path.write_text(json.dumps(cookies()), encoding="utf-8")
    config_loader.accounts[0].update(cookies=[], cookie_file_path=str(path))
    use_async_pool(mcp_server, monkeypatch)
    result = await call_tool(mcp_server, "get_account_health", {"account": "acc1"})
    assert result["cookies"]["valid"] is False
    assert result["cookies"]["source"] == "inline"


@pytest.mark.asyncio
async def test_session_and_health_status_are_local_and_agree(mcp_server, monkeypatch):
    pool = use_async_pool(mcp_server, monkeypatch)
    result = await call_tool(mcp_server, "get_session_status", {"account": "acc1"})
    health = await call_tool(mcp_server, "get_account_health", {"account": "acc1"})
    assert result["ok"] and health["ok"]
    assert result["live_authentication_checked"] is False
    assert result["sessions"][0] == {"account": "acc1", **health["session"]}
    assert health["session"]["state"] == "cold"
    assert health["session"]["warm"] is False
    assert pool._browser is None


@pytest.mark.asyncio
async def test_legacy_status_does_not_claim_live_readiness(mcp_server, browser_factory):
    result = await call_tool(mcp_server, "get_session_status", {"account": "acc1"})
    assert result["ok"] and result["backend"] == "selenium"
    assert result["sessions"][0]["state"] == "cold"
    assert result["sessions"][0]["live_authentication_checked"] is False
    assert browser_factory.created == []


@pytest.mark.asyncio
async def test_invalid_inline_config_cannot_echo_credentials(mcp_server, config_loader, monkeypatch):
    config_loader.accounts[0]["cookies"] = {"auth_token": "private-cookie-marker"}
    use_async_pool(mcp_server, monkeypatch)
    result = await call_tool(mcp_server, "get_account_health", {"account": "acc1"})
    assert result["ok"] and result["config"]["valid"] is False
    assert result["cookies"]["valid"] is False
    assert "private-cookie-marker" not in json.dumps(result)


@pytest.mark.asyncio
@pytest.mark.parametrize("channel,headless,expected", [
    (None, True, "driver_default"), ("chromium", False, "chromium"),
    ("chrome", True, "chrome"), ("msedge", False, "msedge"),
    ("private-invalid-channel", "private-invalid-option", "invalid"),
])
async def test_runtime_reports_only_safe_launch_options(mcp_server, config_loader, monkeypatch, channel, headless, expected):
    use_async_pool(mcp_server, monkeypatch)
    settings = config_loader.settings.setdefault("mcp", {})
    settings.update(browser_channel=channel, browser_headless=headless)
    result = await call_tool(mcp_server, "get_runtime_info")
    assert result["browser_configuration"] == {
        "channel": expected, "headless": headless if isinstance(headless, bool) else None,
        "persistent_profile": False, "cookie_snapshots_saved": False, "basis": "configured_options"}
    assert "private-invalid" not in json.dumps(result)
    assert result["live_authentication_checked"] is False
