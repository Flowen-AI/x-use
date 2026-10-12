"""Backend-correct anonymous proxy probes using synthetic browser doubles."""
import asyncio
import json
import threading
from types import SimpleNamespace

import pytest

from xuse.mcp import proxy_tools
from helpers import (accounts, browser_factory, call_tool, config_loader, draft_store,
                     drafts_path, mcp_server, mcp_settings, queue_store, session_pool)


@pytest.fixture
def async_probe(mcp_server, monkeypatch):
    events = {}
    pool = None

    class Page:
        def set_default_timeout(self, value):
            events['timeout'] = value

        async def goto(self, url, **kwargs):
            events['url'] = url
            if events.get('fail') == 'goto':
                raise RuntimeError('private-user private-secret')
            if events.get('cancel'):
                events['started'].set()
                await asyncio.Future()
            return SimpleNamespace(status=events.get('status', 200))

        def locator(self, selector):
            assert selector == 'body'
            return self

        async def inner_text(self):
            return events.get('body', '{"ip":"2001:db8::7"}')

    class Context:
        async def route(self, glob, handler):
            events['route_handler'] = handler

        async def new_page(self):
            if events.get('fail') == 'page':
                raise RuntimeError('private-user private-secret')
            return Page()

    class Browser:
        async def new_context(self, **kwargs):
            events['context_options'] = kwargs
            if events.get('fail') == 'context':
                raise RuntimeError('private-user private-secret')
            return Context()

    class Pool:
        _process_guard = None
        idle_timeout_seconds = 1

        def __init__(self, loader):
            nonlocal pool
            pool = self
            events['loader'] = loader
            events['created'] = events.get('created', 0) + 1

        async def _get_browser(self):
            events['probe_ttl'] = self.idle_timeout_seconds
            if events.get('fail') == 'startup':
                raise RuntimeError('private-user private-secret')
            return Browser()

        async def _refresh_owned_processes(self):
            events['refreshed'] = True

        async def close_all(self):
            events['closed'] = True
            if events.get('fail') == 'cleanup':
                raise RuntimeError('private-user private-secret')
            if events.get('uncertain_cleanup'):
                self._process_guard = object()

    monkeypatch.setattr(proxy_tools, 'PatchrightSessionPool', Pool)
    monkeypatch.setattr(proxy_tools, 'PlaywrightSessionPool', Pool)
    monkeypatch.setattr(mcp_server.xuse_ctx.session_pool, 'backend', 'patchright', raising=False)
    def forbidden(*args, **kwargs):
        pytest.fail('An async probe touched legacy browser or account credentials')
    monkeypatch.setattr(proxy_tools, 'BrowserManager', forbidden)
    monkeypatch.setattr('xuse.browser.cookies.load_account_cookies', forbidden)
    return events


@pytest.mark.asyncio
@pytest.mark.parametrize('backend', ['patchright', 'playwright'])
async def test_async_proxy_probe_uses_real_backend_without_account_auth(mcp_server, async_probe, monkeypatch, backend):
    monkeypatch.setattr(mcp_server.xuse_ctx.session_pool, 'backend', backend)
    result = await call_tool(mcp_server, 'test_proxy', {
        'proxy_url': 'http://private-user:private-secret@proxy.invalid:8080'})
    assert result['ok'] and result['browser_backend'] == backend
    assert result['egress_ip'] == '2001:db8::7'
    assert result['authenticated_session_used'] is False
    assert result['proxy'] == 'http://***@proxy.invalid:8080'
    assert 'private-user' not in json.dumps(result) and 'private-secret' not in json.dumps(result)
    assert async_probe['loader'] is mcp_server.xuse_ctx.config_loader
    assert async_probe['context_options'] == {
        'proxy': {'server': 'http://proxy.invalid:8080', 'username': 'private-user', 'password': 'private-secret'},
        'service_workers': 'block', 'accept_downloads': False}
    assert async_probe['url'] == proxy_tools.IP_ECHO_URL
    assert async_probe['probe_ttl'] >= 120
    assert async_probe['closed'] and async_probe['refreshed']


@pytest.mark.asyncio
async def test_account_probe_inherits_effective_global_proxy(mcp_server, config_loader, async_probe):
    config_loader.accounts[0].pop('proxy', None)
    config_loader.settings['browser_settings'] = {'proxy': 'http://global.invalid:80'}
    result = await call_tool(mcp_server, 'test_proxy', {'account': 'acc1'})
    assert result['ok'] and result['proxy'] == 'http://global.invalid:80'


