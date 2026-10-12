"""Transparent local account readiness scoring; never predicts X enforcement."""
import math
from typing import Any, Dict, Optional


WEIGHTS = {"configuration": 10, "account_active": 5, "cookies": 20,
           "proxy": 10, "session_transport": 10, "durable_safety": 15,
           "action_outcomes": 10, "daily_budgets": 5,
           "recent_reliability": 5, "live_authentication": 10}
_KINDS = frozenset({"read", "post", "reply", "like", "retweet", "message",
                    "follow", "unlock", "bookmark", "inbox"})
_STATES = frozenset({"cold", "starting", "ready", "busy", "closing", "disconnected", "closed"})
_PAUSES = frozenset({"manual_pause", "pin_required", "challenge", "rate_limited",
                     "login_required", "account_restricted", "account_locked", "session_expired"})


def _number(value: Any) -> bool:
    try:
        return (not isinstance(value, bool) and isinstance(value, (int, float))
                and math.isfinite(value))
    except OverflowError:
        return False


def _count(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def score_account_health(*, config: Dict[str, Any], cookies: Dict[str, Any],
                         proxy: Dict[str, Any], session: Dict[str, Any],
                         safety: Optional[Dict[str, Any]], queue: Dict[str, Any],
                         now: float, policy: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Evaluate allowlisted evidence only; arbitrary store values never escape.

    Unknown evidence earns zero points and is reported separately from a
    failed check. The authentication criterion intentionally stays unknown:
    this local-only call does not navigate, and warm transport is not login.
    """
    criteria = []
    recommendations = []
    blockers = []
    waits = []
    paused = False
    policy = policy or {}

    def add(key, status, points, evidence, recommendation=None, priority="recommended"):
        criteria.append({"id": key, "status": status, "points": points,
                         "max_points": WEIGHTS[key], "evidence": evidence})
        if recommendation:
            recommendations.append({"criterion": key, "priority": priority,
                                    "action": recommendation})

    configuration_valid = config.get("valid") is True and not any(
        config.get(key) for key in ("loader_error", "settings_error"))
    add("configuration", "pass" if configuration_valid else "fail", 10 if configuration_valid else 0,
        {"account_schema_valid": config.get("valid") is True,
         "configuration_load_errors": bool(config.get("loader_error") or config.get("settings_error"))},
        None if configuration_valid else "Fix account and settings validation errors before running actions.", "required")
    if not configuration_valid:
        blockers.append("invalid_configuration")
    active = config.get("is_active") is True
    add("account_active", "pass" if active else "paused", 5 if active else 0,
        {"is_active": active}, None if active else "Keep the deliberate pause until you intend to resume this account.", "informational")
    paused = not active

    valid_cookies = cookies.get("valid") is True
    strict = cookies.get("validation") == "async_cookie_import"
    cookie_points = 20 if valid_cookies and strict else 10 if valid_cookies else 0
    add("cookies", "pass" if cookie_points == 20 else "partial" if valid_cookies else "fail", cookie_points,
        {"configured": cookies.get("configured") is True, "valid": valid_cookies,
         "validation": "strict_import" if strict else "legacy_presence_and_expiry",
         "proves_live_authentication": False},
        None if cookie_points == 20 else "Validate a fresh secure X cookie export; file validity alone does not establish a signed-in session.", "required" if not valid_cookies else "recommended")
    if not valid_cookies:
        blockers.append("invalid_or_missing_cookies")

    proxy_valid = proxy.get("valid")
    add("proxy", "pass" if proxy_valid is True else "fail" if proxy_valid is False else "unknown",
        10 if proxy_valid is True else 0,
        {"configured": proxy.get("configured") is True, "valid": proxy_valid if isinstance(proxy_valid, bool) else None,
         "connectivity_checked": False},
        None if proxy_valid is True else "Validate the configured proxy route and pool without silently falling back to a direct connection.", "required" if proxy_valid is False else "recommended")
    if proxy_valid is False:
        blockers.append("invalid_proxy")

    state = session.get("state") if session.get("state") in _STATES else "unknown"
    transport_ready = (state in ("ready", "busy") and session.get("browser_connected") is True
                       and session.get("tab_closed") is False)
    unavailable = state in ("disconnected", "closed")
    add("session_transport", "pass" if transport_ready else "fail" if unavailable else "unknown", 10 if transport_ready else 0,
        {"state": state, "warm": session.get("warm") is True,
         "browser_connected": session.get("browser_connected") if isinstance(session.get("browser_connected"), bool) else None,
         "tab_closed": session.get("tab_closed") if isinstance(session.get("tab_closed"), bool) else None,
         "proves_live_authentication": False},
        None if transport_ready else "Use a bounded read when ready to start or verify the local session; a cold session is not evidence of account damage.")
    if state in ("busy", "starting", "closing"):
        waits.append("session_" + state)
    if unavailable:
        blockers.append("session_unavailable")

    available = (isinstance(safety, dict) and isinstance(safety.get("paused"), bool)
                 and isinstance(safety.get("uncertain_actions"), list)
                 and isinstance(safety.get("recent_actions"), list))
    safety = safety if available else {}
    durable_paused = safety.get("paused") is True
    pause = safety.get("pause") or {}
    reason = pause.get("reason") if isinstance(pause, dict) else None
    reason = reason if reason in _PAUSES else "other" if durable_paused else None
    add("durable_safety", "paused" if durable_paused else "pass" if available else "unknown", 15 if available and not durable_paused else 0,
        {"available": available, "paused": durable_paused if available else None, "pause_reason": reason},
        "Resolve the recorded pause and use the reviewed recovery flow; do not reset budgets or retry blindly." if durable_paused else None if available else "Restore the durable action ledger before relying on local safeguards.", "required" if not available else "informational")
    paused = paused or durable_paused

    pending = safety.get("uncertain_actions")
    pending = pending if isinstance(pending, list) else []
    unresolved = sum(isinstance(row, dict) and row.get("status") == "uncertain"
                     and row.get("kind") in _KINDS - {"read", "unlock"} for row in pending)
    started = sum(isinstance(row, dict) and row.get("status") == "started"
                  and row.get("kind") in _KINDS - {"read", "unlock"} for row in pending)
    outcomes_known = available and len(pending) < 50
    add("action_outcomes", "fail" if unresolved else "waiting" if started else "pass" if outcomes_known else "unknown",
        10 if outcomes_known and not unresolved and not started else 0,
        {"unresolved_writes_in_snapshot": unresolved, "started_writes_in_snapshot": started,
         "snapshot_limit": 50, "snapshot_may_be_truncated": len(pending) >= 50},
        "Inspect the original outcome and reconcile it before retrying a write." if unresolved else "Wait for active writes to finish; investigate interrupted work before retrying." if started else None if outcomes_known else "Inspect the durable ledger; unavailable or truncated evidence cannot rule out unresolved writes.", "required" if unresolved else "recommended")
    if unresolved:
        blockers.append("unresolved_write_outcome")
    if started:
        waits.append("write_in_progress_or_interrupted")

    caps, remaining = safety.get("daily_caps"), safety.get("daily_remaining")
    budgets_known = (available and isinstance(caps, dict) and isinstance(remaining, dict)
                     and all(isinstance(caps.get(kind), int) and not isinstance(caps.get(kind), bool)
                             and caps[kind] >= 0 and isinstance(remaining.get(kind), int)
                             and not isinstance(remaining.get(kind), bool) and remaining[kind] >= 0
                             for kind in _KINDS))
    caps, remaining = caps if isinstance(caps, dict) else {}, remaining if isinstance(remaining, dict) else {}
    enabled = sorted(kind for kind in _KINDS if _count(caps.get(kind)) > 0)
    all_disabled = budgets_known and not enabled
    exhausted = [kind for kind in enabled if remaining.get(kind) == 0]
    disabled = sorted(kind for kind in _KINDS if caps.get(kind) == 0)
    recent = safety.get("recent_actions")
    recent = [row for row in recent if isinstance(row, dict)] if isinstance(recent, list) else []
    recent = [row for row in recent if row.get("kind") in _KINDS and _number(row.get("started")) and 0 <= now - row["started"] <= 86400]
    cooldowns = {}
    for key, kinds, interval_key in (("read_seconds", {"read", "unlock"}, "read_interval"),
                                     ("write_seconds", _KINDS - {"read", "unlock"}, "write_interval")):
        interval = policy.get(interval_key)
        starts = [row["started"] for row in recent if row["kind"] in kinds]
        cooldowns[key] = max(0, math.ceil(max(starts) + interval - now)) if starts and _number(interval) and interval >= 0 else 0
    minute = sorted(row["started"] for row in recent if row["started"] > now - 60)
    per_minute = _count(policy.get("per_minute"))
    cooldowns["minute_seconds"] = max(0, math.ceil(minute[-per_minute] + 60 - now)) if per_minute and len(minute) >= per_minute else 0
    add("daily_budgets", "paused" if all_disabled else "waiting" if exhausted else "pass" if budgets_known else "unknown", 5 if budgets_known and not exhausted and not all_disabled else 0,
        {"available": budgets_known, "exhausted_action_kinds": exhausted, "disabled_action_kinds": disabled,
         "budget_timezone": "UTC", "cooldowns": cooldowns,
         "cooldown_basis": "latest_50_action_snapshot"},
        "All local action kinds are deliberately disabled; keep those limits until you intend to enable an action." if all_disabled else "Wait for the relevant local allowance to reset; increasing a limit is not an account-health remedy." if exhausted else "Respect the current pacing interval before the next action." if any(cooldowns.values()) else None if budgets_known else "Inspect configured daily limits before scheduling actions.", "informational")
    paused = paused or all_disabled
    if exhausted:
        waits.append("daily_budget_exhausted")
    if any(cooldowns.values()):
        waits.append("local_pacing_cooldown")

    succeeded = sum(row.get("status") == "succeeded" for row in recent)
    failed = sum(row.get("status") == "failed" for row in recent)
    completed = succeeded + failed
    queue_available = queue.get("unavailable") is not True
    add("recent_reliability", "pass" if completed and not failed else "partial" if completed else "unknown",
        round(5 * succeeded / completed) if completed else 0,
        {"window_seconds": 86400, "window_basis": "reservation_started", "maximum_sample_actions": 50, "completed_sample": completed,
         "succeeded_sample": succeeded, "failed_sample": failed,
         "queue_failed_total": _count(queue.get("failed")) if queue_available else None,
         "queue_counts_available": queue_available, "queue_counts_are_lifetime": True},
        "Inspect recent failures and failed queued work before scheduling replacements; these are local outcomes, not an X enforcement score." if failed or _count(queue.get("failed")) else None if completed else "No recent completed action sample is available; do not create activity solely to improve this score.")
    add("live_authentication", "unknown", 0,
        {"checked": False, "authenticated": None},
        "A bounded signed-in read is needed to verify live authentication; this health check deliberately makes no X requests.", "informational")

    unknown = sum(item["max_points"] for item in criteria if item["status"] == "unknown")
    readiness = ("blocked" if blockers else "paused" if paused else "waiting" if waits else
                 "incomplete" if not available or proxy_valid is None or not outcomes_known or not budgets_known else
                 "local_checks_passed" if transport_ready else "unverified")
    return {"score": sum(item["points"] for item in criteria), "max_score": 100,
            "assessment": "local_operational_readiness", "readiness": readiness,
            "evidence_coverage_percent": 100 - unknown, "unknown_points": unknown,
            "live_authentication_checked": False, "x_enforcement_prediction": False,
            "local_only_max_score": 90, "criteria": criteria,
            "blockers": blockers, "waiting_reasons": waits, "recommendations": recommendations,
            "limitations": ["Not a prediction of suspension, reach, spam classification, or X trust.",
                            "Unknown evidence earns no points; a cold session or deliberate pause is not account damage.",
                            "Local action samples are bounded to the latest 50 records within 24 hours."]}
