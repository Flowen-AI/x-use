"""Reviewed publishing, relationship and inbox controls. No direct-mode bypass."""
from typing import Any, Literal

from . import executor as ex
from .annotations import LOCAL_WRITE, READ_ONLY_FROM_X
from .browser_bridge import browser_call
from .browser_tools import _require_browser


CONTROL_ACTIONS = frozenset({'bookmark_tweet', 'remove_bookmark', 'unlike_tweet',
    'undo_retweet', 'unfollow_profile', 'edit_tweet', 'delete_tweet', 'manage_conversation'})


def post_url(value):
    from xuse.browser.page import status_id, validate_x_url
    url = validate_x_url(value)
    if not status_id(url):
        raise ValueError('tweet_url must identify an exact X post.')
    return url


def review_text(value, *, empty=True):
    if not isinstance(value, str) or len(value) > 25000 or '\x00' in value or (not empty and not value.strip()):
        raise ValueError('Post text must be at most 25000 characters; replacement text must not be empty.')
    return value


async def execute_control_draft(ctx, draft):
    _require_browser(ctx)
    payload = draft.payload
    action = draft.action
    kind = {'unfollow_profile': 'follow', 'unlike_tweet': 'like', 'undo_retweet': 'retweet',
            'edit_tweet': 'post', 'delete_tweet': 'post', 'manage_conversation': 'inbox'}.get(action, 'bookmark')
    if action == 'unfollow_profile':
        args = [ex.profile_handle_from(payload['profile'])]
    elif action == 'manage_conversation':
        args = [payload['conversation_id'], payload['operation']]
    else:
        args = [post_url(payload['tweet_url'])]
        if action in ('edit_tweet', 'delete_tweet'):
            args.append(review_text(payload['expected_text']))
        if action == 'edit_tweet':
            args.append(review_text(payload['text'], empty=False))
    return await browser_call(ctx, draft.account, action, *args, kind=kind,
                              action_context={'draft_id': draft.draft_id})


def register_control_tools(server, ctx):
    from .tools import draft_response, dump_tweet, guard, ok_

    def prepare(account, action, payload, preview):
        _require_browser(ctx)
        account, raw, _ = ex.resolve_account(ctx, account)
        ex.require_active(raw, account)
        return draft_response(ctx.draft_store.create(account, action, payload, preview))

    @server.tool(annotations=READ_ONLY_FROM_X)
    @guard
    async def get_bookmarks(account: str, limit: int = 20) -> dict[str, Any]:
        """Read bounded posts in the default bookmarks feed. Partial results; bookmark folders are not traversed."""
        from xuse.browser.page import _limit
        _require_browser(ctx)
        _limit(limit)
        rows = await browser_call(ctx, account, 'get_bookmarks', limit)
        return ok_(account=account, tweets=[dump_tweet(t) for t in rows], partial=True)

    @server.tool(annotations=LOCAL_WRITE)
    @guard
    async def bookmark_tweet(account: str, tweet_url: str) -> dict[str, Any]:
        """Prepare a bookmark draft for one exact post. Always requires approve_draft."""
        url = post_url(tweet_url)
        return prepare(account, 'bookmark_tweet', {'tweet_url': url}, f'Bookmark {url} from {account}')

    @server.tool(annotations=LOCAL_WRITE)
    @guard
    async def remove_bookmark(account: str, tweet_url: str) -> dict[str, Any]:
        """Prepare removal of one bookmark. Always requires approve_draft."""
        url = post_url(tweet_url)
        return prepare(account, 'remove_bookmark', {'tweet_url': url}, f'Remove bookmark {url} from {account}')

    @server.tool(annotations=LOCAL_WRITE)
    @guard
    async def unlike_tweet(account: str, tweet_url: str) -> dict[str, Any]:
        """Prepare removal of your like from one exact post. Always requires approve_draft."""
        url = post_url(tweet_url)
        return prepare(account, 'unlike_tweet', {'tweet_url': url}, f'Unlike {url} from {account}')

    @server.tool(annotations=LOCAL_WRITE)
    @guard
    async def undo_retweet(account: str, tweet_url: str) -> dict[str, Any]:
        """Prepare removal of your repost of one exact post. Always requires approve_draft."""
        url = post_url(tweet_url)
        return prepare(account, 'undo_retweet', {'tweet_url': url}, f'Undo repost {url} from {account}')

    @server.tool(annotations=LOCAL_WRITE)
    @guard
    async def unfollow_profile(account: str, profile: str) -> dict[str, Any]:
        """Prepare unfollowing one exact X profile. Always requires approve_draft."""
        handle = ex.profile_handle_from(profile)
        return prepare(account, 'unfollow_profile', {'profile': handle}, f'Unfollow @{handle} from {account}')

    @server.tool(annotations=LOCAL_WRITE)
    @guard
    async def edit_tweet(account: str, tweet_url: str, expected_text: str, text: str) -> dict[str, Any]:
        """Prepare a text edit of your own post. Supply its current exact text and replacement; configure self_handles. X must expose Edit post; eligibility/time limits apply. Always requires approval. Media replacement is unsupported."""
        url = post_url(tweet_url)
        review_text(expected_text)
        review_text(text, empty=False)
        return prepare(account, 'edit_tweet', {'tweet_url': url, 'expected_text': expected_text, 'text': text},
                       f'Edit {url} from {account}\nBefore: {expected_text}\nAfter: {text}')

    @server.tool(annotations=LOCAL_WRITE)
    @guard
    async def delete_tweet(account: str, tweet_url: str, expected_text: str) -> dict[str, Any]:
        """Prepare permanent deletion of your own exact post. Supply its current text (empty for media-only). Always requires approve_draft; cannot be undone."""
        url = post_url(tweet_url)
        review_text(expected_text)
        return prepare(account, 'delete_tweet', {'tweet_url': url, 'expected_text': expected_text},
                       f'Permanently delete {url} from {account}\nReviewed text: {expected_text}')

    @server.tool(annotations=READ_ONLY_FROM_X)
    @guard
    async def get_conversation_controls(account: str, conversation_id: str) -> dict[str, Any]:
        """Inspect visible request and conversation-menu controls. Opening may mark read; unsupported layouts return an error, never guessed capabilities."""
        from xuse.browser.messaging import conversation_url
        _require_browser(ctx)
        conversation_url(conversation_id)
        return ok_(account=account, **await browser_call(ctx, account, 'get_conversation_controls', conversation_id))

    @server.tool(annotations=LOCAL_WRITE)
    @guard
    async def manage_conversation(account: str, conversation_id: str,
                                  operation: Literal['accept_request', 'mute', 'unmute', 'delete']) -> dict[str, Any]:
        """Draft one inbox action: accept a request, mute/unmute notifications, or delete a conversation from your inbox. Delete cannot be undone locally and does not promise deletion for recipients. Always requires approval; only verified visible controls are supported."""
        from xuse.browser.messaging import conversation_url
        url, identifier = conversation_url(conversation_id)
        # Preserve a numeric ID so the browser can bind it to the observed
        # native chat route, without navigating through the legacy /messages UI.
        reference = identifier if conversation_id == identifier else url
        return prepare(account, 'manage_conversation', {'conversation_id': reference, 'operation': operation},
                       f'{operation} conversation {reference} from {account}')
