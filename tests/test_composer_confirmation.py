"""composer.post_new_tweet must not report success without a post-action
confirmation.

A clicked Post button is not proof of a posted tweet: duplicate text
('Whoops, you already said that'), rate limits and spam interstitials all
reject silently at the UI level. After the click the composer must wait for a
state flip — a toast appears or the composer textarea detaches — and must
fail loudly when a platform error toast shows.
"""

from unittest.mock import MagicMock

import pytest
from selenium.common.exceptions import NoSuchElementException, StaleElementReferenceException, TimeoutException

import xuse.features.publisher.composer as comp
import xuse.utils.selenium_waits as waits_mod
from xuse.features.publisher.composer import COMPOSER_ERROR_TOAST_XPATH
from xuse.features.publisher.reply_handler import REPLY_ANY_TOAST_XPATH as ANY_TOAST_Q
from xuse.models import AccountConfig


@pytest.fixture(autouse=True)
def fast_waits_and_no_sleeps(monkeypatch):
    class FastWait:
        def __init__(self, ctx, timeout):
            self.ctx = ctx

        def until(self, condition, message=""):
            for _ in range(3):
                try:
                    r = condition(self.ctx)
                    if r:
                        return r
                except Exception:
                    pass
            raise TimeoutException("fast-wait: condition never satisfied")

    monkeypatch.setattr(comp, "WebDriverWait", FastWait)
    monkeypatch.setattr(waits_mod, "WebDriverWait", FastWait)
    monkeypatch.setattr(comp.time, "sleep", lambda *_a: None)
    monkeypatch.setattr(comp.random, "uniform", lambda a, b: 0.0)
    monkeypatch.setattr(comp, "select_community_if_configured", lambda _d, _a: True)


NEW_TWEET_BTN_Q = "//a[@data-testid='SideNav_NewTweet_Button']"
LAYERS_Q = "//div[@data-testid='layers']"
TA_SCOPED_Q = ".//div[@data-testid='tweetTextarea_0']"
POST_SCOPED_Q = ".//button[@data-testid='tweetButton']"
ERROR_TOAST_Q = COMPOSER_ERROR_TOAST_XPATH


class El:
    def __init__(self, routes=None, text="", on_click=None):
        self._routes = dict(routes or {})
        self._text = text
        self._on_click = on_click
        self.stale = False
        self.clicks = 0
        self.keys = []

    @property
    def text(self):
        return self._text

    def click(self):
        self.clicks += 1
        if self._on_click:
            self._on_click()

    def send_keys(self, *args):
        self.keys.append(args)

    def is_enabled(self):
        if self.stale:
            raise StaleElementReferenceException("node detached")
        return True

    def is_displayed(self):
        return True

    def find_element(self, by, value):
        entry = self._routes.get(value)
        if entry is None:
            raise NoSuchElementException(f"not found: {value}")
        if isinstance(entry, Exception):
            raise entry
        if isinstance(entry, list):
            if not entry:
                raise NoSuchElementException(f"not found: {value}")
            return entry[0]
        return entry

    def find_elements(self, by, value):
        entry = self._routes.get(value)
        if entry is None or isinstance(entry, Exception):
            return []
        return list(entry) if isinstance(entry, list) else [entry]

    def get_attribute(self, name):
        return None


class FakeDriver(El):
    def get(self, url):
        self.last_url = url

    def execute_script(self, *a, **k):
        pass


def build_world():
    refs = {}
    driver = FakeDriver()
    layers = El()
    textarea = El()
    post_btn = El()
    driver._routes[NEW_TWEET_BTN_Q] = El()
    driver._routes[LAYERS_Q] = layers
    layers._routes[TA_SCOPED_Q] = textarea
    layers._routes[POST_SCOPED_Q] = post_btn
    refs.update(driver=driver, layers=layers, textarea=textarea, post_btn=post_btn)

    bm = MagicMock(name="browser_manager")
    bm.get_driver.return_value = driver
    return bm, refs


def test_post_confirmed_by_toast_returns_true():
    bm, refs = build_world()
    refs["post_btn"]._on_click = lambda: refs["driver"]._routes.__setitem__(
        ANY_TOAST_Q, El(text="Your post was sent.")
    )

    assert comp.post_new_tweet(bm, AccountConfig(account_id="a"), "hello world", []) is True
    assert refs["post_btn"].clicks == 1


