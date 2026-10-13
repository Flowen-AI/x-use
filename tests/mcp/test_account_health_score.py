"""Local readiness scores use evidence, never live X or invented reputation."""
import json
from types import SimpleNamespace

import pytest
from mcp.server.mcpserver import MCPServer

from helpers import call_tool, make_account
from xuse.mcp import support_tools
from xuse.mcp.drafts import DraftStore
from xuse.mcp.executor import Ctx
from xuse.mcp.health_scoring import WEIGHTS, score_account_health
from xuse.mcp.safety import SafetyStore


NOW = 1_800_000_000.0


def baseline():
    caps = dict(SafetyStore.DEFAULT_CAPS)
    return dict(config={"valid": True, "is_active": True},
        cookies={"valid": True, "configured": True, "validation": "async_cookie_import"},
        proxy={"configured": False, "valid": True},
        session={"state": "ready", "warm": True, "browser_connected": True, "tab_closed": False},
        safety={"paused": False, "pause": None, "uncertain_actions": [],
                "recent_actions": [{"kind": "read", "status": "succeeded", "started": NOW - 1000}],
                "daily_caps": caps, "daily_remaining": dict(caps)}, queue={}, now=NOW,
        policy={"read_interval": 2, "write_interval": 90, "per_minute": 6})


def criterion(health, name):
    return next(item for item in health["criteria"] if item["id"] == name)


def test_transparent_weights_and_local_only_ceiling():
    result = score_account_health(**baseline())
    assert sum(WEIGHTS.values()) == 100
    assert result["score"] == sum(item["points"] for item in result["criteria"]) == 90
    assert result["evidence_coverage_percent"] == 90
    assert result["unknown_points"] == 10
    assert result["readiness"] == "local_checks_passed"
    assert result["x_enforcement_prediction"] is False
    assert criterion(result, "live_authentication")["evidence"] == {"checked": False, "authenticated": None}


def test_cold_session_and_no_history_are_unknown_not_failed_or_authenticated():
    data = baseline()
    data["session"] = {"state": "cold", "warm": False}
    data["safety"]["recent_actions"] = []
    result = score_account_health(**data)
    assert result["score"] == 75
    assert result["unknown_points"] == 25
    assert result["readiness"] == "unverified"
    assert criterion(result, "recent_reliability")["status"] == "unknown"
    assert criterion(result, "session_transport")["status"] == "unknown"
    assert result["blockers"] == []


def test_fully_blocked_local_evidence_reaches_zero_without_negative_points():
    data = baseline()
    data["config"].update(valid=False, is_active=False)
    data["cookies"]["valid"] = False
    data["proxy"]["valid"] = False
    data["session"]["state"] = "disconnected"
    data["safety"].update(paused=True, pause={"reason": "manual_pause"},
        uncertain_actions=[{"kind": "post", "status": "uncertain"}],
        recent_actions=[{"kind": "post", "status": "failed", "started": NOW - 1000}])
    data["safety"]["daily_remaining"]["post"] = 0
    result = score_account_health(**data)
    assert result["score"] == 0 and result["readiness"] == "blocked"
    assert all(item["points"] == 0 for item in result["criteria"])


@pytest.mark.parametrize("area,key,value,blocker", [
    ("config", "valid", False, "invalid_configuration"),
    ("config", "settings_error", "private-error", "invalid_configuration"),
    ("cookies", "valid", False, "invalid_or_missing_cookies"),
    ("proxy", "valid", False, "invalid_proxy"),
    ("session", "state", "disconnected", "session_unavailable"),
])
def test_critical_blocker_overrides_numeric_score(area, key, value, blocker):
    data = baseline()
    data[area][key] = value
    result = score_account_health(**data)
    assert result["readiness"] == "blocked"
    assert blocker in result["blockers"]
    assert result["score"] > 50
    assert "private-error" not in json.dumps(result)


@pytest.mark.parametrize("pause", ["manual_pause", "pin_required", "challenge"])
def test_durable_pause_is_not_reported_as_account_damage(pause):
    data = baseline()
    data["safety"].update(paused=True, pause={"reason": pause})
    result = score_account_health(**data)
    assert result["readiness"] == "paused"
    assert result["blockers"] == []
    assert result["x_enforcement_prediction"] is False
    assert criterion(result, "durable_safety")["points"] == 0


def test_inactive_account_is_deliberately_paused():
    data = baseline()
    data["config"]["is_active"] = False
    result = score_account_health(**data)
    assert result["readiness"] == "paused" and result["score"] == 85


