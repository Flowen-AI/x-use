"""Proxy-management MCP tools: list_proxies, add_proxy, remove_proxy,
test_proxy.

Pools live in settings.json (browser_settings.proxy_pools); accounts point at
a direct URL or "pool:<name>". Mutations go through SettingsConfigWriter
(backup + atomic replace) and refresh the in-memory ConfigLoader. Every URL
in every response is fully userinfo-masked (scheme://***@host:port).
test_proxy drives a throwaway browser using the configured backend. It visits
only the IP-echo endpoint, without X login or account cookies. Latency includes
browser startup and the request.
"""
import asyncio
import ipaddress
import json
import re
import time
from typing import Any, Dict, Optional

from xuse.core.browser_manager import BrowserManager
from xuse.core.config_loader import PROJECT_ROOT
from xuse.core.settings_writer import SettingsConfigWriter, SettingsWriteError
from xuse.utils.proxy_manager import ProxyManager
from xuse.utils.proxy_manager import validate_proxy_url as _validate_proxy_url
from xuse.utils.sanitize import mask_url_userinfo
from xuse.browser.sessions import PatchrightSessionPool, PlaywrightSessionPool, resolve_account_proxy
from xuse.browser.errors import SessionError

from . import executor as ex
from .executor import Ctx, ToolError
from .tools import guard, ok_
from .annotations import LOCAL_DESTRUCTIVE, LOCAL_WRITE_IDEMPOTENT, READ_ONLY_FROM_X, READ_ONLY_LOCAL

IP_ECHO_URL = "https://api.ipify.org/?format=json"
PROXY_PROBE_TIMEOUT_SECONDS = 60


def _egress_ip(text: str) -> str:
    """Only return an actual IP address, never arbitrary proxy response text."""
    match = re.search(r'"ip"\s*:\s*"([^"\r\n]{1,64})"', text) if isinstance(text, str) and len(text) <= 100_000 else None
    if match and '%' not in match.group(1):
        try:
            return str(ipaddress.ip_address(match.group(1)))
        except ValueError:
            pass
    raise ToolError("Proxy did not report an egress IP (connection failed or blocked).")


def _probe_lock(ctx):
    lock = getattr(ctx, "_proxy_probe_lock", None)
    if lock is None:
        lock = ctx._proxy_probe_lock = asyncio.Lock()
    return lock


async def _close_proxy_probe_locked(ctx) -> bool:
    """Close the retained owner while holding the probe lifecycle lock."""
    pool = getattr(ctx, "_proxy_probe_pool", None)
    if pool is not None:
        await pool.close_all()
        if getattr(pool, "_process_guard", None) is not None or getattr(pool, "_runtime", None) is not None:
            return False
        if getattr(ctx, "_proxy_probe_pool", None) is pool:
            ctx._proxy_probe_pool = None
    # A timed-out/cancelled Selenium call continues in its worker thread. Do
    # not race that thread with another quit call or lose its retained owner.
    worker = getattr(ctx, "_proxy_probe_task", None)
    if worker is not None:
        if not worker.done():
            return False
        try:
            worker.result()
        except BaseException:
            pass
        ctx._proxy_probe_task = None
    manager = getattr(ctx, "_proxy_probe_manager", None)
    if manager is not None:
        if manager.driver is not None:
            worker = asyncio.create_task(asyncio.to_thread(manager.close_driver, strict=True))
            ctx._proxy_probe_task = worker
            worker.add_done_callback(_consume_probe_exception)
            try:
                await asyncio.wait_for(asyncio.shield(worker), 30)
            except asyncio.TimeoutError:
                return False
            ctx._proxy_probe_task = None
        if manager.driver is not None:
            return False
        ctx._proxy_probe_manager = None
    return True


def _consume_probe_exception(task):
    """Keep background thread failures observed while retaining their owner."""
    if not task.cancelled():
        task.exception()


def _has_retained_probe(ctx):
    return any(getattr(ctx, field, None) is not None for field in (
        "_proxy_probe_pool", "_proxy_probe_manager", "_proxy_probe_task"))


