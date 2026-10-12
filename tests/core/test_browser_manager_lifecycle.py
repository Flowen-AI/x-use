"""BrowserManager lifecycle hardening (fake drivers only — no real browser):

- ``get_driver()`` must quit an unresponsive driver before replacing it.
- Dict-shaped cookie JSON is rejected at load time: no post-start crash, no
  leaked browser, driver still returned without cookies.
- ``pool:<name>`` proxies resolve through ``ProxyManager`` (import path fix).
"""
import json

import pytest
from selenium.common.exceptions import WebDriverException

from xuse.core.browser_manager.constants import PROJECT_ROOT
from xuse.core.browser_manager.cookies import load_cookies_from_file
from xuse.core.browser_manager.service import BrowserManager
from xuse.core.config_loader import CONFIG_DIR as APP_CONFIG_DIR


class FakeWebDriver:
    """Minimal WebDriver stand-in recording lifecycle calls."""

    def __init__(self, responsive: bool = True):
        self.responsive = responsive
        self.quit_called = False
        self.pages = []
        self.cookies_added = []

    @property
    def current_url(self):
        if not self.responsive:
            raise WebDriverException("session gone")
        return "about:blank"

    def quit(self):
        self.quit_called = True

    def set_page_load_timeout(self, seconds):
        pass

    def set_script_timeout(self, seconds):
        pass

    def get(self, url):
        self.pages.append(url)

    def add_cookie(self, cookie):
        self.cookies_added.append(cookie)


def make_manager(make_config_loader, tmp_path, account_config=None, extra_browser_settings=None):
    """BrowserManager backed by tmp config files; WDM cache kept in tmp_path."""
    browser_settings = {
        "webdriver_manager_cache_path": str(tmp_path / "wdm_cache"),
        "enable_stealth": False,
    }
    if extra_browser_settings:
        browser_settings.update(extra_browser_settings)
    loader = make_config_loader(settings={"browser_settings": browser_settings}, accounts=[])
    return BrowserManager(account_config=account_config or {}, config_loader=loader)


def patch_driver_start(monkeypatch, driver):
    """Patch firefox driver init (the default browser type) + login wait."""
    monkeypatch.setattr(
        "xuse.core.browser_manager.service.init_firefox_driver",
        lambda options, configured_path=None, service_args=None: driver,
    )
    monkeypatch.setattr(
        "xuse.core.browser_manager.service.wait_for_signed_in",
        lambda drv, max_wait_seconds=0: False,
    )


def test_anonymous_driver_mode_never_loads_cookies_or_x(make_config_loader, tmp_path, monkeypatch):
    manager = make_manager(make_config_loader, tmp_path, account_config={
        'cookies': [{'name': 'auth_token', 'value': 'synthetic'}]})
    driver = FakeWebDriver()
    patch_driver_start(monkeypatch, driver)
    def forbidden(*args, **kwargs):
        pytest.fail('Anonymous browser attempted account authentication')
    monkeypatch.setattr('xuse.core.browser_manager.service.wait_for_signed_in', forbidden)
    monkeypatch.setattr('xuse.core.browser_manager.service.apply_cookies', forbidden)
    try:
        assert manager.get_driver(authenticate=False) is driver
        assert driver.pages == [] and driver.cookies_added == []
    finally:
        manager.close_driver()
    assert driver.quit_called


# -- unresponsive driver replacement ----------------------------------------


def test_get_driver_quits_unresponsive_driver_before_replacing(make_config_loader, tmp_path, monkeypatch):
    manager = make_manager(make_config_loader, tmp_path)
    old_driver = FakeWebDriver(responsive=False)
    manager.driver = old_driver
    new_driver = FakeWebDriver()
    patch_driver_start(monkeypatch, new_driver)

    driver = manager.get_driver()

    assert old_driver.quit_called is True
    assert driver is new_driver
    assert manager.driver is new_driver


# -- cookie file shape validation --------------------------------------------


def test_load_cookies_from_file_rejects_dict_shape(tmp_path):
    cookie_file = tmp_path / "cookies.json"
    cookie_file.write_text(json.dumps({"cookies": [{"name": "a", "value": "b"}]}), encoding="utf-8")
    assert load_cookies_from_file(str(cookie_file), APP_CONFIG_DIR, PROJECT_ROOT) is None


