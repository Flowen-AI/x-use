"""New controls against intercepted native browser DOM; no live X writes."""
import html

import pytest

from xuse.browser.errors import BrowserActionError
from test_messaging_dom import app, native_browser, conversation as conversation_document  # noqa: F401

pytestmark = pytest.mark.asyncio(loop_scope='module')
URL = 'https://x.com/alice/status/123'
TOGGLES = [('bookmark_tweet', 'bookmark', 'removeBookmark'),
           ('remove_bookmark', 'removeBookmark', 'bookmark'),
           ('unlike_tweet', 'unlike', 'like'), ('undo_retweet', 'unretweet', 'retweet')]


def post(control='', script='', text='before', handle='alice', identifier='123'):
    return f'''<article data-testid="tweet">
      <a href="/{handle}/status/{identifier}"><time>Now</time></a>
      <div data-testid="User-Name"><span>@{handle}</span></div>
      <div data-testid="tweetText">{html.escape(text)}</div>{control}</article>{script}'''


@pytest.mark.parametrize('action,before,after', TOGGLES)
@pytest.mark.parametrize('state', ['change', 'already', 'duplicate', 'wrong_target', 'no_change', 'redirect'])
async def test_toggle_target_and_outcome(app, action, before, after, state):
    app.adapter.action_confirmation_timeout_ms = 200
    initial = after if state == 'already' else before
    change = f"this.dataset.testid='{after}'"
    if state == 'no_change':
        change = ''
    if state == 'redirect':
        change += ";history.replaceState({},'', '/bob/status/999')"
    button = f'<button data-testid="{initial}" onclick="document.documentElement.dataset.clicked=\'yes\';{change}">Control</button>'
    extra = ''
    if action == 'undo_retweet' and state not in ('already', 'duplicate'):
        button = f'<button data-testid="{initial}" onclick="document.querySelector(\'#confirm\').hidden=false">Repost</button>'
        change = f"document.querySelector('[data-testid={before}]').dataset.testid='{after}'"
        if state == 'no_change':
            change = ''
        if state == 'redirect':
            change += ";history.replaceState({},'', '/bob/status/999')"
        extra = f'<button hidden id="confirm" data-testid="unretweetConfirm" onclick="document.documentElement.dataset.clicked=\'yes\';{change}">Undo</button>'
    body = post(button * (2 if state == 'duplicate' else 1), identifier='999' if state == 'wrong_target' else '123') + extra
    app.document('/alice/status/123', body)
    if state in ('change', 'already'):
        result = await getattr(app.adapter, action)(URL)
        assert result['success'] and result['status'] == ('already_done' if state == 'already' else 'confirmed')
    else:
        with pytest.raises(BrowserActionError):
            await getattr(app.adapter, action)(URL)
        if state in ('duplicate', 'wrong_target'):
            assert await app.page.locator('html').get_attribute('data-clicked') is None


def owned_post(mode, *, text='before', label=None, changed=False):
    option = label or ('Delete' if mode == 'delete' else 'Edit post')
    composer = '<div contenteditable="true" data-testid="tweetTextarea_0">before</div><button data-testid="tweetButton" onclick="document.querySelector(\'[data-testid=tweetText]\').textContent=document.querySelector(\'[data-testid=tweetTextarea_0]\').innerText;document.querySelector(\'[role=dialog]\').hidden=true">Update</button>'
    deletion = '<h2>Delete post?</h2><button data-testid="confirmationSheetConfirm" onclick="document.querySelector(\'article\').remove();document.querySelector(\'#toast\').hidden=false">Delete</button>'
    control = '<button data-testid="caret" onclick="document.querySelector(\'[role=menu]\').hidden=false">More</button>'
    mutation = "document.querySelector('[data-testid=tweetText]').textContent='changed';" if changed else ''
    return post(control, text=text) + f'''<div role="menu" hidden><button role="menuitem" onclick="{mutation}document.querySelector('[role=dialog]').hidden=false;this.parentElement.hidden=true">{option}</button></div>
      <div role="dialog" hidden>{deletion if mode == 'delete' else composer}</div>
      <div id="toast" data-testid="toast" hidden>Your post was deleted.</div>'''