async def close_proxy_probe(ctx) -> bool:
    """Wait for an active probe, then retry its own retained runtime cleanup."""
    # A pool's direct browser launch is not an account startup task. Waiting
    # for its probe lock prevents shutdown from racing resource publication.
    async with _probe_lock(ctx):
        return await _close_proxy_probe_locked(ctx)


async def _probe_async_proxy(ctx, backend: str, proxy: dict) -> Dict[str, Any]:
    """Use isolated runtime ownership without touching an account session."""
    async with _probe_lock(ctx):
        if _has_retained_probe(ctx):
            raise ToolError("A previous proxy probe has unconfirmed cleanup; restart the server before another probe.")
        pool_type = PatchrightSessionPool if backend == "patchright" else PlaywrightSessionPool
        pool = pool_type(ctx.config_loader)
        # Synthetic probes are not account entries. Keep their idle reaper
        # beyond the bounded probe deadline even if account TTL is very short.
        pool.idle_timeout_seconds = max(pool.idle_timeout_seconds, 120)
        # Retain before any resource-producing await. Failed/cancelled cleanup
        # keeps the owner reachable and blocks subsequent probe launches.
        ctx._proxy_probe_pool = pool
        started = time.monotonic()
        try:
            async def probe():
                browser = await pool._get_browser()
                context = await browser.new_context(proxy=proxy, service_workers="block", accept_downloads=False)
                async def echo_only(route):
                    if route.request.url == IP_ECHO_URL:
                        await route.continue_()
                    else:
                        await route.abort()
                await context.route("**/*", echo_only)
                page = await context.new_page()
                await pool._refresh_owned_processes()
                page.set_default_timeout(5_000)
                response = await page.goto(IP_ECHO_URL, wait_until="domcontentloaded", timeout=25_000)
                if response is None or not 200 <= response.status < 300:
                    raise ToolError("Proxy echo request failed or was blocked.")
                address = _egress_ip(await page.locator("body").inner_text())
                return {"egress_ip": address, "latency_ms": int((time.monotonic() - started) * 1000)}
            return await asyncio.wait_for(probe(), PROXY_PROBE_TIMEOUT_SECONDS)
        finally:
            if not await _close_proxy_probe_locked(ctx):
                raise ToolError("Proxy probe cleanup could not confirm browser exit; restart the server before another probe.")