@pytest.mark.parametrize("status,readiness", [("started", "waiting"), ("uncertain", "blocked")])
def test_write_outcomes_need_observation_before_retry(status, readiness):
    data = baseline()
    data["safety"]["uncertain_actions"] = [{"kind": "message", "status": status, "body": "private-message"}]
    result = score_account_health(**data)
    assert result["readiness"] == readiness
    assert criterion(result, "action_outcomes")["points"] == 0
    assert "private-message" not in json.dumps(result)


def test_truncated_snapshot_cannot_certify_no_older_unresolved_writes():
    data = baseline()
    data["safety"]["uncertain_actions"] = [{"kind": "read", "status": "started"}] * 50
    result = score_account_health(**data)
    assert criterion(result, "action_outcomes")["status"] == "unknown"
    assert result["readiness"] == "incomplete"


def test_exhausted_budget_waits_but_deliberately_disabled_actions_do_not_penalize():
    data = baseline()
    data["safety"]["daily_caps"]["unlock"] = 0
    data["safety"]["daily_remaining"]["unlock"] = 0
    assert score_account_health(**data)["score"] == 90
    data["safety"]["daily_remaining"]["message"] = 0
    result = score_account_health(**data)
    assert result["readiness"] == "waiting"
    assert criterion(result, "daily_budgets")["evidence"]["exhausted_action_kinds"] == ["message"]
    assert result["score"] == 85


def test_disabling_every_action_reports_deliberate_pause_not_ready_or_damaged():
    data = baseline()
    data["safety"]["daily_caps"] = dict.fromkeys(SafetyStore.DEFAULT_CAPS, 0)
    data["safety"]["daily_remaining"] = dict.fromkeys(SafetyStore.DEFAULT_CAPS, 0)
    result = score_account_health(**data)
    assert result["readiness"] == "paused" and result["score"] == 85
    assert result["blockers"] == []
    assert criterion(result, "daily_budgets")["status"] == "paused"


def test_cooldown_changes_readiness_without_calling_pacing_account_damage():
    data = baseline()
    data["safety"]["recent_actions"] = [{"kind": "post", "status": "succeeded", "started": NOW - 10}]
    result = score_account_health(**data)
    assert result["readiness"] == "waiting" and result["score"] == 90
    assert criterion(result, "daily_budgets")["evidence"]["cooldowns"]["write_seconds"] == 80
    data["now"] += 100
    assert score_account_health(**data)["readiness"] == "local_checks_passed"


def test_minute_pacing_uses_latest_bounded_reservations_without_resetting_budget():
    data = baseline()
    data["policy"]["per_minute"] = 2
    data["safety"]["recent_actions"] = [
        {"kind": "read", "status": "succeeded", "started": NOW - 50},
        {"kind": "read", "status": "succeeded", "started": NOW - 10},
        {"kind": "read", "status": "succeeded", "started": NOW - 5},
    ]
    result = score_account_health(**data)
    assert criterion(result, "daily_budgets")["evidence"]["cooldowns"]["minute_seconds"] == 50
    assert result["score"] == 90 and result["readiness"] == "waiting"


def test_incomplete_budget_snapshot_cannot_earn_readiness_points():
    data = baseline()
    del data["safety"]["daily_remaining"]["message"]
    result = score_account_health(**data)
    assert criterion(result, "daily_budgets")["status"] == "unknown"
    assert result["readiness"] == "incomplete"


def test_reliability_uses_recent_completed_sample_and_improves_after_recovery():
    data = baseline()
    data["safety"]["recent_actions"] += [
        {"kind": "post", "status": "failed", "started": NOW - 500},
        {"kind": "post", "status": "failed", "started": NOW - 86401},
        {"kind": "post", "status": "started", "started": NOW - 200},
    ]
    result = score_account_health(**data)
    reliability = criterion(result, "recent_reliability")
    assert reliability["evidence"]["completed_sample"] == 2
    assert reliability["evidence"]["failed_sample"] == 1
    data["safety"]["recent_actions"][1]["status"] = "succeeded"
    assert score_account_health(**data)["score"] > result["score"]


@pytest.mark.parametrize("safety", [None, {}, {"paused": False}])
def test_absent_or_malformed_safety_is_unknown_evidence(safety):
    data = baseline()
    data["safety"] = safety
    result = score_account_health(**data)
    assert result["readiness"] == "incomplete"
    assert criterion(result, "durable_safety")["status"] == "unknown"
    assert criterion(result, "daily_budgets")["status"] == "unknown"
    assert result["score"] <= 55