def test_load_cookies_from_file_rejects_non_dict_entries(tmp_path):
    cookie_file = tmp_path / "cookies.json"
    cookie_file.write_text(json.dumps(["not-a-cookie"]), encoding="utf-8")
    assert load_cookies_from_file(str(cookie_file), APP_CONFIG_DIR, PROJECT_ROOT) is None


def test_load_cookies_from_file_rejects_entries_missing_name_or_value(tmp_path):
    cookie_file = tmp_path / "cookies.json"
    cookie_file.write_text(json.dumps([{"domain": ".x.com"}]), encoding="utf-8")
    assert load_cookies_from_file(str(cookie_file), APP_CONFIG_DIR, PROJECT_ROOT) is None


def test_load_cookies_from_file_accepts_valid_list(tmp_path):
    cookie_file = tmp_path / "cookies.json"
    cookies = [{"name": "auth_token", "value": "x", "domain": ".x.com"}]
    cookie_file.write_text(json.dumps(cookies), encoding="utf-8")
    assert load_cookies_from_file(str(cookie_file), APP_CONFIG_DIR, PROJECT_ROOT) == cookies


def test_dict_shaped_cookie_file_does_not_crash_get_driver(make_config_loader, tmp_path, monkeypatch):
    cookie_file = tmp_path / "cookies.json"
    cookie_file.write_text(json.dumps({"cookies": [{"name": "a", "value": "b"}]}), encoding="utf-8")
    manager = make_manager(make_config_loader, tmp_path, account_config={"cookies": str(cookie_file)})
    assert manager.cookies_data is None

    driver = FakeWebDriver()
    patch_driver_start(monkeypatch, driver)
    assert manager.get_driver() is driver
    assert driver.cookies_added == []
    manager.close_driver()
    assert driver.quit_called is True


# -- proxy pool resolution ----------------------------------------------------


def test_opt_in_strict_quit_retains_driver_for_cleanup_retry(make_config_loader, tmp_path):
    manager = make_manager(make_config_loader, tmp_path)

    class Driver:
        fail = True

        def quit(self):
            if self.fail:
                raise RuntimeError('private-user private-secret')

    driver = Driver()
    manager.driver = driver
    with pytest.raises(RuntimeError, match='cleanup remains pending') as caught:
        manager.close_driver(strict=True)
    assert manager.driver is driver and 'private-secret' not in str(caught.value)
    driver.fail = False
    manager.close_driver(strict=True)
    assert manager.driver is None


def test_anonymous_startup_cleanup_failure_keeps_driver_owned(make_config_loader, tmp_path, monkeypatch):
    manager = make_manager(make_config_loader, tmp_path)
    driver = FakeWebDriver()
    patch_driver_start(monkeypatch, driver)

    def fail(*args, **kwargs):
        raise RuntimeError('private-user private-secret')

    monkeypatch.setattr(driver, 'set_script_timeout', fail)
    monkeypatch.setattr(driver, 'quit', fail)
    with pytest.raises(RuntimeError, match='cleanup remains pending'):
        manager.get_driver(authenticate=False)
    assert manager.driver is driver


def test_proxy_pool_reference_is_resolved(make_config_loader, tmp_path):
    manager = make_manager(
        make_config_loader,
        tmp_path,
        account_config={"account_id": "acc1", "proxy": "pool:resi"},
        extra_browser_settings={"proxy_pools": {"resi": ["http://user:pass@proxy1.example:8080"]}},
    )
    assert manager.effective_proxy == "http://user:pass@proxy1.example:8080"