async def _probe_legacy_proxy(ctx, resolved: str) -> Dict[str, Any]:
    """Bound the caller's wait while retaining any unfinished WebDriver owner."""
    async with _probe_lock(ctx):
        if _has_retained_probe(ctx):
            raise ToolError("A previous proxy probe has unconfirmed cleanup; restart the server before another probe.")
        options = ctx.config_loader.get_setting("browser_settings.driver_options", [])
        if isinstance(options, list) and any(isinstance(option, str) and
                option.strip().lower().lstrip('-').split('=', 1)[0] in {'profile', 'p', 'user-data-dir', 'profile-directory'}
                for option in options):
            raise ToolError("Proxy probes require a temporary browser profile; remove persistent-profile driver options.")
        manager = BrowserManager(account_config={
            "account_id": "__proxy_probe__", "proxy": resolved, "is_active": True},
            config_loader=ctx.config_loader)
        ctx._proxy_probe_manager = manager

        def probe():
            try:
                started = time.monotonic()
                driver = manager.get_driver(authenticate=False)
                driver.set_page_load_timeout(25)
                driver.get(IP_ECHO_URL)
                return {"egress_ip": _egress_ip(driver.page_source or ""),
                        "latency_ms": int((time.monotonic() - started) * 1000)}
            finally:
                manager.close_driver(strict=True)

        worker = asyncio.create_task(asyncio.to_thread(probe))
        ctx._proxy_probe_task = worker
        worker.add_done_callback(_consume_probe_exception)
        try:
            return await asyncio.wait_for(asyncio.shield(worker), PROXY_PROBE_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            raise ToolError("Proxy probe timed out; its browser cleanup remains pending. Restart the server before another probe.") from None
        finally:
            if worker.done():
                ctx._proxy_probe_task = None
                if manager.driver is None:
                    ctx._proxy_probe_manager = None


def mask_proxy(url: Optional[str]) -> Optional[str]:
    """Mask the whole userinfo: http://user:pass@host:port -> http://***@host:port.

    Delegates to the single implementation in xuse.utils.sanitize.
    """
    return mask_url_userinfo(url)


def validate_proxy_url(url: str) -> str:
    """Trim + validate scheme and host. URLs containing ${ENV_VAR} pass a
    structural check only — the env var may legitimately resolve later.
    Delegates to the shared validator in xuse.utils.proxy_manager so the MCP
    tools and the accounts write gate cannot drift."""
    try:
        return _validate_proxy_url(url)
    except ValueError as e:
        raise ToolError(str(e)) from e


def _pools_snapshot(ctx: Ctx) -> Dict[str, Any]:
    pools_raw = ctx.config_loader.get_setting("browser_settings.proxy_pools", {}) or {}
    state_file = ctx.config_loader.get_setting(
        "browser_settings.proxy_pool_state_file", "data/proxy_pools_state.json")
    state: Dict[str, Any] = {}
    state_path = PROJECT_ROOT / state_file
    if state_path.is_file():
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except Exception:
            state = {}
    return {
        name: {
            "size": len(urls),
            "proxies": [mask_proxy(u) for u in urls],
            "rotation_cursor": state.get(name),
        }
        for name, urls in pools_raw.items()
    }


def _mutate_pools(ctx: Ctx, fn) -> Dict[str, Any]:
    writer = SettingsConfigWriter(ctx.config_loader.settings_file)

    def _apply(settings: Dict[str, Any]) -> Dict[str, Any]:
        browser = settings.setdefault("browser_settings", {})
        pools = browser.setdefault("proxy_pools", {})
        fn(pools)
        return settings

    try:
        updated = writer.mutate(_apply)
    except SettingsWriteError as e:
        raise ToolError(ex.sanitize_text(str(e))) from e
    ctx.config_loader.settings = updated
    return updated


def register_proxy_tools(server, ctx: Ctx) -> None:
    """Register the four proxy-management tools on the FastMCP server."""

    @server.tool(annotations=READ_ONLY_LOCAL)
    @guard
    async def list_proxies() -> Dict[str, Any]:
        """List proxy pools (size, masked members, rotation cursor), the global
        pool strategy, each account's assignment, and the global fallback
        proxy. Read-only, never starts a browser."""
        accounts = [
            {"account_id": a["account_id"], "proxy": mask_proxy(a.get("proxy"))}
            for a in ctx.config_loader.get_accounts_config()
            if isinstance(a, dict) and a.get("account_id")
        ]
        fallback = ctx.config_loader.get_setting("browser_settings.proxy")
        return ok_(
            pools=_pools_snapshot(ctx),
            strategy=ctx.config_loader.get_setting(
                "browser_settings.proxy_pool_strategy", "hash"),
            accounts=accounts,
            global_proxy=mask_proxy(fallback) if fallback else None,
        )

    @server.tool(annotations=LOCAL_WRITE_IDEMPOTENT)
    @guard
    async def add_proxy(pool: str, proxy_url: str) -> Dict[str, Any]:
        """Add a proxy URL to a named pool (created if new) in settings.json.
        Validated, deduped, backed up, atomic. Credentials are masked in the
        response. Assign it to accounts via update_account(proxy="pool:<name>")
        or a direct URL."""
        if not pool or not pool.strip():
            raise ToolError("pool must not be empty.")
        pool = pool.strip()
        url = validate_proxy_url(proxy_url)

        def _fn(pools: Dict[str, Any]) -> None:
            members = pools.setdefault(pool, [])
            if url in members:
                raise ToolError(f"Proxy already present in pool '{pool}'.")
            members.append(url)

        _mutate_pools(ctx, _fn)
        return ok_(pool=pool, pools=_pools_snapshot(ctx),
                   message=f"Added to pool '{pool}'. Assign with "
                           f"update_account(account, proxy=\"pool:{pool}\").")

    @server.tool(annotations=LOCAL_DESTRUCTIVE)
    @guard
    async def remove_proxy(pool: str, proxy_url: str, confirm: bool = False) -> Dict[str, Any]:
        """Remove a proxy URL from a pool. Requires confirm=true. The response
        lists accounts still assigned pool:<name>. Emptying a referenced pool
        is allowed but reported (those accounts fail closed until reassigned)."""
        if not confirm:
            raise ToolError("remove_proxy requires confirm=true.")
        url = (proxy_url or "").strip()

        def _fn(pools: Dict[str, Any]) -> None:
            members = pools.get(pool)
            if not members or url not in members:
                raise ToolError(f"Proxy not found in pool '{pool}'.")
            members.remove(url)

        _mutate_pools(ctx, _fn)
        affected = [
            a["account_id"]
            for a in ctx.config_loader.get_accounts_config()
            if isinstance(a, dict) and a.get("proxy") == f"pool:{pool}"
        ]
        return ok_(pool=pool, removed=True, pools=_pools_snapshot(ctx),
                   affected_accounts=affected)

    @server.tool(annotations=READ_ONLY_FROM_X)
    @guard
    async def test_proxy(account: Optional[str] = None,
                         proxy_url: Optional[str] = None) -> Dict[str, Any]:
        """Probe the effective proxy in an isolated, cookieless browser using
        the configured backend; visits only an IP-echo endpoint, never X.
        Explicit proxy_url wins; account assignments inherit the global proxy.
        Returns validated egress IP, startup/request latency and masked route.
        Async HTTP(S) authentication is supported; legacy Selenium rejects
        authenticated proxies. Legacy round_robin probes advance its cursor.
        """
        backend = getattr(ctx.session_pool, "backend", "selenium")
        if proxy_url is not None:
            raw = {"proxy": validate_proxy_url(proxy_url), "account_id": "__proxy_probe__"}
        elif account:
            _, raw, _ = ex.resolve_account(ctx, account)
        else:
            raise ToolError("Pass proxy_url or account.")
        try:
            if backend in ("patchright", "playwright"):
                async_proxy = resolve_account_proxy(ctx.config_loader, raw)
                if async_proxy is None:
                    raise ToolError("No effective proxy configured; a proxy probe will not use a direct connection.")
                # Server is already separated from userinfo by the strict async
                # resolver; never interpolate its username/password into output.
                display = async_proxy["server"]
                if "username" in async_proxy or "password" in async_proxy:
                    scheme, _, authority = display.partition("://")
                    display = scheme + "://***@" + authority
                probe = await _probe_async_proxy(ctx, backend, async_proxy)
                return ok_(proxy=display, browser_backend=backend, authenticated_session_used=False, **probe)
            if backend != "selenium":
                raise ToolError("Proxy probe is unsupported for the configured browser backend.")
            resolved = ProxyManager(ctx.config_loader).resolve(raw.get("proxy"), raw.get("account_id"))
            if resolved is None:
                raise ToolError("No effective proxy configured; a proxy probe will not use a direct connection.")
        except SessionError as error:
            if error.reason != "invalid_proxy":
                raise ToolError("Proxy probe browser startup or cleanup failed; check the browser installation and restart if necessary.") from None
            raise ToolError("The effective proxy configuration is invalid or unsupported for this backend.") from None
        except ValueError:
            raise ToolError("The effective proxy configuration is invalid or unsupported for this backend.") from None
        except ToolError:
            raise
        except Exception:
            raise ToolError("Proxy probe failed; check connectivity and browser installation. Credentials and driver details were omitted.") from None

        try:
            probe = await _probe_legacy_proxy(ctx, resolved)
        except ToolError:
            raise
        except Exception:
            raise ToolError("Proxy probe failed; check connectivity and browser settings. Legacy Selenium does not support authenticated proxies.") from None
        return ok_(proxy=mask_proxy(resolved), browser_backend=backend,
                   authenticated_session_used=False, **probe)