def test_composer_detach_alone_does_not_confirm_post():
    bm, refs = build_world()
    refs["post_btn"]._on_click = lambda: setattr(refs["textarea"], "stale", True)

    assert comp.post_new_tweet(bm, AccountConfig(account_id="a"), "hello world", []) is False


def test_post_without_confirmation_returns_false(caplog):
    bm, refs = build_world()

    with caplog.at_level("ERROR", logger=comp.logger.name):
        result = comp.post_new_tweet(bm, AccountConfig(account_id="a"), "hello world", [])

    assert result is False
    assert refs["post_btn"].clicks == 1
    assert any("confirmation" in r.getMessage().lower() for r in caplog.records)


def test_platform_error_toast_returns_false(caplog):
    bm, refs = build_world()
    refs["post_btn"]._on_click = lambda: refs["driver"]._routes.__setitem__(
        ERROR_TOAST_Q, El(text="Whoops! You already said that.")
    )

    with caplog.at_level("ERROR", logger=comp.logger.name):
        result = comp.post_new_tweet(bm, AccountConfig(account_id="a"), "hello world", [])

    assert result is False
    assert any("already said that" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize('text', ['Draft saved', 'Link copied', 'You cannot post right now', 'Your post was sent to drafts'])
def test_unrelated_or_rejection_toasts_do_not_confirm_publication(text):
    bm, refs = build_world()
    refs['post_btn']._on_click = lambda: refs['driver']._routes.__setitem__(ANY_TOAST_Q, El(text=text))
    assert comp.post_new_tweet(bm, AccountConfig(account_id='a'), 'hello', []) is False
    assert refs['post_btn'].clicks == 1


def test_preexisting_success_toast_is_not_reused():
    bm, refs = build_world()
    refs['driver']._routes[ANY_TOAST_Q] = El(text='Your post was sent.')
    assert comp.post_new_tweet(bm, AccountConfig(account_id='a'), 'hello', []) is False


def test_requested_media_missing_input_never_posts_text_only(tmp_path):
    media = tmp_path / 'test.png'
    media.write_bytes(b'synthetic')
    bm, refs = build_world()
    assert comp.post_new_tweet(bm, AccountConfig(account_id='a'), 'hello', [str(media)]) is False
    assert refs['post_btn'].clicks == 0


def test_requested_media_not_ready_never_clicks_post(tmp_path):
    media = tmp_path / 'test.png'
    media.write_bytes(b'synthetic')
    bm, refs = build_world()
    upload = El()
    refs['layers']._routes["//input[@data-testid='fileInput' and @type='file']"] = upload
    assert comp.post_new_tweet(bm, AccountConfig(account_id='a'), 'hello', [str(media)]) is False
    assert len(upload.keys) == 1
    assert refs['post_btn'].clicks == 0


def test_ready_media_preserves_all_requested_attachments(tmp_path):
    media = [tmp_path / f'test{i}.png' for i in range(2)]
    for path in media:
        path.write_bytes(b'synthetic')
    bm, refs = build_world()
    upload = El()
    refs['layers']._routes["//input[@data-testid='fileInput' and @type='file']"] = upload
    refs['driver']._routes["//button[@data-testid='tweetButton']"] = refs['post_btn']
    refs['post_btn']._on_click = lambda: refs['driver']._routes.__setitem__(ANY_TOAST_Q, El(text='Your post was sent.'))
    assert comp.post_new_tweet(bm, AccountConfig(account_id='a'), 'hello', [str(p) for p in media]) is True
    assert upload.keys == [('\n'.join(str(p.resolve()) for p in media),)]
    assert refs['post_btn'].clicks == 1


@pytest.mark.parametrize('names', [
    ['missing.png'], ['one.png', 'two.mp4'], ['one.mp4', 'two.mp4'],
    ['one.txt'], ['one.gif', 'two.png'], [f'{i}.png' for i in range(5)],
])
def test_invalid_media_set_is_rejected_before_browser_start(tmp_path, names):
    paths = [tmp_path / name for name in names]
    for path in paths:
        if path.name != 'missing.png':
            path.write_bytes(b'synthetic')
    bm, refs = build_world()
    assert comp.post_new_tweet(bm, AccountConfig(account_id='a'), 'hello', [str(p) for p in paths]) is False
    bm.get_driver.assert_not_called()
    assert refs['post_btn'].clicks == 0
