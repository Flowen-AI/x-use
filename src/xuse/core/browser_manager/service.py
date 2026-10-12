import logging
from pathlib import Path
from typing import List, Dict, Optional, Any

from selenium.webdriver.remote.webdriver import WebDriver
from selenium.common.exceptions import WebDriverException, TimeoutException

from xuse.core.config_loader import ConfigLoader, CONFIG_DIR as APP_CONFIG_DIR
from xuse.utils.proxy_manager import ProxyManager
from .constants import PROJECT_ROOT, DEFAULT_WDM_CACHE_PATH, set_wdm_ssl_verify
from .cookies import load_cookies_from_file, apply_cookies, is_valid_cookie_list
from .options import configure_driver_options
from .drivers import (
    init_chrome_driver,
    init_firefox_driver,
    apply_stealth_if_configured,
)
# Flexible import for login state utilities (works when run as module or script)
try:
    from xuse.utils.login_state import wait_for_signed_in  # type: ignore
except Exception:  # pragma: no cover - fallback for various runner contexts
    import sys, os
    sys.path.append(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
    from xuse.utils.login_state import wait_for_signed_in  # type: ignore

logger = logging.getLogger(__name__)


class BrowserManager:
    def __init__(self, account_config: Optional[Dict[str, Any]] = None, config_loader: Optional[ConfigLoader] = None):
        self.config_loader = config_loader if config_loader else ConfigLoader()
        self.browser_settings = self.config_loader.get_setting('browser_settings', {})
        self.driver: Optional[WebDriver] = None
        self.account_config = account_config if account_config else {}
        self.cookies_data: Optional[List[Dict[str, Any]]] = None
        self.effective_proxy: Optional[str] = None
        self.logged_in_handle: Optional[str] = None

        # Configured but invalid routes must never start a direct browser.
        # Only None/empty values inherit the global fallback; False, 0 and
        # sentinel strings are malformed settings, not a request to go direct.
        self.effective_proxy = ProxyManager(self.config_loader).resolve(
            self.account_config.get('proxy'), account_id=self.account_config.get('account_id'))

        # Configure webdriver_manager SSL verify if provided
        wdm_ssl_verify_config = self.browser_settings.get('webdriver_manager_ssl_verify')
        if wdm_ssl_verify_config is not None:
            set_wdm_ssl_verify(bool(wdm_ssl_verify_config))
            logger.info("WebDriver Manager SSL verification set.")

        # Cache path
        wdm_cache = self.browser_settings.get('webdriver_manager_cache_path', str(DEFAULT_WDM_CACHE_PATH))
        self.wdm_cache_path = Path(wdm_cache)
        self.wdm_cache_path.mkdir(parents=True, exist_ok=True)
        logger.info(f"WebDriver Manager cache path set to: {self.wdm_cache_path.resolve()}")

        # Match the async backend's account-file-relative cookie resolution.
        account_config_dir = Path(getattr(self.config_loader, 'accounts_file',
                                         APP_CONFIG_DIR / 'accounts.json')).parent
        # Load cookies from account_config if provided
        if 'cookies' in self.account_config:
            cookies_input = self.account_config['cookies']
            if isinstance(cookies_input, str):
                self.cookies_data = load_cookies_from_file(cookies_input, account_config_dir, PROJECT_ROOT)
            elif isinstance(cookies_input, list):
                if is_valid_cookie_list(cookies_input):
                    self.cookies_data = cookies_input
                else:
                    logger.error(
                        "Invalid 'cookies' list in account_config: every entry must "
                        "be a dict with 'name' and 'value'. Starting without cookies."
                    )
            else:
                logger.warning("Invalid 'cookies' format in account_config: expected path string or list.")
        elif isinstance(self.account_config.get('cookie_file_path'), str):
            self.cookies_data = load_cookies_from_file(self.account_config['cookie_file_path'], account_config_dir, PROJECT_ROOT)

    def get_driver(self, *, authenticate: bool = True) -> WebDriver:
        if self.driver and self.is_driver_active():
            return self.driver

        # A set-but-unresponsive driver (crashed or killed externally) must be
        # quit before it is replaced — otherwise every recovery leaks the old
        # browser + driver processes (close_driver guards errors and nulls the
        # attribute).
        if self.driver is not None:
            self.close_driver()

        browser_type = str(self.browser_settings.get('type', 'firefox')).lower()
        headless = bool(self.browser_settings.get('headless', False))
        window_size = self.browser_settings.get('window_size')
        driver_options_extra = self.browser_settings.get('driver_options', [])
        
        if browser_type == 'chrome':
            from selenium.webdriver.chrome.options import Options as ChromeOptions  # local import to avoid heavy deps at import time
            use_uc = bool(self.browser_settings.get('use_undetected_chromedriver', False))
            options = configure_driver_options(
                ChromeOptions(),
                'chrome',
                headless=headless,
                window_size=window_size,
                proxy=self.effective_proxy,
                additional_options=driver_options_extra,
                custom_user_agent=self.browser_settings.get('custom_user_agent') if self.browser_settings.get('user_agent_generation') == 'custom' else None,
                for_undetected=use_uc,
            )
            service_args = self.browser_settings.get('chrome_service_args', [])
            configured_path = self.browser_settings.get('chrome_driver_path')
            try:
                self.driver = init_chrome_driver(
                    options, use_undetected=use_uc, configured_path=configured_path,
                    service_args=service_args,
                    version_main=self.browser_settings.get('chrome_version_main'))
            except Exception as e:
                logger.error(f"Failed to initialize Chrome driver: {e}", exc_info=True)
                self.driver = None
                raise
        elif browser_type == 'firefox':
            from selenium.webdriver.firefox.options import Options as FirefoxOptions  # local import
            options = configure_driver_options(
                FirefoxOptions(),
                'firefox',
                headless=headless,
                window_size=window_size,
                proxy=self.effective_proxy,
                additional_options=driver_options_extra,
                custom_user_agent=self.browser_settings.get('custom_user_agent') if self.browser_settings.get('user_agent_generation') == 'custom' else None,
            )
            service_args = self.browser_settings.get('firefox_service_args', [])
            configured_path = self.browser_settings.get('gecko_driver_path')
            try:
                self.driver = init_firefox_driver(options, configured_path=configured_path, service_args=service_args)
            except Exception as e:
                logger.error(f"Failed to initialize Firefox driver: {e}", exc_info=True)
                self.driver = None
                raise
        else:
            raise WebDriverException(f"Unsupported browser type: {browser_type}")

        try:
            # Set timeouts
            page_load_timeout = int(self.browser_settings.get('page_load_timeout_seconds', 30))
            script_timeout = int(self.browser_settings.get('script_timeout_seconds', 30))
            self.driver.set_page_load_timeout(page_load_timeout)
            self.driver.set_script_timeout(script_timeout)
            logger.info(f"{browser_type.capitalize()} WebDriver initialized successfully.")

            if not authenticate:
                # Explicit anonymous probes never inject account cookies,
                # navigate to X, apply page scripts, or wait for sign-in.
                return self.driver

            # Optional stealth
            apply_stealth_if_configured(self.driver, browser_type, bool(self.browser_settings.get('enable_stealth', True)))

            # Apply cookies if available
            if self.cookies_data:
                cookie_domain_url = self.browser_settings.get('cookie_domain_url')
                if not cookie_domain_url:
                    logger.warning("No 'cookie_domain_url' configured in browser_settings. Cookies may not be set correctly.")
                apply_cookies(self.driver, self.cookies_data, cookie_domain_url)
                logger.info(f"Attempted to apply {len(self.cookies_data)} cookies to the browser session.")

            # Optionally verify or allow manual login to establish a signed-in session
            try:
                login_wait_seconds = int(self.browser_settings.get('login_wait_seconds', 0))
            except Exception:
                login_wait_seconds = 0
            try:
                self.driver.get(self.browser_settings.get('cookie_domain_url', 'https://x.com') + '/home')
            except Exception:
                pass
            signed_in = wait_for_signed_in(self.driver, max_wait_seconds=login_wait_seconds)
            if signed_in:
                logger.info("Session appears signed in.")
                # Try to detect the logged-in handle for own-post avoidance
                try:
                    from selenium.webdriver.common.by import By  # local import
                    profile_link = self.driver.find_element(By.XPATH, "//a[@data-testid='AppTabBar_Profile_Link']")
                    href = profile_link.get_attribute('href') if profile_link else None
                    if href and 'x.com/' in href:
                        handle = href.rstrip('/').split('x.com/')[-1]
                        if handle:
                            self.logged_in_handle = handle.lstrip('@')
                            logger.info(f"Detected logged-in handle: @{self.logged_in_handle}")
                except Exception:
                    pass
            else:
                if login_wait_seconds > 0:
                    logger.warning("Session still appears logged out after waiting. Proceeding anyway.")
                else:
                    logger.info("Session may not be signed in. Some actions may be limited. Configure valid cookies or set 'browser_settings.login_wait_seconds' to allow manual login.")

            return self.driver

        except BaseException:
            # A constructor can succeed before timeout, cookie, or login setup
            # fails. The context manager has not entered yet, so it cannot
            # clean up this partially initialized session for us.
            self.close_driver(strict=not authenticate)
            raise

    def close_driver(self, *, strict: bool = False) -> None:
        self.logged_in_handle = None
        if self.driver:
            quit_acknowledged = False
            try:
                self.driver.quit()
                quit_acknowledged = True
                logger.info("WebDriver session closed.")
            except Exception as e:
                if strict:
                    # Anonymous probes keep this exact owner for retry. A
                    # WebDriver quit acknowledgement is not OS exit proof.
                    raise RuntimeError("WebDriver quit was not acknowledged; cleanup remains pending.") from None
                logger.error(f"Error closing WebDriver: {e}", exc_info=True)
            finally:
                if not strict or quit_acknowledged:
                    self.driver = None

    def navigate_to(self, url: str, ensure_driver: bool = True) -> bool:
        if ensure_driver and (not self.driver or not self.is_driver_active()):
            try:
                self.get_driver()
            except WebDriverException:
                logger.error("Failed to initialize driver for navigation.")
                return False
        if not self.driver:
            logger.error("No active WebDriver instance to navigate.")
            return False
        try:
            logger.info(f"Navigating to {url}")
            self.driver.get(url)
            return True
        except TimeoutException:
            logger.error(f"Timeout while loading page: {url}")
            return False
        except Exception as e:
            logger.error(f"Error navigating to {url}: {e}", exc_info=True)
            return False

    def is_driver_active(self) -> bool:
        if not self.driver:
            return False
        try:
            _ = self.driver.current_url
            return True
        except Exception:
            logger.warning("WebDriver is not responsive.")
            return False

    def __enter__(self):
        if not self.driver or not self.is_driver_active():
            self.get_driver()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close_driver()
