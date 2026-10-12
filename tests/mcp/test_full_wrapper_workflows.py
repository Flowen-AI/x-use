"""Previously missing successful MCP entry paths, with isolated service fakes."""
import asyncio

import pytest

from helpers import call_tool, accounts, browser_factory, config_loader, draft_store, drafts_path, mcp_server, mcp_settings, queue_store, session_pool  # noqa: F401
from test_playwright_integration import async_server  # noqa: F401
from xuse.models import ScrapedTweet

pytestmark = pytest.mark.asyncio


async def test_profile_feed_and_session_wrappers(async_server):
    ctx = async_server.xuse_ctx

    async def profile(handle):
        assert handle == 'alice'
        return {'handle': handle, 'display_name': 'Alice'}

    async def feed(limit):
        assert limit == 2
        return [ScrapedTweet(tweet_id='123', tweet_url='https://x.com/alice/status/123', text_content='hello')]
    ctx.session_pool.browser.get_profile = profile
    ctx.session_pool.browser.get_home_feed = feed
    assert (await call_tool(async_server, 'get_profile', {'account': 'acc1', 'profile': '@alice'}))['handle'] == 'alice'
    assert len((await call_tool(async_server, 'get_home_feed', {'account': 'acc1', 'limit': 2}))['tweets']) == 1
    status = await call_tool(async_server, 'get_session_status', {'account': 'acc1'})
    assert status['sessions'][0]['warm']
    assert (await call_tool(async_server, 'get_account_safety', {'account': 'acc1'}))['ok']
    assert (await call_tool(async_server, 'close_session', {'account': 'acc1'}))['closed']
    assert ctx.session_pool.closed


async def test_generated_draft_and_explicit_reply_survive_review(mcp_server, monkeypatch):
    from xuse.mcp import actions

    async def generate(ctx, account, topic):
        assert account == 'acc1' and topic == 'testing'
        return 'A generated draft for review.'
    monkeypatch.setattr(actions, 'generate_post_text', generate)
    for name, args, expected in [
        ('generate_and_post', {'topic': 'testing'}, 'A generated draft for review.'),
        ('reply_to_tweet', {'tweet_url': 'https://x.com/alice/status/123', 'text': 'Reviewed reply'}, 'Reviewed reply'),
    ]:
        draft = await call_tool(mcp_server, name, {'account': 'acc1', **args})
        assert draft['ok'] and draft['payload']['text'] == expected
        stored = await call_tool(mcp_server, 'get_draft', {'draft_id': draft['draft_id']})
        assert stored['ok']
        assert (await call_tool(mcp_server, 'reject_draft', {'draft_id': draft['draft_id']}))['ok']


async def test_legacy_cycle_wrapper_tracks_completion_without_mutating_configuration(mcp_server, monkeypatch):
    from xuse.mcp import write_tools
    calls = []
    before = mcp_server.xuse_ctx.config_loader.get_accounts_config()

    class Orchestrator:
        async def _process_account(self, raw):
            calls.append(raw)
    monkeypatch.setattr(write_tools, 'TwitterOrchestrator', Orchestrator)
    result = await call_tool(mcp_server, 'run_cycle', {'account': 'acc1', 'pipelines': 'likes'})
    assert result['ok']
    await mcp_server.xuse_ctx.runs[result['run_id']]['task']
    assert len(calls) == 1 and calls[0]['account_id'] == 'acc1'
    assert mcp_server.xuse_ctx.runs[result['run_id']]['status'] == 'finished'
    assert mcp_server.xuse_ctx.config_loader.get_accounts_config() == before
