"""Regressions discovered by the October end-to-end audit (no real X)."""
import pytest

from xuse.models import MediaItem, ScrapedTweet
from helpers import (accounts, browser_factory, call_tool, config_loader, draft_store,
                     drafts_path, mcp_server, mcp_settings, queue_store, session_pool)


@pytest.mark.asyncio
@pytest.mark.parametrize('tool', ['get_tweet', 'prepare_reply'])
async def test_media_only_post_is_valid_context(mcp_server, monkeypatch, tool):
    tweet = ScrapedTweet(tweet_id='123', text_content='', user_handle='author',
                         media=[MediaItem(type='image', url='https://pbs.twimg.com/media/a.jpg')])
    async def fetch(*args, **kwargs):
        return [tweet]
    monkeypatch.setattr('xuse.mcp.tools.uses_playwright', lambda ctx: True)
    monkeypatch.setattr('xuse.mcp.tools.browser_call', fetch)
    result = await call_tool(mcp_server, tool, {
        'account':'acc1', 'tweet_url':'https://x.com/author/status/123', 'include_images':False})
    assert result['ok'] is True
    assert result['text_content'] == ''
    assert len(result['media']) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('cookies', [
    [{'name':'unrelated', 'value':'synthetic'}],
    [{'name':'auth_token','value':'synthetic','expires':1}, {'name':'ct0','value':'synthetic'}],
])
async def test_health_validates_inline_cookies(mcp_server, cookies, monkeypatch):
    raw = mcp_server.xuse_ctx.session_pool.find_account_dict('acc1')
    raw['cookies'] = cookies
    monkeypatch.setattr(mcp_server.xuse_ctx.session_pool, 'find_account_dict', lambda account: raw)
    result = await call_tool(mcp_server, 'get_account_health', {'account':'acc1'})
    assert result['ok'] is True
    assert result['cookies']['valid'] is False
    assert result['cookies']['problems']


@pytest.mark.asyncio
async def test_unknown_public_counts_remain_unknown(mcp_server, monkeypatch):
    async def fetch(*args, **kwargs):
        return ScrapedTweet(tweet_id='123', text_content='text', like_count=None,
                            reply_count=None, retweet_count=None, view_count=None)
    monkeypatch.setattr('xuse.mcp.tools.scrape_single_tweet', fetch)
    result = await call_tool(mcp_server, 'get_tweet', {
        'account':'acc1', 'tweet_url':'https://x.com/author/status/123', 'include_images':False})
    for key in ('like_count','reply_count','retweet_count','view_count'):
        assert result[key] is None


@pytest.mark.asyncio
async def test_runtime_info_is_local_and_contains_no_account_data(mcp_server, browser_factory):
    result = await call_tool(mcp_server, 'get_runtime_info')
    assert result['ok'] is True
    assert result['tool_count'] == len(await mcp_server.list_tools())
    assert result['draft_mode'] is True
    assert result['auto_drain_enabled'] is False
    assert result['live_authentication_checked'] is False
    assert 'acc1' not in str(result)
    assert browser_factory.created == []


@pytest.mark.asyncio
@pytest.mark.parametrize('backend', ['patchright', 'playwright', 'selenium'])
async def test_runtime_info_reports_backend_constraints_without_starting_browser(
        mcp_server, browser_factory, monkeypatch, tmp_path, backend):
    from xuse.mcp.safety import SafetyStore
    monkeypatch.setattr(mcp_server.xuse_ctx.session_pool, 'backend', backend, raising=False)
    monkeypatch.setenv('XUSE_ENV', 'development')
    monkeypatch.setenv('XUSE_DEV_UNLOCK_DAILY_CAP', '20')
    policy = SafetyStore(tmp_path / 'diagnostics.sqlite3')
    mcp_server.xuse_ctx.safety_store = policy
    # Runtime diagnostics must never inspect an account's ledger or cookies.
    monkeypatch.setattr(policy, 'status', lambda *args: pytest.fail('account state inspected'))
    result = await call_tool(mcp_server, 'get_runtime_info')
    assert result['ok'] is True
    support = result['execution_support']
    assert support['basis'] == 'configured_backend'
    assert support['async_browser_tools'] is (backend != 'selenium')
    assert support['legacy_run_cycle'] is (backend == 'selenium')
    assert support['durable_browser_safety'] is (backend != 'selenium')
    assert support['resumable_thread_execution'] is (backend != 'selenium')
    assert support['live_ui_verified'] is False
    if backend == 'selenium':
        assert result['unlock_policy'] is None
    else:
        assert result['unlock_policy'] == {
            'daily_cap': 20, 'development_override': 20,
            'budget_timezone': 'UTC', 'remaining_requires': 'get_account_safety'}
    assert 'acc1' not in str(result)
    assert browser_factory.created == []


@pytest.mark.asyncio
async def test_runtime_info_does_not_claim_durable_threads_without_store(mcp_server, monkeypatch):
    monkeypatch.setattr(mcp_server.xuse_ctx.session_pool, 'backend', 'patchright', raising=False)
    mcp_server.xuse_ctx.safety_store = None
    result = await call_tool(mcp_server, 'get_runtime_info')
    assert result['execution_support']['durable_browser_safety'] is False
    assert result['execution_support']['resumable_thread_execution'] is False
    assert result['unlock_policy'] is None
