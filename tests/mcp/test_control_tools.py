"""Review gates and the durable dispatch path for account/inbox controls."""
import pytest

from helpers import call_tool
from test_playwright_integration import async_server  # noqa: F401
from xuse.browser.errors import BrowserActionError

CASES = [
    ('bookmark_tweet', {'tweet_url': 'https://x.com/alice/status/123'}, 'bookmark'),
    ('remove_bookmark', {'tweet_url': 'https://x.com/alice/status/123'}, 'bookmark'),
    ('unlike_tweet', {'tweet_url': 'https://x.com/alice/status/123'}, 'like'),
    ('undo_retweet', {'tweet_url': 'https://x.com/alice/status/123'}, 'retweet'),
    ('unfollow_profile', {'profile': '@alice'}, 'follow'),
    ('edit_tweet', {'tweet_url': 'https://x.com/alice/status/123', 'expected_text': 'before', 'text': 'after'}, 'post'),
    ('delete_tweet', {'tweet_url': 'https://x.com/alice/status/123', 'expected_text': ''}, 'post'),
    *[('manage_conversation', {'conversation_id': 'https://x.com/messages/10-20', 'operation': op}, 'inbox')
      for op in ('accept_request', 'mute', 'unmute', 'delete')],
]
pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize('name,args,kind', CASES)
async def test_review_dispatch_and_duplicate_approval(async_server, name, args, kind):
    ctx = async_server.xuse_ctx
    ctx.draft_mode = False
    calls = []

    async def perform(*values):
        ctx.session_pool.browser.action_validator()
        calls.append(values)
        return {'success': True, 'evidence': {'observed': 'fixture_only'}}
    setattr(ctx.session_pool.browser, name, perform)
    draft = await call_tool(async_server, name, {'account': 'acc1', **args})
    assert draft['ok'] and draft['status'] == 'pending'
    assert not calls and ctx.session_pool.started == 0
    result = await call_tool(async_server, 'approve_draft', {'draft_id': draft['draft_id']})
    assert result['ok'] and result['status'] == 'executed'
    assert len(calls) == 1
    assert ctx.safety_store.status('acc1')['daily_used'][kind] == 1
    duplicate = await call_tool(async_server, 'approve_draft', {'draft_id': draft['draft_id']})
    assert not duplicate['ok'] and len(calls) == 1


@pytest.mark.parametrize('name,args,kind', CASES)
async def test_uncertain_control_cannot_be_retried(async_server, name, args, kind):
    ctx = async_server.xuse_ctx
    calls = []

    async def perform(*values):
        calls.append(values)
        raise BrowserActionError('outcome_unknown')
    setattr(ctx.session_pool.browser, name, perform)
    draft = await call_tool(async_server, name, {'account': 'acc1', **args})
    result = await call_tool(async_server, 'approve_draft', {'draft_id': draft['draft_id']})
    assert not result['ok'] and result['error']['action_id']
    assert ctx.draft_store.get(draft['draft_id']).status == 'uncertain'
    assert not (await call_tool(async_server, 'approve_draft', {'draft_id': draft['draft_id']}))['ok']
    assert len(calls) == 1


@pytest.mark.parametrize('name,args,kind', CASES)
async def test_paused_account_does_not_execute_control(async_server, name, args, kind):
    ctx = async_server.xuse_ctx
    draft = await call_tool(async_server, name, {'account': 'acc1', **args})
    ctx.safety_store.pause('acc1', 'manual_pause')
    result = await call_tool(async_server, 'approve_draft', {'draft_id': draft['draft_id']})
    assert not result['ok'] and ctx.session_pool.started == 0
    assert ctx.draft_store.get(draft['draft_id']).status == 'pending'


@pytest.mark.parametrize('name,args', [
    ('bookmark_tweet', {'tweet_url': 'https://evil.example/alice/status/123'}),
    ('remove_bookmark', {'tweet_url': 'https://x.com/home'}),
    ('unlike_tweet', {'tweet_url': 'https://x.com/alice/status/123/extra'}),
    ('undo_retweet', {'tweet_url': 'http://x.com/alice/status/123'}),
    ('unfollow_profile', {'profile': '../settings'}),
    ('edit_tweet', {'tweet_url': 'https://x.com/alice/status/123', 'expected_text': 'before', 'text': ' '}),
    ('delete_tweet', {'tweet_url': 'https://x.com/alice/status/123', 'expected_text': '\x00'}),
    ('manage_conversation', {'conversation_id': 'https://evil.example/messages/10-20', 'operation': 'delete'}),
    ('get_conversation_controls', {'conversation_id': '../settings'}),
    ('get_bookmarks', {'limit': 0}),
])
async def test_invalid_control_input_never_starts_browser(async_server, name, args):
    result = await call_tool(async_server, name, {'account': 'acc1', **args})
    assert not result['ok'] and async_server.xuse_ctx.session_pool.started == 0


async def test_new_read_controls(async_server):
    browser = async_server.xuse_ctx.session_pool.browser

    async def bookmarks(limit):
        return []

    async def controls(conversation_id):
        return {'conversation_id': '10-20', 'actions': ['mute'], 'partial': True}
    browser.get_bookmarks = bookmarks
    browser.get_conversation_controls = controls
    assert (await call_tool(async_server, 'get_bookmarks', {'account': 'acc1'}))['tweets'] == []
    assert (await call_tool(async_server, 'get_conversation_controls', {'account': 'acc1', 'conversation_id': '10-20'}))['actions'] == ['mute']


@pytest.mark.parametrize('reference', ['10-20', 'https://x.com/i/chat/10-20'])
async def test_conversation_draft_preserves_native_reference(async_server, reference):
    calls = []
    async def perform(conversation_id, operation):
        calls.append((conversation_id, operation))
        return {'success': True}
    async_server.xuse_ctx.session_pool.browser.manage_conversation = perform
    draft = await call_tool(async_server, 'manage_conversation', {'account': 'acc1', 'conversation_id': reference, 'operation': 'mute'})
    assert async_server.xuse_ctx.draft_store.get(draft['draft_id']).payload['conversation_id'] == reference
    assert (await call_tool(async_server, 'approve_draft', {'draft_id': draft['draft_id']}))['ok']
    assert calls == [(reference, 'mute')]