@pytest.mark.parametrize('failure_stage', ['page_timeout', 'script_timeout', 'cookies', 'login', 'interrupt'])
def test_partial_start_failure_closes_driver_and_allows_clean_retry(
    make_config_loader, tmp_path, monkeypatch, failure_stage
):
    manager = make_manager(make_config_loader, tmp_path)
    driver = FakeWebDriver()
    patch_driver_start(monkeypatch, driver)

    def fail(*_args, **_kwargs):
        if failure_stage == 'interrupt':
            raise KeyboardInterrupt()
        raise WebDriverException('synthetic setup failure')

    if failure_stage == 'page_timeout':
        driver.set_page_load_timeout = fail
    elif failure_stage == 'script_timeout':
        driver.set_script_timeout = fail
    elif failure_stage == 'cookies':
        manager.cookies_data = [{'name': 'auth_token', 'value': 'synthetic'}]
        monkeypatch.setattr('xuse.core.browser_manager.service.apply_cookies', fail)
    else:
        monkeypatch.setattr('xuse.core.browser_manager.service.wait_for_signed_in', fail)

    error_type = KeyboardInterrupt if failure_stage == 'interrupt' else WebDriverException
    with pytest.raises(error_type):
        manager.get_driver()
    assert driver.quit_called
    assert manager.driver is None

    manager.cookies_data = None
    replacement = FakeWebDriver()
    patch_driver_start(monkeypatch, replacement)
    assert manager.get_driver() is replacement
    manager.close_driver()
    assert replacement.quit_called


def test_close_driver_clears_detected_identity_even_when_quit_fails(make_config_loader, tmp_path):
    manager = make_manager(make_config_loader, tmp_path)
    driver = FakeWebDriver()

    def fail():
        raise WebDriverException('already dead')

    driver.quit = fail
    manager.driver = driver
    manager.logged_in_handle = 'old_account'
    manager.close_driver()
    assert manager.driver is None
    assert manager.logged_in_handle is None


@pytest.mark.parametrize('href,expected', [
    ('https://x.com/Synthetic_User', 'Synthetic_User'),
    ('https://x.com/Synthetic_User/', 'Synthetic_User'),
    ('https://X.COM:443/Synthetic_User', 'Synthetic_User'),
    ('https://www.x.com/Synthetic_User', 'Synthetic_User'),
    ('https://mobile.x.com/Synthetic_User', 'Synthetic_User'),
    ('https://twitter.com/Synthetic_User', 'Synthetic_User'),
    ('https://www.twitter.com/Synthetic_User', 'Synthetic_User'),
    ('https://mobile.twitter.com/Synthetic_User', 'Synthetic_User'),
    ('https://x.com/Synthetic_User?ref=private-marker#section', 'Synthetic_User'),
    ('https://not-x.com/Victim', None),
    ('https://evil.invalid/x.com/Victim', None),
    ('https://x.com.evil.invalid/Victim', None),
    ('https://untrusted.x.com/Victim', None),
    ('https://evil.invalid/?redirect=https://x.com/Victim', None),
    ('https://evil.invalid/#https://x.com/Victim', None),
    ('https://x.com@evil.invalid/Victim', None),
    ('https://private-marker@x.com/Victim', None),
    ('https://user:private-marker@x.com/Victim', None),
    ('https://x.com:8443/Victim', None),
    ('https://x.com:bad/Victim', None),
    ('http://x.com/Victim', None),
    ('javascript:https://x.com/Victim', None),
    ('//x.com/Victim', None),
    ('/Victim', None),
    ('https://x.com/', None),
    ('https://x.com/Victim/status/123', None),
    ('https://x.com//Victim', None),
    ('https://x.com/@Victim', None),
    ('https://x.com/%56ictim', None),
    ('https://x.com/abcdefghijklmnop', None),
    ('https://x.com/Victım', None),
    ('https://x.com/Victim\\other', None),
    ('https://x.com/Vi\nctim', None),
    ('\thttps://x.com/Victim', None),
    ('https://[x.com/Victim', None),
    ('', None),
    (None, None),
    (123, None),
])
def test_profile_identity_requires_a_real_profile_url(
    make_config_loader, tmp_path, monkeypatch, caplog, href, expected
):
    """Untrusted DOM links must not corrupt the own-post avoidance identity."""
    manager = make_manager(make_config_loader, tmp_path)
    driver = FakeWebDriver()
    patch_driver_start(monkeypatch, driver)
    monkeypatch.setattr('xuse.core.browser_manager.service.wait_for_signed_in',
                        lambda *_args, **_kwargs: True)

    class ProfileLink:
        def get_attribute(self, name):
            assert name == 'href'
            return href

    driver.find_element = lambda *_args: ProfileLink()
    with caplog.at_level('INFO'):
        try:
            assert manager.get_driver() is driver
            assert manager.logged_in_handle == expected
            assert 'private-marker' not in caplog.text
        finally:
            manager.close_driver()
    assert driver.quit_called