def test_arbitrary_private_store_fields_are_never_returned():
    data = baseline()
    data["safety"].update(secret="private-cookie", paused=True, pause={"reason": "private-message"})
    data["safety"]["recent_actions"][0].update(body="private-message", proxy="http://private-user:private-pass@proxy.invalid")
    data["queue"] = {"failed": 2, "body": "private-body"}
    result = json.dumps(score_account_health(**data))
    assert all(value not in result for value in ("private-cookie", "private-message", "private-user", "private-pass", "private-body"))


@pytest.fixture
def health_server(make_config_loader, tmp_path):
    clock = [NOW]
    raw = make_account("health", cookies=[
        {"name": name, "value": "private-synthetic-" + name, "domain": ".x.com", "secure": True}
        for name in ("auth_token", "ct0")])
    loader = make_config_loader(accounts=[raw])
    cold = {"warm": False, "state": "cold", "browser_connected": None, "tab_closed": None,
            "live_authentication_checked": False}
    def forbidden(*args, **kwargs):
        raise AssertionError("Health must not start or acquire a browser")
    pool = SimpleNamespace(backend="patchright", find_account_dict=lambda account: raw,
        session_status=lambda account: dict(cold), acquire=forbidden, entry_for=forbidden)
    safety = SafetyStore(tmp_path / "health.sqlite3", {"read_interval_seconds": 0,
        "write_interval_seconds": 0}, clock=lambda: clock[0])
    ctx = Ctx(loader, pool, DraftStore(), safety_store=safety)
    server = MCPServer("isolated-health")
    support_tools.register_support_tools(server, ctx)
    return server, ctx, raw, clock, cold


@pytest.mark.asyncio
async def test_mcp_local_score_preserves_existing_fields_without_starting_browser(health_server):
    server, ctx, raw, clock, cold = health_server
    result = await call_tool(server, "get_account_health", {"account": "health"})
    assert result["ok"] is True and result["health"]["score"] == 75
    assert {"config", "cookies", "metrics", "session", "queue", "drafts"} <= set(result)
    assert result["proxy"] == {"configured": False, "valid": True, "connectivity_checked": False}
    assert "private-synthetic" not in json.dumps(result)


@pytest.mark.asyncio
async def test_mcp_real_ledger_pause_uncertain_reconciliation_and_resume(health_server):
    server, ctx, raw, clock, cold = health_server
    action = ctx.safety_store.reserve("health", "post")
    ctx.safety_store.finish("health", action, "uncertain")
    ctx.safety_store.pause("health", "manual_pause")
    result = await call_tool(server, "get_account_health", {"account": "health"})
    assert result["health"]["readiness"] == "blocked"
    ctx.safety_store.reconcile("health", action, "succeeded")
    ctx.safety_store.resume("health")
    clock[0] += 1000
    cold.update(state="ready", warm=True, browser_connected=True, tab_closed=False)
    recovered = await call_tool(server, "get_account_health", {"account": "health"})
    assert recovered["health"]["score"] == 90
    assert recovered["health"]["readiness"] == "local_checks_passed"
    assert recovered["health"]["live_authentication_checked"] is False


@pytest.mark.asyncio
async def test_mcp_invalid_proxy_never_leaks_endpoint_credentials(health_server):
    server, ctx, raw, clock, cold = health_server
    raw["proxy"] = "http://private-user:private-password@/"
    result = await call_tool(server, "get_account_health", {"account": "health"})
    assert result["health"]["readiness"] == "blocked"
    assert "invalid_proxy" in result["health"]["blockers"]
    assert all(value not in json.dumps(result) for value in ("private-user", "private-password"))


@pytest.mark.asyncio
@pytest.mark.parametrize("browser,route,expected", [
    ("chrome", "http://proxy.invalid:8080", True),
    ("chrome", "http://private-user:private-password@proxy.invalid:8080", False),
    ("firefox", "https://proxy.invalid:8080", False),
    ("firefox", "socks5://proxy.invalid:1080", True),
])
async def test_mcp_legacy_proxy_validation_matches_backend_capabilities(health_server, browser, route, expected):
    server, ctx, raw, clock, cold = health_server
    ctx.session_pool.backend = "selenium"
    ctx.config_loader.settings["browser_settings"] = {"type": browser}
    raw["proxy"] = route
    result = await call_tool(server, "get_account_health", {"account": "health"})
    assert result["proxy"]["valid"] is expected
    assert criterion(result["health"], "proxy")["status"] == ("pass" if expected else "fail")
    assert all(value not in json.dumps(result) for value in ("proxy.invalid", "private-user", "private-password"))


