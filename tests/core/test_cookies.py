"""Cookie application contract: exactly one domain navigation and NO
trailing refresh. The caller's verification navigation (service.py loads
/home right after) is what sends the freshly-set cookies — the refresh was
a redundant 4th page load on every cold start (live finding, v2.3)."""
import pytest
from selenium.common.exceptions import WebDriverException

from xuse.core.browser_manager.cookies import apply_cookies


class FakeDriver:
    def __init__(self):
        self.calls = []
        self.current_url = 'https://x.com/home'

    def get(self, url):
        self.calls.append(("get", url))
        self.current_url = url

    def refresh(self):
        self.calls.append(("refresh",))

    def add_cookie(self, cookie):
        self.calls.append(("add_cookie", cookie["name"]))


def test_apply_cookies_navigates_once_without_refresh():
    driver = FakeDriver()
    apply_cookies(
        driver,
        [{"name": "auth_token", "value": "x", "domain": ".x.com", "path": "/"}],
        "https://x.com",
    )
    assert [c for c in driver.calls if c[0] == "get"] == [("get", "https://x.com")]
    assert ("add_cookie", "auth_token") in driver.calls
    assert ("refresh",) not in driver.calls


def test_apply_cookies_without_domain_just_sets_cookies():
    driver = FakeDriver()
    apply_cookies(driver, [{"name": "ct0", "value": "y"}], None)
    assert driver.calls == [("add_cookie", "ct0")]


def test_failed_navigation_does_not_inject_host_only_credentials():
    driver = FakeDriver()
    driver.current_url = 'https://unrelated.example/'

    def fail(_url):
        raise WebDriverException('navigation failed')

    driver.get = fail
    with pytest.raises(WebDriverException, match='navigation failed'):
        apply_cookies(driver, [{'name': 'auth_token', 'value': 'synthetic-secret'}], 'https://x.com')
    assert driver.calls == []


@pytest.mark.parametrize('origin', [
    'https://unrelated.example/', 'https://x.com.evil.example/',
    'http://x.com/', 'https://user:secret@x.com/', 'https://x.com:444/',
    'about:blank', 'https://x.com:invalid/',
])
@pytest.mark.parametrize('navigate', [True, False])
def test_unverified_origin_never_receives_cookies(origin, navigate):
    driver = FakeDriver()
    driver.current_url = origin
    with pytest.raises(WebDriverException):
        apply_cookies(driver, [{'name': 'auth_token', 'value': 'synthetic-secret'}], origin if navigate else None)
    assert not any(call[0] == 'add_cookie' for call in driver.calls)


def test_redirect_to_unrelated_origin_is_rejected():
    driver = FakeDriver()
    driver.get = lambda _url: setattr(driver, 'current_url', 'https://unrelated.example/')
    with pytest.raises(WebDriverException, match='could not be verified'):
        apply_cookies(driver, [{'name': 'auth_token', 'value': 'synthetic-secret'}], 'https://x.com')
    assert driver.calls == []


def test_cookie_error_does_not_log_driver_echoed_credentials(caplog):
    driver = FakeDriver()

    def fail(cookie):
        raise WebDriverException(f'Invalid payload: {cookie}')

    driver.add_cookie = fail
    apply_cookies(driver, [{'name': 'auth_token', 'value': 'synthetic-secret'}], 'https://x.com')
    assert 'synthetic-secret' not in caplog.text
    assert 'WebDriverException' in caplog.text


@pytest.mark.parametrize('expiry_key', ['expiry', 'expires', 'expirationDate'])
def test_all_supported_expiry_formats_are_preserved(expiry_key):
    driver = FakeDriver()
    submitted = []
    driver.add_cookie = submitted.append
    apply_cookies(driver, [{'name': 'auth_token', 'value': 'test', expiry_key: 2000000000.75}], None)
    assert submitted[0]['expiry'] == 2000000000
