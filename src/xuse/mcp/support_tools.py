"""Support MCP tools: list_drafts, get_draft, reject_draft, get_run_status,
get_account_health. All read-only except reject_draft, which is a local
status flip — nothing touches X."""
import json
import asyncio
import logging
import re
from pathlib import Path
from typing import Any, Dict, Optional

from xuse.core.config_loader import PROJECT_ROOT
from xuse.doctor import check_cookie_data
from xuse.models import AccountConfig

from . import executor as ex
from .executor import Ctx, ToolError
from .tools import guard, ok_
from .annotations import LOCAL_WRITE_IDEMPOTENT, READ_ONLY_LOCAL
from .local_reads import MAX_COOKIE_BYTES, read_json

logger = logging.getLogger(__name__)

_DRAFT_STATUSES = ("pending", "approved", "partial", "executed", "failed", "rejected", "uncertain")
_SAFE_ACCOUNT_ID = re.compile(r"[A-Za-z0-9_-]+")


def register_support_tools(server, ctx: Ctx) -> None:
    """Register local status, diagnostics and draft management tools."""

    @server.tool(annotations=READ_ONLY_LOCAL)
    @guard
    async def get_runtime_info() -> Dict[str, Any]:
        """Inspect server/package versions, tool count and configured execution
        mode, backend workflow support and effective unlock allowance without
        starting a browser. Contains no credentials, paths, account identities
        or raw environment values. Configuration does not prove live X
        authentication; use get_account_health and a bounded read for that.
        """
        import platform
        from importlib.metadata import PackageNotFoundError, version
        from xuse import __version__
        from mcp.types import LATEST_PROTOCOL_VERSION

        backend = getattr(ctx.session_pool, "backend", "selenium")
        async_browser = backend in ("patchright", "playwright")
        browser_configuration = None
        if async_browser:
            channel = ctx.config_loader.get_setting("mcp.browser_channel",
                ctx.config_loader.get_setting("browser_settings.channel", None))
            headless = ctx.config_loader.get_setting("mcp.browser_headless", True)
            browser_configuration = {
                "channel": channel if channel in ("chrome", "msedge", "chromium") else "driver_default" if channel is None else "invalid",
                "headless": headless if isinstance(headless, bool) else None,
                "persistent_profile": False,
                "cookie_snapshots_saved": False,
                "basis": "configured_options",
            }
        safety = ctx.safety_store if async_browser else None
        execution_support = {
            "basis": "configured_backend",
            "async_browser_tools": async_browser,
            "legacy_run_cycle": backend == "selenium",
            "durable_browser_safety": safety is not None,
            "resumable_thread_execution": async_browser and safety is not None,
            "live_ui_verified": False,
        }
        unlock_policy = None if safety is None else {
            "daily_cap": safety.caps["unlock"],
            "development_override": safety.development_unlock_override,
            "budget_timezone": "UTC",
            "remaining_requires": "get_account_safety",
        }
        packages = {}
        for name in ("mcp", backend):
            try:
                packages[name] = version(name)
            except PackageNotFoundError:
                packages[name] = None
        return ok_(server_version=__version__, python_version=platform.python_version(),
                   sdk_protocol_version=LATEST_PROTOCOL_VERSION, packages=packages,
                   browser_backend=backend, draft_mode=ctx.draft_mode,
                   auto_drain_enabled=ctx.queue_config.auto_drain.enabled,
                   tool_count=len(await server.list_tools()),
                   execution_support=execution_support, unlock_policy=unlock_policy,
                   browser_configuration=browser_configuration,
                   live_authentication_checked=False)

    @server.tool(annotations=READ_ONLY_LOCAL)
    @guard
    async def list_drafts(status: Optional[str] = None, account: Optional[str] = None,
                          limit: int = 20) -> Dict[str, Any]:
        """List drafts, newest first. `status` filters to one of
        pending/approved/partial/executed/failed/rejected/uncertain; `account` filters by
        account id; `limit` caps the response (max 100). Read-only."""
        if status is not None and status not in _DRAFT_STATUSES:
            raise ToolError(
                f"Unknown draft status '{status}'. Allowed: {list(_DRAFT_STATUSES)}.")
        drafts = ctx.draft_store.list(status=status)
        if account is not None:
            drafts = [d for d in drafts if d.account == account]
        drafts.sort(key=lambda d: d.created_at, reverse=True)
        total = len(drafts)
        limit = max(1, min(int(limit), 100))
        return ok_(total=total, returned=min(total, limit),
                   drafts=[json.loads(d.model_dump_json()) for d in drafts[:limit]])

    @server.tool(annotations=READ_ONLY_LOCAL)
    @guard
    async def get_draft(draft_id: str) -> Dict[str, Any]:
        """Show one draft by id. Read-only."""
        try:
            draft = ctx.draft_store.get(draft_id)
        except KeyError:
            raise ToolError(f"Unknown draft_id '{draft_id}'.") from None
        return ok_(draft=json.loads(draft.model_dump_json()))

    @server.tool(annotations=LOCAL_WRITE_IDEMPOTENT)
    @guard
    async def reject_draft(draft_id: str) -> Dict[str, Any]:
        """Reject a pending draft so it can never be approved. Local status
        change only, nothing touches X."""
        try:
            draft = ctx.draft_store.get(draft_id)
        except KeyError:
            raise ToolError(f"Unknown draft_id '{draft_id}'.") from None
        if draft.status != "pending":
            raise ToolError(
                f"Draft '{draft_id}' is already {draft.status}, "
                "only pending drafts can be rejected.")
        if draft.action == "publish_thread":
            from .thread_tools import _store
            store = _store(ctx)
            run_id = draft.payload.get("run_id")
            with store.execution_lock(run_id):
                run = store.validate_draft(run_id, draft)
                if run["state"] == "complete":
                    raise ToolError("A completed thread cannot be rejected.")
                # A blocked pending draft can retain prior authorization.
                # Revoke it durably under the same lock used by continuation;
                # an active run must finish before rejection can be accepted.
                store.cancel(run_id)
                ctx.draft_store.set_status(draft_id, "rejected")
        else:
            ctx.draft_store.set_status(draft_id, "rejected")
        return ok_(draft_id=draft_id, status="rejected")

    @server.tool(annotations=READ_ONLY_LOCAL)
    @guard
    async def get_run_status(run_id: Optional[str] = None) -> Dict[str, Any]:
        """Poll run_cycle handles. With `run_id` omitted, lists every run
        started since this server booted. Read-only."""
        def _public(run: Dict[str, Any]) -> Dict[str, Any]:
            return {k: v for k, v in run.items() if k != "task"}

        if run_id is not None:
            run = ctx.runs.get(run_id)
            if run is None:
                raise ToolError(f"Unknown run_id '{run_id}'.")
            return ok_(run_id=run_id, **_public(run))
        return ok_(count=len(ctx.runs),
                   runs=[{"run_id": rid, **_public(r)} for rid, r in ctx.runs.items()])

    @server.tool(annotations=READ_ONLY_LOCAL)
    @guard
    async def get_account_health(account: str) -> Dict[str, Any]:
        """Read-only health snapshot for one account: config presence,
        cookie-file validity, metrics counters, warm-session state, queue
        depth, pending drafts, and a transparent local readiness score out of
        100. Unknown evidence earns no points; this does not predict X
        enforcement or establish live authentication. Never starts a browser. A config that fails
        validation is REPORTED (config.valid=false with the error), not
        raised, surfacing that is the point of a health check."""
        raw = ctx.session_pool.find_account_dict(account)  # SessionError if unknown
        account_id = raw.get("account_id", account)
        if not _SAFE_ACCOUNT_ID.fullmatch(account_id):
            raise ToolError(f"Invalid account id: {account_id!r}")
        config_info: Dict[str, Any] = {"is_active": bool(raw.get("is_active", True))}
        try:
            AccountConfig.model_validate(raw)
            config_info["valid"] = True
        except Exception:
            config_info["valid"] = False
            # Validation errors may quote malformed inline cookie values.
            config_info["error"] = "Account configuration failed validation. Check its field types."
        # Surface corrupt-on-disk config (the loader boots empty on parse
        # failure and records the reason on these attributes).
        for attr, key in (("accounts_error", "loader_error"),
                          ("settings_error", "settings_error")):
            loader_error = getattr(ctx.config_loader, attr, None)
            if loader_error:
                config_info[key] = "Configuration could not be parsed; inspect its field types locally."
        cookies_info: Dict[str, Any]
        if getattr(ctx.session_pool, "backend", "selenium") in ("patchright", "playwright"):
            from xuse.browser.cookies import load_account_cookies
            from xuse.browser.errors import SessionError
            source = ("inline" if raw.get("cookies") is not None else
                      "file" if raw.get("cookie_file_path") else None)
            cookies_info = {"configured": source is not None, "source": source,
                            "valid": False, "problems": [],
                            "validation": "async_cookie_import",
                            "live_authentication_checked": False}
            try:
                # Use identical source precedence, relative paths and strict
                # validation as startup. Never return cookie data or paths.
                await asyncio.to_thread(load_account_cookies, raw, ctx.config_loader)
            except SessionError as error:
                cookies_info["problems"] = [error.reason]
            else:
                cookies_info["valid"] = True
        elif raw.get("cookies"):
            valid, problems = check_cookie_data(raw["cookies"])
            cookies_info = {"configured": True, "valid": valid, "problems": problems}
        elif raw.get("cookie_file_path"):
            cookie_path = Path(raw["cookie_file_path"])
            if not cookie_path.is_absolute():
                cookie_path = PROJECT_ROOT / raw["cookie_file_path"]
            if cookie_path.is_file():
                try:
                    data = await asyncio.to_thread(read_json, cookie_path, max_bytes=MAX_COOKIE_BYTES)
                    valid, problems = check_cookie_data(data)
                    cookies_info = {"configured": True, "file": raw["cookie_file_path"],
                                    "valid": valid, "problems": problems}
                except Exception as e:
                    cookies_info = {"configured": True, "file": raw["cookie_file_path"],
                                    "valid": False, "problems": [f"unreadable: {ex.sanitize_text(e)}"]}
            else:
                cookies_info = {"configured": True, "file": raw["cookie_file_path"],
                                "valid": False, "problems": ["cookie file not found"]}
        else:
            cookies_info = {"configured": False, "problems": ["no cookies configured"]}
        summary: Dict[str, Any] = {}
        summary_path = PROJECT_ROOT / "data" / "metrics" / f"{account_id}.json"
        if summary_path.exists():
            try:
                summary = await asyncio.to_thread(read_json, summary_path)
                if not isinstance(summary, dict):
                    raise ValueError("Metrics must be an object.")
                # Metrics summaries are not an event-log export. Preserve the
                # public counters/timestamps, never arbitrary stored bodies.
                from datetime import datetime
                public_summary = {"account_id": account_id, "counters": {}}
                known_counters = {"posts", "replies", "retweets", "quote_tweets", "likes", "errors",
                                  "read", "post", "reply", "retweet", "like", "message", "messages",
                                  "follow", "follows", "unlock", "bookmark", "bookmarks", "inbox"}
                counters = summary.get("counters", {})
                if isinstance(counters, dict):
                    public_summary["counters"] = {key: value for key, value in counters.items()
                        if key in known_counters and isinstance(value, int) and not isinstance(value, bool) and value >= 0}
                for key in ("last_run_started_at", "last_run_finished_at"):
                    value = summary.get(key)
                    if value is None:
                        public_summary[key] = None
                    elif isinstance(value, str):
                        try:
                            public_summary[key] = datetime.fromisoformat(value.replace("Z", "+00:00")).isoformat()
                        except ValueError:
                            public_summary[key] = None
                summary = public_summary
            except Exception:
                summary = {"unreadable": True}
        from .browser_tools import session_metadata
        from .health_scoring import score_account_health
        try:
            queue_counts = await asyncio.to_thread(ctx.queue_store.counts, account=account_id) if ctx.queue_store else {}
        except Exception:
            queue_counts = {"unavailable": True}
        try:
            pending_drafts = len([d for d in ctx.draft_store.list(status="pending")
                                  if d.account == account_id])
            drafts_info = {"pending": pending_drafts}
        except Exception:
            drafts_info = {"pending": None, "unavailable": True}
        try:
            session_info = session_metadata(ctx.session_pool, account_id)
        except Exception:
            session_info = {"warm": False, "state": "unknown", "browser_connected": None,
                            "tab_closed": None, "idle_seconds": None,
                            "live_authentication_checked": False}
        backend = getattr(ctx.session_pool, "backend", "selenium")
        proxy_info = {"configured": bool(raw.get("proxy") or ctx.config_loader.get_setting("browser_settings.proxy", None)),
                      "valid": None, "connectivity_checked": False}
        if backend in ("patchright", "playwright"):
            from xuse.browser.sessions import resolve_account_proxy
            try:
                resolved = await asyncio.to_thread(resolve_account_proxy, ctx.config_loader, raw)
            except Exception:
                proxy_info["valid"] = False
            else:
                proxy_info.update(valid=True, configured=resolved is not None)
                del resolved  # Never include proxy endpoints or credentials in diagnostics.
        else:
            from xuse.utils.proxy_manager import ProxyManager
            from xuse.core.browser_manager.options import validate_legacy_proxy
            try:
                # Preview validates every pool candidate without selecting a
                # route or advancing a round-robin cursor. The capability
                # check matches browser startup and does not create options.
                routes = ProxyManager(ctx.config_loader).preview_routes(raw.get("proxy"))
                browser_type = str(ctx.config_loader.get_setting("browser_settings.type", "firefox")).lower()
                driver_options = ctx.config_loader.get_setting("browser_settings.driver_options", [])
                for route in routes or [None]:
                    validate_legacy_proxy(route, browser_type, driver_options)
                proxy_info.update(valid=True, configured=bool(routes))
                del routes
            except Exception:
                proxy_info["valid"] = False
        safety_info = None
        policy_info = {}
        import time
        assessment_time = time.time()
        if ctx.safety_store is not None:
            try:
                safety_info = await asyncio.to_thread(ctx.safety_store.status, account_id)
                assessment_time = ctx.safety_store.clock()
                policy_info = {name: getattr(ctx.safety_store, name, None)
                               for name in ("read_interval", "write_interval", "per_minute")}
            except Exception:
                # Ledger failures are unknown evidence, never a healthy zero.
                safety_info = None
        health = score_account_health(config=config_info, cookies=cookies_info,
            proxy=proxy_info, session=session_info, safety=safety_info,
            queue=queue_counts if ctx.queue_store is not None else {"unavailable": True},
            now=assessment_time, policy=policy_info)
        return ok_(account=account_id,
                   config=config_info,
                   cookies=cookies_info,
                   metrics=summary or {"counters": {}},
                   session=session_info,
                   queue=queue_counts,
                   drafts=drafts_info,
                   proxy=proxy_info, health=health)