@pytest.mark.asyncio
async def test_missing_effective_proxy_refuses_direct_browser(mcp_server, config_loader, async_probe):
    config_loader.accounts[0].pop('proxy', None)
    config_loader.settings['browser_settings'] = {}
    result = await call_tool(mcp_server, 'test_proxy', {'account': 'acc1'})
    assert not result['ok'] and 'direct connection' in json.dumps(result['error'])
    assert 'loader' not in async_probe


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['startup', 'context', 'page', 'goto', 'cleanup'])
async def test_every_async_probe_failure_closes_owned_runtime_and_redacts_driver_errors(mcp_server, async_probe, failure):
    async_probe['fail'] = failure
    result = await call_tool(mcp_server, 'test_proxy', {'proxy_url': 'http://proxy.invalid:80'})
    assert not result['ok'] and async_probe['closed']
    assert 'private-user' not in json.dumps(result) and 'private-secret' not in json.dumps(result)
    async_probe['fail'] = None
    assert await proxy_tools.close_proxy_probe(mcp_server.xuse_ctx)


@pytest.mark.asyncio
@pytest.mark.parametrize('body', [
    pytest.param('{"ip":"private-secret"}', id='arbitrary-text'),
    pytest.param('{"ip":"fe80::1%private-secret"}', id='scoped-ipv6'),
    pytest.param('{"ip":"999.999.999.999"}', id='invalid-ipv4'),
    pytest.param('error', id='not-json'),
    pytest.param('x' * 100_001, id='oversized'),
])
async def test_echo_endpoint_cannot_return_arbitrary_text_as_ip(mcp_server, async_probe, body):
    async_probe['body'] = body
    result = await call_tool(mcp_server, 'test_proxy', {'proxy_url': 'http://proxy.invalid:80'})
    assert not result['ok'] and async_probe['closed']
    assert 'private-secret' not in json.dumps(result)


@pytest.mark.asyncio
async def test_async_probe_blocks_redirects_and_subresources(mcp_server, async_probe):
    result = await call_tool(mcp_server, 'test_proxy', {'proxy_url': 'http://proxy.invalid:80'})
    assert result['ok']
    events = []
    class Route:
        async def continue_(self):
            events.append('allow')
        async def abort(self):
            events.append('block')
    for url in (proxy_tools.IP_ECHO_URL, 'https://x.com/home', 'https://elsewhere.invalid/'):
        route = Route()
        route.request = SimpleNamespace(url=url)
        await async_probe['route_handler'](route)
    assert events == ['allow', 'block', 'block']