@pytest.mark.asyncio
async def test_mcp_legacy_round_robin_preview_never_reads_or_advances_cursor(health_server, monkeypatch):
    from xuse.utils.proxy_manager import ProxyManager
    server, ctx, raw, clock, cold = health_server
    ctx.session_pool.backend = "selenium"
    ctx.config_loader.settings["browser_settings"] = {"type": "chrome", "proxy_pool_strategy": "round_robin",
        "proxy_pools": {"test": ["http://first.invalid:8080", "http://second.invalid:8080"]}}
    raw["proxy"] = "pool:test"
    def forbidden(*args, **kwargs):
        raise AssertionError("Read-only health must not access the route cursor")
    monkeypatch.setattr(ProxyManager, "_load_state", forbidden)
    monkeypatch.setattr(ProxyManager, "_save_state", forbidden)
    result = await call_tool(server, "get_account_health", {"account": "health"})
    assert result["proxy"]["valid"] is True
    ctx.config_loader.settings["browser_settings"]["proxy_pools"]["test"].append("http://user:password@bad.invalid:8080")
    rejected = await call_tool(server, "get_account_health", {"account": "health"})
    assert rejected["proxy"]["valid"] is False


@pytest.mark.asyncio
async def test_mcp_legacy_direct_route_rejects_conflicting_driver_override(health_server):
    server, ctx, raw, clock, cold = health_server
    ctx.session_pool.backend = "selenium"
    ctx.config_loader.settings["browser_settings"] = {"type": "chrome", "driver_options": ["--proxy-server=http://private.invalid:8080"]}
    result = await call_tool(server, "get_account_health", {"account": "health"})
    assert result["proxy"]["valid"] is False
    assert "private.invalid" not in json.dumps(result)


@pytest.mark.asyncio
async def test_mcp_ledger_failure_reports_unknown_without_exception_detail(health_server, monkeypatch):
    server, ctx, raw, clock, cold = health_server
    def unavailable(*args):
        raise OSError("private-ledger-path-and-secret")
    monkeypatch.setattr(ctx.safety_store, "status", unavailable)
    result = await call_tool(server, "get_account_health", {"account": "health"})
    assert result["ok"] is True
    assert criterion(result["health"], "durable_safety")["status"] == "unknown"
    assert "private-ledger" not in json.dumps(result)


@pytest.mark.asyncio
async def test_mcp_local_queue_and_draft_failures_are_bounded_private_diagnostics(health_server, monkeypatch):
    server, ctx, raw, clock, cold = health_server
    def unavailable(*args, **kwargs):
        raise OSError("private-message-body-in-corrupt-store")
    ctx.queue_store = SimpleNamespace(counts=unavailable)
    monkeypatch.setattr(ctx.draft_store, "list", unavailable)
    result = await call_tool(server, "get_account_health", {"account": "health"})
    assert result["ok"] is True
    assert result["queue"] == {"unavailable": True}
    assert result["drafts"] == {"pending": None, "unavailable": True}
    assert "private-message" not in json.dumps(result)


@pytest.mark.asyncio
async def test_mcp_metrics_return_counters_not_event_bodies_or_private_metadata(health_server):
    server, ctx, raw, clock, cold = health_server
    path = support_tools.PROJECT_ROOT / "data" / "metrics" / "health.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"account_id": "private-account", "counters": {"posts": 2, "private-key": 1},
        "events": [{"message": "private-message"}], "cookie": "private-cookie",
        "last_run_started_at": "private-timestamp", "last_run_finished_at": "2026-10-12T12:00:00"}))
    result = await call_tool(server, "get_account_health", {"account": "health"})
    assert result["metrics"]["counters"] == {"posts": 2}
    assert result["metrics"]["last_run_finished_at"] == "2026-10-12T12:00:00"
    assert "private-" not in json.dumps(result)


@pytest.mark.asyncio
async def test_mcp_cookie_expiry_is_a_blocker_not_an_authentication_probe(health_server):
    server, ctx, raw, clock, cold = health_server
    raw["cookies"][0]["expires"] = 1
    result = await call_tool(server, "get_account_health", {"account": "health"})
    assert result["cookies"]["problems"] == ["expired_credentials"]
    assert "invalid_or_missing_cookies" in result["health"]["blockers"]
    assert result["health"]["live_authentication_checked"] is False