@pytest.mark.parametrize('action', ['delete_tweet', 'edit_tweet'])
@pytest.mark.parametrize('case', ['success', 'stale', 'foreign', 'missing_feature', 'changed_during_review', 'no_evidence'])
async def test_owned_post_reviews(app, action, case):
    app.adapter.account = {'self_handles': ['alice' if case != 'foreign' else 'bob']}
    app.adapter.action_confirmation_timeout_ms = 200
    body = owned_post('delete' if action == 'delete_tweet' else 'edit',
                      text='changed' if case == 'stale' else 'before',
                      label='Something else' if case == 'missing_feature' else None,
                      changed=case == 'changed_during_review')
    if case == 'no_evidence':
        body = body.replace('Your post was deleted.', 'Unrelated toast').replace("document.querySelector('[data-testid=tweetText]').textContent=document.querySelector('[data-testid=tweetTextarea_0]').innerText;", '')
    app.document('/alice/status/123', body)
    args = [URL, 'before'] + (['after'] if action == 'edit_tweet' else [])
    if case == 'success':
        assert (await getattr(app.adapter, action)(*args))['success']
    else:
        with pytest.raises(BrowserActionError):
            await getattr(app.adapter, action)(*args)


@pytest.mark.parametrize('case', ['success', 'already', 'wrong_dialog', 'duplicate', 'no_change'])
async def test_unfollow_profile(app, case):
    app.adapter.action_confirmation_timeout_ms = 200
    initial = 'follow' if case == 'already' else 'unfollow'
    button = f'<button data-testid="123-{initial}" onclick="document.querySelector(\'[role=dialog]\').hidden=false">Following</button>'
    change = '' if case == 'no_change' else "document.querySelector('[data-testid$=-unfollow]').dataset.testid='123-follow'"
    app.document('/alice', f'''<div><div data-testid="UserName">@alice</div>{button * (2 if case == 'duplicate' else 1)}</div>
      <div role="dialog" hidden>Unfollow @{'bob' if case == 'wrong_dialog' else 'alice'}?
      <button data-testid="confirmationSheetConfirm" onclick="{change}">Unfollow</button></div>''')
    if case in ('success', 'already'):
        assert (await app.adapter.unfollow_profile('alice'))['success']
    else:
        with pytest.raises(BrowserActionError):
            await app.adapter.unfollow_profile('alice')


def chat_controls(operation, *, effect=True, request=False):
    body = conversation_document(header='<span>@alice</span>')
    if request:
        body = body.replace('<textarea data-testid="dmComposerTextInput"></textarea>', '')
        change = "document.querySelector('#request').outerHTML='<textarea data-testid=dmComposerTextInput></textarea>'" if effect else ''
        return body.replace('<div id="history">', f'<div id="request" data-testid="dm-message-request-prompt"><button data-testid="dm-message-request-accept-button" onclick="{change}">Accept</button></div><div id="history">')
    label = {'mute': 'Mute notifications', 'unmute': 'Unmute notifications', 'delete': 'Delete conversation'}[operation]
    inverse = 'Unmute notifications' if operation == 'mute' else 'Mute notifications'
    change = f"this.textContent='{inverse}'" if effect else ''
    if operation == 'delete':
        change = "document.querySelector('[role=dialog]').hidden=false;this.parentElement.hidden=true"
    body = body.replace('</header>', '<button aria-label="More options" onclick="document.querySelector(\'[role=menu]\').hidden=false">More</button></header>')
    deletion = "document.querySelector('[data-testid=DMConversationView]').remove();history.replaceState({},'', '/messages');document.querySelector('#toast').hidden=false" if effect else ''
    return body + f'''<div role="menu" tabindex="-1" hidden onkeydown="if(event.key==='Escape')this.hidden=true"><button role="menuitem" onclick="{change}">{label}</button></div>
        <div role="dialog" hidden>Delete conversation?<button data-testid="confirmationSheetConfirm" onclick="{deletion}">Delete</button></div>
        <div id="toast" data-testid="toast" hidden>Conversation deleted.</div>'''


