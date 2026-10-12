"""Single-attempt controls tied to exact rendered targets and observed outcomes.

Menu variants are deliberately narrow. Missing controls mean unavailable, not
permission to use private APIs or act on a nearby post/conversation.
"""
import asyncio
import re
from contextlib import asynccontextmanager

from .errors import BrowserActionError
from .messaging import _safe_dom_errors as safe, _HEADER, _REQUEST_PROMPT, conversation_url


class ControlsMixin:
    async def _control_one(self, locator, reason='unsupported_dom'):
        if await locator.count() != 1 or not await locator.is_visible():
            raise BrowserActionError(reason)
        return locator

    async def _control_observe(self, predicate):
        deadline = asyncio.get_running_loop().time() + self.action_confirmation_timeout_ms / 1000
        while True:
            await self.check_blocked()
            if await predicate():
                return
            if asyncio.get_running_loop().time() >= deadline:
                raise BrowserActionError('outcome_unknown')
            await asyncio.sleep(.1)

    @safe('read_failed')
    async def get_bookmarks(self, limit=20):
        from .page import _limit
        _limit(limit)
        await self.navigate('https://x.com/i/bookmarks')
        tab = self.page.get_by_role('tab', name='Bookmarks', exact=True)
        empty = self.page.get_by_text(re.compile(r'^(?:Save posts for later|Save Tweets for later|You haven.t added any posts to your Bookmarks yet)$', re.I))
        deadline = asyncio.get_running_loop().time() + 10
        while True:
            await self.check_blocked()
            route = self.page.url.rstrip('/')
            if route not in ('https://x.com/i/bookmarks', 'https://x.com/i/history'):
                raise BrowserActionError('unsupported_dom')
            if route == 'https://x.com/i/history':
                if (await tab.count() == 1 and await tab.is_visible() and
                        await tab.get_attribute('aria-selected') == 'true' and
                        await tab.get_attribute('href') == '/i/history'):
                    break
                # A hydrated, explicitly unselected tab is a different view.
                if await tab.count() == 1 and await tab.get_attribute('aria-selected') == 'false':
                    raise BrowserActionError('unsupported_dom')
            elif await self._visible(self.page.get_by_test_id('tweet')) or await self._visible(empty):
                break
            if asyncio.get_running_loop().time() >= deadline:
                raise BrowserActionError('unsupported_dom')
            await asyncio.sleep(.1)

        async def verify_view():
            if self.page.url.rstrip('/') != route:
                raise BrowserActionError('unsupported_dom')
            if route == 'https://x.com/i/history':
                await self._control_one(tab)
                if await tab.get_attribute('aria-selected') != 'true' or await tab.get_attribute('href') != '/i/history':
                    raise BrowserActionError('unsupported_dom')
        await verify_view()
        if await empty.count() == 1 and await empty.is_visible() and not await self.page.get_by_test_id('tweet').count():
            await verify_view()
            return []
        return await self._collect(limit, route=self.page.url, view_validator=verify_view)

    async def _post_toggle(self, url, action, before, after, confirm=None):
        card, _, target = await self._target(url, wait_for_target=True)
        source, inverse = self._target_control(card, before), self._target_control(card, after)
        await self._verify_action_target(card, target)
        if await inverse.count() == 1 and await inverse.is_visible() and not await source.count():
            return self._result(action, 'already_done', tweet_id=target, observed=after)
        button = await self._control_one(source)
        if await inverse.count():
            raise BrowserActionError('unsupported_dom')
        await self._verify_action_target(card, target)
        self._before_write()
        await button.click()
        if confirm:
            button = self.page.get_by_test_id(confirm)
            await self._wait_visible(button)
            await self._control_one(button)
            await self._verify_action_target(card, target)
            self._before_write()
            await button.click()

        async def observed():
            await self._verify_action_target(card, target)
            return await inverse.count() == 1 and await inverse.is_visible() and not await source.count()
        await self._control_observe(observed)
        return self._result(action, tweet_id=target, observed=after)

    @safe('outcome_unknown')
    async def bookmark_tweet(self, url):
        return await self._post_toggle(url, 'bookmark_tweet', 'bookmark', 'removeBookmark')

    @safe('outcome_unknown')
    async def remove_bookmark(self, url):
        return await self._post_toggle(url, 'remove_bookmark', 'removeBookmark', 'bookmark')

    @safe('outcome_unknown')
    async def unlike_tweet(self, url):
        return await self._post_toggle(url, 'unlike_tweet', 'unlike', 'like')

    @safe('outcome_unknown')
    async def undo_retweet(self, url):
        return await self._post_toggle(url, 'undo_retweet', 'unretweet', 'retweet', 'unretweetConfirm')

    @safe('outcome_unknown')
    async def unfollow_profile(self, handle):
        from .page import _handle
        handle = _handle(handle)
        await self.navigate('https://x.com/' + handle)
        header = self.page.get_by_test_id('UserName').filter(has=self.page.get_by_text(re.compile('^@' + re.escape(handle) + '$', re.I), exact=True))
        await self._wait_visible(header)
        await self._verify_profile_target(header, handle)
        region = header.locator('xpath=ancestor::div[.//button[substring(@data-testid, string-length(@data-testid)-6)="-follow" or substring(@data-testid, string-length(@data-testid)-8)="-unfollow"]][1]')
        await self._control_one(region)
        source = region.locator('button[data-testid$="-unfollow"]')
        inverse = region.locator('button[data-testid$="-follow"]')
        if await inverse.count() == 1 and await inverse.is_visible() and not await source.count():
            return self._result('unfollow_profile', 'already_done', handle=handle)
        await self._control_one(source)
        await self._verify_profile_target(header, handle)
        self._before_write()
        await source.click()
        dialog = self.page.get_by_role('dialog').filter(has=self.page.get_by_test_id('confirmationSheetConfirm'))
        await self._wait_visible(dialog)
        await self._control_one(dialog)
        # The confirmation must name the exact profile, not just say Unfollow.
        if not re.search(r'@' + re.escape(handle) + r'(?![A-Za-z0-9_])', await dialog.inner_text(), re.I):
            raise BrowserActionError('profile_mismatch')
        confirm = await self._control_one(dialog.get_by_test_id('confirmationSheetConfirm'))
        await self._verify_profile_target(header, handle)
        self._before_write()
        await confirm.click()

        async def observed():
            await self._verify_profile_target(header, handle)
            return await inverse.count() == 1 and await inverse.is_visible() and not await source.count()
        await self._control_observe(observed)
        return self._result('unfollow_profile', handle=handle, observed='follow_control')

    async def _reviewed_post(self, url, expected_text):
        card, data, target = await self._target(url, wait_for_target=True)
        own = {str(h).lstrip('@').lower() for h in self.account.get('self_handles', [])}
        if not data.get('handle') or data['handle'].lstrip('@').lower() not in own:
            raise BrowserActionError('ownership_unverified')
        if data.get('text') != expected_text:
            raise BrowserActionError('stale_review')
        return card, target

    async def _verify_review(self, card, target, expected_text):
        from .page import _SNAPSHOT
        await self._verify_action_target(card, target)
        data = await card.evaluate(_SNAPSHOT)
        own = {str(h).lstrip('@').lower() for h in self.account.get('self_handles', [])}
        if str(data.get('handle') or '').lstrip('@').lower() not in own:
            raise BrowserActionError('ownership_unverified')
        if data.get('text') != expected_text:
            raise BrowserActionError('stale_review')

    async def _post_menu(self, card, target, expected_text, label):
        if await self._visible(self.page.get_by_role('menu')) or await self._visible(self.page.get_by_role('dialog')):
            raise BrowserActionError('unsupported_dom')
        button = await self._control_one(self._target_control(card, 'caret'))
        await self._verify_review(card, target, expected_text)
        await button.click()
        menu = self.page.get_by_role('menu')
        await self._wait_visible(menu)
        await self._control_one(menu)
        option = await self._control_one(menu.get_by_role('menuitem', name=label, exact=True), 'feature_unavailable')
        await self._verify_review(card, target, expected_text)
        return option

    @safe('outcome_unknown')
    async def delete_tweet(self, url, expected_text):
        card, target = await self._reviewed_post(url, expected_text)
        if await self._visible(self.page.get_by_test_id('toast')):
            raise BrowserActionError('unsupported_dom')
        option = await self._post_menu(card, target, expected_text, 'Delete')
        await option.click()
        dialog = self.page.get_by_role('dialog').filter(has=self.page.get_by_test_id('confirmationSheetConfirm'))
        await self._wait_visible(dialog)
        await self._control_one(dialog)
        if not re.search(r'Delete (?:post|Tweet)\?', await dialog.inner_text(), re.I):
            raise BrowserActionError('unsupported_dom')
        confirm = await self._control_one(dialog.get_by_test_id('confirmationSheetConfirm'))
        await self._verify_review(card, target, expected_text)
        self._before_write()
        await confirm.click()

        async def observed():
            # Absence alone can be navigation or virtualization, not deletion.
            toast = self.page.get_by_test_id('toast').filter(has_text=re.compile(r'^(?:Your (?:post|Tweet) was deleted|Post deleted)\.?$', re.I))
            return await toast.count() == 1 and await toast.is_visible() and await self._find_target(target) is None
        await self._control_observe(observed)
        return self._result('delete_tweet', tweet_id=target, observed='deletion_toast_and_target_absent')

    @safe('outcome_unknown')
    async def edit_tweet(self, url, expected_text, text):
        self._text(text)
        card, target = await self._reviewed_post(url, expected_text)
        if await self._visible(self.page.get_by_test_id('toast')):
            raise BrowserActionError('unsupported_dom')
        from .page import status_id
        before_ids = {status_id(data.get('status_url')) for data in await self._snapshots()}
        option = await self._post_menu(card, target, expected_text, 'Edit post')
        await option.click()
        dialog = self.page.get_by_role('dialog').filter(has=self.page.get_by_test_id('tweetTextarea_0'))
        await self._wait_visible(dialog)
        await self._control_one(dialog)
        composer = await self._control_one(dialog.get_by_test_id('tweetTextarea_0'))
        if await composer.inner_text() != expected_text:
            raise BrowserActionError('stale_review')
        await composer.fill(text)
        submit = await self._control_one(dialog.get_by_test_id('tweetButton'))
        if not await submit.is_enabled():
            raise BrowserActionError('feature_unavailable')
        await self._verify_review(card, target, expected_text)
        if await composer.inner_text() != text:
            raise BrowserActionError('stale_review')
        self._before_write()
        await submit.click()

        evidence = {}

        async def observed():
            if await dialog.is_visible():
                return False
            from .page import _SNAPSHOT, status_id
            current = status_id(self.page.url)
            own = {str(h).lstrip('@').lower() for h in self.account.get('self_handles', [])}
            if current == target:
                await self._verify_action_target(card, target)
                data = await card.evaluate(_SNAPSHOT)
                if data.get('text') == text and str(data.get('handle') or '').lstrip('@').lower() in own:
                    evidence.update(tweet_id=target, observed='same_post_updated_text')
                    return True
                return False
            # X can assign a new ID to an edited version. Require the exact new
            # route, a previously unseen own card, reviewed text and edit toast.
            toast = self.page.get_by_test_id('toast').filter(has_text=re.compile(r'^Your post has been edited\.?$', re.I))
            candidate = await self._find_target(current) if current else None
            if not candidate or await toast.count() != 1 or not await toast.is_visible():
                return False
            data = candidate[1]
            if (current in before_ids or data.get('text') != text or
                    str(data.get('handle') or '').lstrip('@').lower() not in own):
                return False
            await self._verify_action_target(candidate[0], current)
            evidence.update(tweet_id=current, original_tweet_id=target, observed='edit_toast_and_new_own_version')
            return True
        await self._control_observe(observed)
        return self._result('edit_tweet', **evidence)

    async def _conversation_info(self, participants):
        # Locator text matching uses textContent, whose block boundaries may
        # have no newlines. Validate the rendered inner_text separately below.
        info = await self._wait_one(self.page.get_by_role('dialog').filter(has_text='Conversation info'))
        text = await info.inner_text()
        handles = {value.lower() for value in re.findall(r'(?<![A-Za-z0-9_])@([A-Za-z0-9_]{1,15})(?![A-Za-z0-9_])', text)}
        expected = {value.lower().lstrip('@') for value in participants}
        if not text.startswith('Conversation info\n') or handles != expected:
            raise BrowserActionError('recipient_mismatch')
        return info

    async def _conversation_guard(self, scope, url, participants, *, allow_info=False):
        await self._messaging_blocked()
        current = self.page.url.rstrip('/')
        info_route = allow_info and url.startswith('https://x.com/i/chat/') and current == url + '/info'
        if (current != url and not info_route) or await scope.count() != 1 or not await scope.is_visible():
            raise BrowserActionError('conversation_mismatch')
        if await self._participant_handles(scope) != participants:
            raise BrowserActionError('recipient_mismatch')
        if info_route:
            await self._conversation_info(participants)

    async def _conversation_menu(self, scope, url, participants):
        if await self._visible(self.page.get_by_role('menu')):
            raise BrowserActionError('unsupported_dom')
        dialogs = await self._visible(self.page.get_by_role('dialog'))
        if not dialogs:
            header = await self._control_one(scope.locator(_HEADER))
            button = header.get_by_role('button', name=re.compile(r'^(?:More|More options|Conversation options)$', re.I))
            await self._control_one(button, 'feature_unavailable')
            await self._conversation_guard(scope, url, participants, allow_info=True)
            await button.click()
        menu = self.page.get_by_role('menu')
        deadline = asyncio.get_running_loop().time() + self.messaging_timeout_ms / 1000
        while not await self._visible(menu):
            await self._conversation_guard(scope, url, participants, allow_info=True)
            dialogs = await self._visible(self.page.get_by_role('dialog'))
            if dialogs:
                if len(dialogs) != 1:
                    raise BrowserActionError('unsupported_dom')
                info = await self._conversation_info(participants)
                more = await self._control_one(info.get_by_role('button', name='More', exact=True), 'feature_unavailable')
                await self._conversation_guard(scope, url, participants, allow_info=True)
                await more.click()
                if await more.get_attribute('aria-haspopup') == 'dialog':
                    while True:
                        await self._conversation_guard(scope, url, participants, allow_info=True)
                        popup_id = await more.get_attribute('aria-controls')
                        if popup_id and re.fullmatch(r'[A-Za-z0-9_:-]{1,200}', popup_id):
                            popup = self.page.locator(f'[role="dialog"][id="{popup_id}"]')
                            if (await more.get_attribute('aria-expanded') == 'true' and
                                    await popup.count() == 1 and await popup.is_visible()):
                                return popup
                        if asyncio.get_running_loop().time() >= deadline:
                            raise BrowserActionError('unsupported_dom')
                        await asyncio.sleep(.1)
                break
            if asyncio.get_running_loop().time() >= deadline:
                raise BrowserActionError('feature_unavailable')
            await asyncio.sleep(.1)
        await self._wait_visible(menu)
        await self._control_one(menu)
        await self._conversation_guard(scope, url, participants, allow_info=True)
        return menu

    async def _close_conversation_info(self, scope, url, participants):
        if self.page.url.rstrip('/') != url + '/info':
            return
        info = await self._conversation_info(participants)
        back = await self._control_one(info.get_by_role('button', name='Back', exact=True))
        await self._conversation_guard(scope, url, participants, allow_info=True)
        await back.click()

        async def closed():
            if self.page.url.rstrip('/') == url + '/info':
                return False
            await self._conversation_guard(scope, url, participants)
            return not await info.is_visible()
        await self._control_observe(closed)

    async def _dismiss_conversation_controls(self, scope, url, participants, menu):
        """Dismiss only controls still bound to the verified conversation."""
        await self._conversation_guard(scope, url, participants, allow_info=True)
        if menu is not None and await menu.is_visible():
            await menu.press('Escape')

            async def dismissed():
                await self._conversation_guard(scope, url, participants, allow_info=True)
                return not await menu.is_visible()
            await self._control_observe(dismissed)
        elif menu is None:
            # Menu discovery may fail after opening the info sheet. Do not
            # dismiss an unrelated or unbound popup in an unsupported layout.
            if await self._visible(self.page.get_by_role('menu')):
                return
            dialogs = await self._visible(self.page.get_by_role('dialog'))
            if len(dialogs) > 1:
                return
        await self._close_conversation_info(scope, url, participants)

    @asynccontextmanager
    async def _conversation_controls(self, scope, url, participants, *, close_on_success=True):
        # Track reopened menu instances too: modern popover IDs can change.
        menus = []
        try:
            menus.append(await self._conversation_menu(scope, url, participants))
            yield menus
        except Exception:
            try:
                await self._dismiss_conversation_controls(scope, url, participants, menus[-1] if menus else None)
            except Exception:
                # Cleanup must not hide the original mismatch, challenge or
                # uncertain write, nor turn it into a successful operation.
                pass
            raise
        else:
            if close_on_success:
                await self._dismiss_conversation_controls(scope, url, participants, menus[-1])

    async def _notification_controls(self, menu):
        controls = {}
        present = 0
        for operation in ('mute', 'unmute'):
            button = await self._conversation_action(menu, operation)
            count = await button.count()
            if count > 1:
                raise BrowserActionError('unsupported_dom')
            present += count
            if (count == 1 and await button.is_visible() and await button.is_enabled()
                    and await button.get_attribute('aria-disabled') != 'true'):
                controls[operation] = button
        if present > 1:
            raise BrowserActionError('unsupported_dom')
        return controls

    @safe('read_failed')
    async def get_conversation_controls(self, conversation_id):
        scope, url, resolved = await self._open_conversation(conversation_id)
        participants = await self._wait_participants(scope)
        state = await self._conversation_reply_state(scope)
        actions = []
        if state['acceptance_required'] is True:
            accept = scope.locator(_REQUEST_PROMPT).get_by_test_id('dm-message-request-accept-button')
            if await accept.is_enabled() and await accept.get_attribute('aria-disabled') != 'true':
                actions.append('accept_request')
        unsupported_actions = []
        layout = 'request_prompt_only' if actions else 'unavailable'
        try:
            async with self._conversation_controls(scope, url, participants) as menus:
                menu = menus[-1]
                native = await menu.get_attribute('role') == 'dialog'
                layout = 'conversation_info_popover' if native else 'menu'
                deletion = menu.get_by_role('button', name='Delete conversation', exact=True)
                if native and await deletion.count() == 1 and await deletion.is_visible():
                    unsupported_actions.append({'operation': 'delete', 'reason': 'unsupported_confirmation_layout'})
                actions.extend(await self._notification_controls(menu))
                deletion = await self._conversation_action(menu, 'delete')
                if (deletion is not None and await deletion.count() == 1
                        and await deletion.is_visible() and await deletion.is_enabled()
                        and await deletion.get_attribute('aria-disabled') != 'true'):
                    actions.append('delete')
        except BrowserActionError as exc:
            if exc.reason != 'feature_unavailable':
                raise
        await self._conversation_guard(scope, url, participants, allow_info=True)
        return {'conversation_id': resolved, 'url': url, 'actions': actions, 'partial': True,
                'unsupported_actions': unsupported_actions, 'control_layout': layout,
                'can_reply': state['can_reply'], 'acceptance_required': state['acceptance_required'],
                'source': 'visible_controls', 'opening_may_mark_read': True}

    _conversation_labels = {'mute': 'Mute notifications', 'unmute': 'Unmute notifications', 'delete': 'Delete conversation'}

    async def _conversation_action(self, menu, operation):
        if await menu.get_attribute('role') == 'dialog':
            # Modern deletion uses a different confirmation flow, not yet verified.
            if operation == 'delete':
                return None
            return menu.get_by_role('button', name={'mute': 'Mute', 'unmute': 'Unmute'}[operation], exact=True)
        return menu.get_by_role('menuitem', name=self._conversation_labels[operation], exact=True)

    @safe('outcome_unknown')
    async def manage_conversation(self, conversation_id, operation):
        if operation not in {'accept_request', *self._conversation_labels}:
            raise BrowserActionError('invalid_input')
        scope, url, resolved = await self._open_conversation(conversation_id)
        participants = await self._wait_participants(scope)
        if await self._visible(self.page.get_by_test_id('toast')):
            raise BrowserActionError('unsupported_dom')
        if operation == 'accept_request':
            await self._conversation_reply_state(scope)
            prompt = await self._control_one(scope.locator(_REQUEST_PROMPT), 'feature_unavailable')
            button = await self._control_one(prompt.get_by_test_id('dm-message-request-accept-button'))
            if not await button.is_enabled() or await button.get_attribute('aria-disabled') == 'true':
                raise BrowserActionError('feature_unavailable')
            await self._conversation_guard(scope, url, participants)
            self._before_write()
            await button.click()

            async def accepted():
                try:
                    current_url, current_id = conversation_url(self.page.url)
                except ValueError:
                    raise BrowserActionError('conversation_mismatch') from None
                allowed = {url}
                if url.startswith('https://x.com/i/chat/requests/'):
                    allowed.add('https://x.com/i/chat/' + resolved)
                if current_url not in allowed or current_id != resolved:
                    raise BrowserActionError('conversation_mismatch')
                current_scope = await self._conversation_scope()
                await self._conversation_guard(current_scope, current_url, participants)
                state = await self._conversation_reply_state(current_scope)
                return state['acceptance_required'] is False and state['can_reply'] is True
            await self._control_observe(accepted)
            return self._result('manage_conversation', conversation_id=resolved, operation=operation, observed='writable_composer')
        async with self._conversation_controls(scope, url, participants, close_on_success=operation != 'delete') as menus:
            menu = menus[-1]
            if operation != 'delete':
                controls = await self._notification_controls(menu)
                opposite = 'unmute' if operation == 'mute' else 'mute'
                if opposite in controls:
                    await self._conversation_guard(scope, url, participants, allow_info=True)
                    return self._result('manage_conversation', 'already_done', conversation_id=resolved,
                                        operation=operation, observed='inverse_notification_control')
                candidate = controls.get(operation)
            else:
                candidate = await self._conversation_action(menu, operation)
            if candidate is None:
                raise BrowserActionError('feature_unavailable')
            button = await self._control_one(candidate, 'feature_unavailable')
            if not await button.is_enabled() or await button.get_attribute('aria-disabled') == 'true':
                raise BrowserActionError('feature_unavailable')
            await self._conversation_guard(scope, url, participants, allow_info=True)
            self._before_write()
            await button.click()
            if operation == 'delete':
                dialog = self.page.get_by_role('dialog').filter(has=self.page.get_by_test_id('confirmationSheetConfirm'))
                await self._wait_visible(dialog)
                await self._control_one(dialog)
                if not re.search(r'Delete conversation\?', await dialog.inner_text(), re.I):
                    raise BrowserActionError('unsupported_dom')
                confirm = await self._control_one(dialog.get_by_test_id('confirmationSheetConfirm'))
                await self._conversation_guard(scope, url, participants, allow_info=True)
                self._before_write()
                await confirm.click()

                async def deleted():
                    toast = self.page.get_by_test_id('toast').filter(has_text=re.compile(r'^Conversation deleted\.?$', re.I))
                    return (await toast.count() == 1 and await toast.is_visible() and
                            not await scope.is_visible() and self.page.url.rstrip('/') in ('https://x.com/messages', 'https://x.com/i/chat'))
                await self._control_observe(deleted)
                evidence = 'deletion_toast_and_conversation_closed'
            else:
                # Observe the inverse option after reopening the exact same menu.
                await self._conversation_guard(scope, url, participants, allow_info=True)
                if not await menu.is_visible():
                    menu = await self._conversation_menu(scope, url, participants)
                    menus.append(menu)

                async def changed():
                    await self._conversation_guard(scope, url, participants, allow_info=True)
                    return opposite in await self._notification_controls(menu)
                await self._control_observe(changed)
                evidence = 'inverse_notification_control'
        return self._result('manage_conversation', conversation_id=resolved, operation=operation, observed=evidence)