@pytest.mark.asyncio
async def test_async_probe_cancellation_still_closes_runtime(mcp_server, async_probe):
    async_probe.update(cancel=True, started=asyncio.Event())
    task = asyncio.create_task(proxy_tools._probe_async_proxy(
        mcp_server.xuse_ctx, 'patchright', {'server': 'http://proxy.invalid:80'}))
    await asyncio.wait_for(async_probe['started'].wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert async_probe['closed']


@pytest.mark.asyncio
async def test_shutdown_waits_for_active_probe_before_closing_retained_pool(mcp_server, async_probe):
    async_probe.update(cancel=True, started=asyncio.Event())
    probe = asyncio.create_task(proxy_tools._probe_async_proxy(
        mcp_server.xuse_ctx, 'patchright', {'server': 'http://proxy.invalid:80'}))
    await asyncio.wait_for(async_probe['started'].wait(), 1)
    cleanup = asyncio.create_task(proxy_tools.close_proxy_probe(mcp_server.xuse_ctx))
    await asyncio.sleep(0)
    assert not cleanup.done() and not async_probe.get('closed')
    probe.cancel()
    with pytest.raises(asyncio.CancelledError):
        await probe
    assert await asyncio.wait_for(cleanup, 1)
    assert async_probe['closed'] and mcp_server.xuse_ctx._proxy_probe_pool is None


@pytest.mark.asyncio
async def test_uncertain_cleanup_is_not_reported_as_success(mcp_server, async_probe):
    async_probe['uncertain_cleanup'] = True
    result = await call_tool(mcp_server, 'test_proxy', {'proxy_url': 'http://proxy.invalid:80'})
    assert not result['ok'] and 'could not confirm browser exit' in json.dumps(result['error'])
    retained = mcp_server.xuse_ctx._proxy_probe_pool
    assert retained is not None
    second = await call_tool(mcp_server, 'test_proxy', {'proxy_url': 'http://proxy.invalid:80'})
    assert not second['ok'] and async_probe['created'] == 1
    # Server shutdown can retry exactly this retained owner; no foreign targets.
    async_probe['uncertain_cleanup'] = False
    retained._process_guard = None
    assert await proxy_tools.close_proxy_probe(mcp_server.xuse_ctx)
    assert mcp_server.xuse_ctx._proxy_probe_pool is None


@pytest.mark.asyncio
async def test_cleanup_exception_retains_probe_owner_and_blocks_next_launch(mcp_server, async_probe):
    async_probe['fail'] = 'cleanup'
    first = await call_tool(mcp_server, 'test_proxy', {'proxy_url': 'http://proxy.invalid:80'})
    assert not first['ok'] and mcp_server.xuse_ctx._proxy_probe_pool is not None
    second = await call_tool(mcp_server, 'test_proxy', {'proxy_url': 'http://proxy.invalid:80'})
    assert not second['ok'] and async_probe['created'] == 1
    async_probe['fail'] = None
    assert await proxy_tools.close_proxy_probe(mcp_server.xuse_ctx)


@pytest.mark.asyncio
async def test_http_error_never_reports_a_successful_proxy(mcp_server, async_probe):
    async_probe['status'] = 407
    result = await call_tool(mcp_server, 'test_proxy', {'proxy_url': 'http://proxy.invalid:80'})
    assert not result['ok'] and async_probe['closed']


@pytest.fixture
def legacy_probe(mcp_server, monkeypatch):
    events = {'started': threading.Event(), 'release': threading.Event()}

    class Driver:
        page_source = '{"ip":"203.0.113.7"}'

        def set_page_load_timeout(self, seconds):
            assert seconds == 25

        def get(self, url):
            events['url'] = url
            events['started'].set()
            if events.get('block'):
                assert events['release'].wait(5)
            if events.get('navigation_fail'):
                raise RuntimeError('private-user private-secret')

    class Manager:
        driver = None

        def __init__(self, account_config, config_loader):
            assert 'cookies' not in account_config and 'cookie_file_path' not in account_config
            assert config_loader is mcp_server.xuse_ctx.config_loader
            events['created'] = events.get('created', 0) + 1

        def get_driver(self, *, authenticate):
            assert authenticate is False
            self.driver = Driver()
            return self.driver

        def close_driver(self, *, strict):
            assert strict is True
            events['quit_attempts'] = events.get('quit_attempts', 0) + 1
            if events.get('quit_fail'):
                raise RuntimeError('private-user private-secret')
            self.driver = None

    monkeypatch.setattr(proxy_tools, 'BrowserManager', Manager)
    monkeypatch.setattr(mcp_server.xuse_ctx.session_pool, 'backend', 'selenium', raising=False)
    yield events
    events['release'].set()


@pytest.mark.asyncio
@pytest.mark.parametrize('navigation_fail', [False, True])
async def test_legacy_probe_closes_anonymous_browser_on_success_and_request_failure(mcp_server, legacy_probe, navigation_fail):
    legacy_probe['navigation_fail'] = navigation_fail
    result = await call_tool(mcp_server, 'test_proxy', {'proxy_url': 'http://proxy.invalid:80'})
    assert result['ok'] is not navigation_fail
    assert legacy_probe['url'] == proxy_tools.IP_ECHO_URL
    assert legacy_probe['quit_attempts'] == 1
    assert not proxy_tools._has_retained_probe(mcp_server.xuse_ctx)
    assert 'private-secret' not in json.dumps(result)


@pytest.mark.asyncio
async def test_legacy_quit_failure_retains_owner_and_blocks_another_probe(mcp_server, legacy_probe):
    legacy_probe['quit_fail'] = True
    first = await call_tool(mcp_server, 'test_proxy', {'proxy_url': 'http://proxy.invalid:80'})
    assert not first['ok'] and mcp_server.xuse_ctx._proxy_probe_manager.driver is not None
    second = await call_tool(mcp_server, 'test_proxy', {'proxy_url': 'http://proxy.invalid:80'})
    assert not second['ok'] and legacy_probe['created'] == 1
    assert 'private-secret' not in json.dumps(first)
    legacy_probe['quit_fail'] = False
    assert await proxy_tools.close_proxy_probe(mcp_server.xuse_ctx)
    assert not proxy_tools._has_retained_probe(mcp_server.xuse_ctx)
    assert legacy_probe['quit_attempts'] == 2


@pytest.mark.asyncio
@pytest.mark.parametrize('interrupt', ['cancel', 'timeout'])
async def test_legacy_interrupted_worker_stays_owned_until_it_finishes(mcp_server, legacy_probe, monkeypatch, interrupt):
    legacy_probe['block'] = True
    if interrupt == 'timeout':
        monkeypatch.setattr(proxy_tools, 'PROXY_PROBE_TIMEOUT_SECONDS', 0.05)
    task = asyncio.create_task(proxy_tools._probe_legacy_proxy(mcp_server.xuse_ctx, 'http://proxy.invalid:80'))
    try:
        assert await asyncio.to_thread(legacy_probe['started'].wait, 1)
        if interrupt == 'cancel':
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            with pytest.raises(proxy_tools.ToolError, match='timed out'):
                await task
        worker = mcp_server.xuse_ctx._proxy_probe_task
        assert not worker.done()
        assert not await proxy_tools.close_proxy_probe(mcp_server.xuse_ctx)
        assert not legacy_probe.get('quit_attempts')
        second = await call_tool(mcp_server, 'test_proxy', {'proxy_url': 'http://proxy.invalid:80'})
        assert not second['ok'] and legacy_probe['created'] == 1
    finally:
        legacy_probe['release'].set()
        worker = mcp_server.xuse_ctx._proxy_probe_task
        if worker is not None:
            await asyncio.wait_for(asyncio.shield(worker), 2)
        assert await proxy_tools.close_proxy_probe(mcp_server.xuse_ctx)
    assert legacy_probe['quit_attempts'] == 1
    assert not proxy_tools._has_retained_probe(mcp_server.xuse_ctx)