@pytest.mark.parametrize('operation', ['accept_request', 'mute', 'unmute', 'delete'])
@pytest.mark.parametrize('case', ['success', 'no_change', 'wrong_participant', 'route_changed'])
async def test_conversation_actions(app, operation, case):
    app.adapter.action_confirmation_timeout_ms = 200
    app.document('/messages/10-20', chat_controls(operation, effect=case != 'no_change', request=operation == 'accept_request'))
    if case in ('wrong_participant', 'route_changed'):
        original = app.adapter._conversation_guard
        calls = 0

        async def changed(scope, url, participants, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                if case == 'wrong_participant':
                    await app.page.get_by_test_id('DMConversationHeader').evaluate("e=>e.firstChild.textContent='@bob'")
                else:
                    await app.page.evaluate("history.replaceState({},'', '/messages/10-30')")
            await original(scope, url, participants, **kwargs)
        app.adapter._conversation_guard = changed
    if case == 'success':
        assert (await app.adapter.manage_conversation('https://x.com/messages/10-20', operation))['success']
    else:
        with pytest.raises(BrowserActionError):
            await app.adapter.manage_conversation('https://x.com/messages/10-20', operation)


async def test_controls_inventory_and_bookmarks(app):
    app.document('/messages/10-20', chat_controls('mute'))
    assert (await app.adapter.get_conversation_controls('https://x.com/messages/10-20'))['actions'] == ['mute']
    app.document('/i/bookmarks', post())
    assert (await app.adapter.get_bookmarks(1))[0].tweet_id == '123'


async def test_empty_bookmarks(app):
    app.document('/i/bookmarks', '<h1>Save posts for later</h1>')
    assert await app.adapter.get_bookmarks(1) == []


@pytest.mark.parametrize('selected', ['true', 'false'])
async def test_history_redirect_requires_selected_bookmarks_tab(app, selected):
    app.document('/i/bookmarks', f'''<script>history.replaceState({{}},'', '/i/history')</script>
      <a role="tab" aria-selected="{selected}" href="/i/history">Bookmarks</a>''' + post())
    if selected == 'true':
        assert (await app.adapter.get_bookmarks(1))[0].tweet_id == '123'
    else:
        with pytest.raises(BrowserActionError):
            await app.adapter.get_bookmarks(1)


async def test_bookmarks_tab_switch_during_collection_is_rejected(app):
    app.document('/i/bookmarks', '''<script>history.replaceState({},'', '/i/history')</script>
      <a role="tab" aria-selected="true" href="/i/history">Bookmarks</a>''' + post())
    original = app.adapter._snapshots

    async def switch_tab():
        rows = await original()
        await app.page.get_by_role('tab').evaluate("e=>e.setAttribute('aria-selected','false')")
        return rows
    app.adapter._snapshots = switch_tab
    with pytest.raises(BrowserActionError):
        await app.adapter.get_bookmarks(1)


@pytest.mark.parametrize('toast', [True, False])
async def test_edited_version_new_id_requires_edit_notification(app, toast):
    app.adapter.account = {'self_handles': ['alice']}
    app.adapter.action_confirmation_timeout_ms = 200
    body = owned_post('edit')
    transition = "document.querySelector('article a').href='/alice/status/456';history.replaceState({},'', '/alice/status/456');"
    if toast:
        transition += "document.querySelector('#toast').textContent='Your post has been edited.';document.querySelector('#toast').hidden=false;"
    body = body.replace("document.querySelector('[role=dialog]').hidden=true", transition + "document.querySelector('[role=dialog]').hidden=true")
    app.document('/alice/status/123', body)
    if toast:
        result = await app.adapter.edit_tweet(URL, 'before', 'after')
        assert result['evidence']['tweet_id'] == '456' and result['evidence']['original_tweet_id'] == '123'
    else:
        with pytest.raises(BrowserActionError):
            await app.adapter.edit_tweet(URL, 'before', 'after')


async def test_preexisting_delete_notification_is_not_new_evidence(app):
    app.adapter.account = {'self_handles': ['alice']}
    app.document('/alice/status/123', owned_post('delete').replace('data-testid="toast" hidden', 'data-testid="toast"'))
    with pytest.raises(BrowserActionError):
        await app.adapter.delete_tweet(URL, 'before')
    assert await app.page.get_by_test_id('tweet').count() == 1


@pytest.mark.parametrize('identity', ['alice', 'bob'])
async def test_two_step_conversation_info_menu_requires_same_participant(app, identity):
    body = chat_controls('mute').replace("document.querySelector('[role=menu]').hidden=false", "document.querySelector('#info').hidden=false")
    # Remove the separate deletion fixture so only the current info sheet exists.
    start = body.index('<div role="dialog" hidden>Delete conversation?')
    end = body.index('</div>', start) + len('</div>')
    body = body[:start] + body[end:]
    body += f'''<div role="dialog" id="info" hidden><div>Conversation info</div><div>@{identity}</div>
      <button aria-label="More" onclick="document.querySelector('[role=menu]').hidden=false">More</button></div>'''
    app.document('/messages/10-20', body)
    if identity == 'alice':
        result = await app.adapter.get_conversation_controls('https://x.com/messages/10-20')
        assert result['actions'] == ['mute']
    else:
        with pytest.raises(BrowserActionError) as error:
            await app.adapter.get_conversation_controls('https://x.com/messages/10-20')
        assert error.value.reason == 'recipient_mismatch'


async def test_inline_header_button_cannot_be_part_of_handle(app):
    app.document('/messages/10-20', chat_controls('mute').replace('<span>@alice</span>', '@alice'))
    with pytest.raises(BrowserActionError) as error:
        await app.adapter.get_conversation_controls('https://x.com/messages/10-20')
    assert error.value.reason == 'recipient_unverified'


@pytest.mark.parametrize('destination', ['10-20', '10-30'])
async def test_request_acceptance_can_promote_only_same_conversation(app, destination):
    body = chat_controls('accept_request', request=True)
    body = body.replace("document.querySelector('#request').outerHTML=", f"history.replaceState({{}},'', '/i/chat/{destination}');document.querySelector('#request').outerHTML=")
    app.document('/i/chat/requests/10-20', body)
    if destination == '10-20':
        assert (await app.adapter.manage_conversation('https://x.com/i/chat/requests/10-20', 'accept_request'))['success']
    else:
        with pytest.raises(BrowserActionError) as error:
            await app.adapter.manage_conversation('https://x.com/i/chat/requests/10-20', 'accept_request')
        assert error.value.reason == 'conversation_mismatch'


def modern_controls(operation='mute', *, destination='10-20', participant='alice', bound=True):
    body = conversation_document(header='<span>@alice</span>')
    body = body.replace('</header>', f'''<button aria-label="More options" onclick="history.replaceState({{}},'', '/i/chat/{destination}/info');document.querySelector('#info').hidden=false">More</button></header>''')
    label, inverse = ('Mute', 'Unmute') if operation == 'mute' else ('Unmute', 'Mute')
    popup = 'options' if bound else 'unrelated'
    return body + f'''<div id="info" role="dialog" hidden><div>Conversation info</div><span>@{participant}</span>
      <button aria-label="Back" onclick="this.parentElement.hidden=true;history.replaceState({{}},'', '/i/chat/{destination}')">Back</button>
      <button aria-label="More" aria-haspopup="dialog" aria-expanded="false" onclick="this.setAttribute('aria-expanded','true');this.setAttribute('aria-controls','{popup}');document.querySelector('#options').hidden=false">More</button></div>
      <div id="options" role="dialog" tabindex="-1" hidden onkeydown="if(event.key==='Escape')this.hidden=true">
      <button onclick="this.textContent=this.textContent==='Mute'?'Unmute':'Mute'">{label}</button><button onclick="document.documentElement.dataset.deleted='yes'">Delete conversation</button></div>'''


@pytest.mark.parametrize('operation', ['mute', 'unmute'])
async def test_modern_info_route_and_bound_popover_actions(app, operation):
    app.document('/i/chat/10-20', modern_controls(operation))
    assert (await app.adapter.manage_conversation('https://x.com/i/chat/10-20', operation))['success']


async def test_modern_controls_reports_only_verified_flows(app):
    app.document('/i/chat/10-20', modern_controls())
    result = await app.adapter.get_conversation_controls('https://x.com/i/chat/10-20')
    assert result['actions'] == ['mute']
    assert result['control_layout'] == 'conversation_info_popover'
    assert result['unsupported_actions'] == [{'operation': 'delete', 'reason': 'unsupported_confirmation_layout'}]
    assert await app.page.locator('html').get_attribute('data-deleted') is None
    assert app.page.url == 'https://x.com/i/chat/10-20'


async def test_modern_read_mute_unmute_cycle_preserves_conversation(app):
    url = 'https://x.com/i/chat/10-20'
    app.document('/i/chat/10-20', modern_controls())
    assert (await app.adapter.get_conversation_controls(url))['actions'] == ['mute']
    assert (await app.adapter.manage_conversation(url, 'mute'))['success']
    assert (await app.adapter.get_conversation_controls(url))['actions'] == ['unmute']
    assert (await app.adapter.manage_conversation(url, 'unmute'))['success']
    assert (await app.adapter.get_conversation_controls(url))['actions'] == ['mute']
    assert app.page.url == url


@pytest.mark.parametrize('case', ['wrong_route', 'wrong_participant', 'unbound_popup', 'unsupported_delete'])
async def test_modern_controls_reject_mismatches_before_action(app, case):
    app.adapter.messaging_timeout_ms = 300
    app.document('/i/chat/10-20', modern_controls(destination='10-30' if case == 'wrong_route' else '10-20',
                 participant='bob' if case == 'wrong_participant' else 'alice', bound=case != 'unbound_popup'))
    with pytest.raises(BrowserActionError):
        await app.adapter.manage_conversation('https://x.com/i/chat/10-20', 'delete' if case == 'unsupported_delete' else 'mute')
    assert await app.page.locator('html').get_attribute('data-deleted') is None
    assert await app.page.get_by_role('button', name='Unmute', exact=True).count() == 0


async def test_unsupported_delete_cleans_up_before_next_read(app):
    url = 'https://x.com/i/chat/10-20'
    app.document('/i/chat/10-20', modern_controls())
    with pytest.raises(BrowserActionError, match='feature_unavailable'):
        await app.adapter.manage_conversation(url, 'delete')
    assert app.page.url == url
    assert not await app.page.locator('#info').is_visible()
    assert not await app.page.locator('#options').is_visible()
    assert (await app.adapter.get_conversation_controls(url))['actions'] == ['mute']
    assert await app.page.locator('html').get_attribute('data-deleted') is None


async def test_missing_info_menu_cleans_up_before_returning_unavailable(app):
    url = 'https://x.com/i/chat/10-20'
    body = modern_controls().replace('aria-label="More" aria-haspopup', 'aria-label="Unavailable" aria-haspopup')
    app.document('/i/chat/10-20', body)
    result = await app.adapter.get_conversation_controls(url)
    assert result['actions'] == [] and result['control_layout'] == 'unavailable'
    assert app.page.url == url
    assert not await app.page.locator('#info').is_visible()


async def test_unconfirmed_notification_change_keeps_failure_and_closes_info(app):
    url = 'https://x.com/i/chat/10-20'
    app.adapter.action_confirmation_timeout_ms = 150
    body = modern_controls().replace("this.textContent=this.textContent==='Mute'?'Unmute':'Mute'", '')
    app.document('/i/chat/10-20', body)
    with pytest.raises(BrowserActionError, match='outcome_unknown'):
        await app.adapter.manage_conversation(url, 'mute')
    assert app.page.url == url
    assert not await app.page.locator('#options').is_visible()
    assert (await app.adapter.get_conversation_controls(url))['actions'] == ['mute']


@pytest.mark.parametrize('layout', ['legacy', 'modern'])
@pytest.mark.parametrize('operation', ['mute', 'unmute'])
async def test_notification_action_already_done_never_clicks(app, layout, operation):
    opposite = 'unmute' if operation == 'mute' else 'mute'
    path = '/i/chat/10-20' if layout == 'modern' else '/messages/10-20'
    app.document(path, modern_controls(opposite) if layout == 'modern' else chat_controls(opposite))
    def fail_write():
        pytest.fail('Already-applied notification state must not write.')
    app.adapter._before_write = fail_write
    result = await app.adapter.manage_conversation('https://x.com' + path, operation)
    assert result['status'] == 'already_done'
    assert app.page.url == 'https://x.com' + path
    assert (await app.adapter.get_conversation_controls('https://x.com' + path))['actions'] == [opposite]


@pytest.mark.parametrize('layout', ['legacy', 'modern'])
@pytest.mark.parametrize('case', ['both', 'duplicate', 'disabled', 'aria_disabled'])
async def test_notification_controls_reject_ambiguous_or_disabled_states(app, layout, case):
    modern = layout == 'modern'
    path = '/i/chat/10-20' if modern else '/messages/10-20'
    body = modern_controls() if modern else chat_controls('mute')
    label = 'Mute' if modern else 'Mute notifications'
    inverse = 'Unmute' if modern else 'Unmute notifications'
    role = '' if modern else ' role="menuitem"'
    marker = '>Mute</button>' if modern else '>Mute notifications</button>'
    if case in ('both', 'duplicate'):
        body = body.replace(marker, marker + f'<button{role}>{inverse if case == "both" else label}</button>')
    else:
        attribute = ' disabled' if case == 'disabled' else ' aria-disabled="true"'
        if modern:
            body = body.replace('<button onclick="this.textContent', '<button' + attribute + ' onclick="this.textContent')
        else:
            body = body.replace('<button role="menuitem"', '<button' + attribute + ' role="menuitem"')
    app.document(path, body)
    def fail_write():
        pytest.fail('Unsupported notification controls must not write.')
    app.adapter._before_write = fail_write
    url = 'https://x.com' + path
    if case in ('both', 'duplicate'):
        with pytest.raises(BrowserActionError, match='unsupported_dom'):
            await app.adapter.get_conversation_controls(url)
    else:
        assert (await app.adapter.get_conversation_controls(url))['actions'] == []
    with pytest.raises(BrowserActionError, match='unsupported_dom|feature_unavailable'):
        await app.adapter.manage_conversation(url, 'mute')
    assert app.page.url == url


async def test_cleanup_does_not_navigate_after_participant_changes(app):
    url = 'https://x.com/i/chat/10-20'
    body = modern_controls().replace("this.textContent=this.textContent==='Mute'?'Unmute':'Mute'",
        "document.querySelector('[data-testid=DMConversationHeader] span').textContent='@bob'")
    app.document('/i/chat/10-20', body)
    with pytest.raises(BrowserActionError, match='recipient_mismatch'):
        await app.adapter.manage_conversation(url, 'mute')
    assert app.page.url == url + '/info'
    assert await app.page.locator('#options').is_visible()


async def test_cleanup_requires_observed_popover_dismissal(app):
    url = 'https://x.com/i/chat/10-20'
    app.adapter.action_confirmation_timeout_ms = 150
    app.document('/i/chat/10-20', modern_controls().replace("if(event.key==='Escape')this.hidden=true", ''))
    with pytest.raises(BrowserActionError, match='outcome_unknown'):
        await app.adapter.get_conversation_controls(url)
    assert app.page.url == url + '/info'
    assert await app.page.locator('#info').is_visible()


async def test_reopened_popover_with_new_id_is_cleaned_up(app):
    url = 'https://x.com/i/chat/10-20'
    body = modern_controls().replace('id="options" role=', 'id="options" data-options role=')
    body = body.replace("this.setAttribute('aria-controls','options');document.querySelector('#options').hidden=false",
        "const popup=document.querySelector('[data-options]');popup.id+='-next';this.setAttribute('aria-controls',popup.id);popup.hidden=false")
    body = body.replace("this.textContent=this.textContent==='Mute'?'Unmute':'Mute'", "this.textContent='Unmute';this.parentElement.hidden=true")
    app.document('/i/chat/10-20', body)
    assert (await app.adapter.manage_conversation(url, 'mute'))['success']
    assert not await app.page.locator('[data-options]').is_visible()
    assert app.page.url == url


@pytest.mark.parametrize('attribute', ['disabled', 'aria-disabled="true"'])
async def test_disabled_request_acceptance_is_not_advertised_or_clicked(app, attribute):
    url = 'https://x.com/messages/10-20'
    body = chat_controls('accept_request', request=True).replace('data-testid="dm-message-request-accept-button"',
        f'data-testid="dm-message-request-accept-button" {attribute}')
    app.document('/messages/10-20', body)
    result = await app.adapter.get_conversation_controls(url)
    assert result['acceptance_required'] is True and result['actions'] == []
    def fail_write():
        pytest.fail('Disabled acceptance must not write.')
    app.adapter._before_write = fail_write
    with pytest.raises(BrowserActionError, match='feature_unavailable'):
        await app.adapter.manage_conversation(url, 'accept_request')
