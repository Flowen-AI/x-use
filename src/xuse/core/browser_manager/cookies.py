import json
import logging
from pathlib import Path
from typing import List, Dict, Optional, Any
from urllib.parse import urlsplit

from selenium.webdriver.remote.webdriver import WebDriver
from selenium.common.exceptions import WebDriverException

logger = logging.getLogger(__name__)

_COOKIE_HOSTS = {"x.com", "www.x.com", "mobile.x.com", "twitter.com", "www.twitter.com", "mobile.twitter.com"}


def _is_cookie_origin(url: str) -> bool:
    """Never deliver an X session cookie to an unrelated current page."""
    try:
        parsed = urlsplit(url)
        return (
            parsed.scheme == "https"
            and parsed.hostname in _COOKIE_HOSTS
            and parsed.port in (None, 443)
            and parsed.username is None
            and parsed.password is None
        )
    except (TypeError, ValueError):
        return False


def is_valid_cookie_list(data: Any) -> bool:
    """Cookie payloads must be a list of dicts carrying at least name+value.

    Anything else (a ``{"cookies": [...]}`` wrapper dict, a bare cookie object,
    string entries) would crash ``apply_cookies`` with an AttributeError AFTER
    the browser process has already started — leaking the live browser.
    """
    return (
        isinstance(data, list)
        and all(isinstance(c, dict) and "name" in c and "value" in c for c in data)
    )


def load_cookies_from_file(candidate_path: str, config_dir: Path, project_root: Path) -> Optional[List[Dict[str, Any]]]:
    """Load cookies JSON from config dir, project root, or absolute path."""
    config_dir_cookie_path = config_dir / candidate_path
    project_root_cookie_path = project_root / candidate_path

    resolved: Optional[Path] = None
    if config_dir_cookie_path.is_file():
        resolved = config_dir_cookie_path
        logger.debug(f"Found cookie file relative to config directory: {resolved}")
    elif project_root_cookie_path.is_file():
        resolved = project_root_cookie_path
        logger.debug(f"Found cookie file relative to project root: {resolved}")
    else:
        abs_path = Path(candidate_path)
        if abs_path.is_absolute() and abs_path.is_file():
            resolved = abs_path
            logger.debug(f"Found cookie file at absolute path: {resolved}")

    if not resolved:
        logger.error(f"Cookie file not found at '{candidate_path}' (checked config dir, project root, and absolute path).")
        return None

    try:
        with resolved.open('r', encoding='utf-8') as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        logger.error(f"Error decoding JSON from cookie file {resolved}: {e}")
        return None
    except Exception as e:
        logger.error(f"Error loading cookies from file {resolved}: {e}")
        return None

    if not is_valid_cookie_list(data):
        logger.error(
            f"Cookie file {resolved} has an unexpected shape: expected a JSON "
            f"list of cookie objects with 'name' and 'value' (got "
            f"{type(data).__name__}). Starting without cookies."
        )
        return None
    return data


def apply_cookies(driver: WebDriver, cookies: List[Dict[str, Any]], cookie_domain_url: Optional[str]) -> None:
    if cookie_domain_url:
        if not _is_cookie_origin(cookie_domain_url):
            raise WebDriverException("Cookie setup requires an HTTPS X or Twitter origin.")
        try:
            driver.get(cookie_domain_url)
        except Exception:
            # Even a host-only cookie would otherwise be added to whichever
            # unrelated page remained open after the failed navigation.
            raise WebDriverException("Cookie origin navigation failed; no cookies applied.") from None

    try:
        current_origin_valid = _is_cookie_origin(driver.current_url)
    except Exception:
        current_origin_valid = False
    if not current_origin_valid:
        raise WebDriverException("Cookie origin could not be verified; no cookies applied.")

    for cookie_dict in cookies:
        selenium_cookie: Dict[str, Any] = {}

        # Normalize common export formats (e.g., from browser extensions)
        # - "expirationDate" -> "expiry" (Selenium expects integer seconds)
        # - map sameSite variants
        # - strip unsupported fields
        for key, value in cookie_dict.items():
            if key in ('expiry', 'expires', 'expirationDate'):
                try:
                    # Many exports store float seconds; Selenium accepts int
                    selenium_cookie['expiry'] = int(value)
                except Exception:
                    # Ignore invalid expiry; Selenium treats it as a session cookie
                    pass
            elif key == 'httpOnly':
                selenium_cookie['httpOnly'] = bool(value)
            elif key == 'sameSite':
                # Accept common variants: Strict/Lax/None or "no_restriction" -> "None"
                try:
                    s = str(value).strip().lower()
                except Exception:
                    s = ''
                mapped: Optional[str] = None
                if s in ('none', 'no_restriction', 'no-restriction'):
                    mapped = 'None'
                elif s == 'lax':
                    mapped = 'Lax'
                elif s == 'strict':
                    mapped = 'Strict'
                # Only set if mapped to a valid Selenium value
                if mapped:
                    selenium_cookie['sameSite'] = mapped
            elif key in ['name', 'value', 'path', 'domain', 'secure']:
                selenium_cookie[key] = value
            else:
                # Ignore extraneous keys like hostOnly, session, storeId, etc.
                continue

        # Domain normalization: if cookies were exported for twitter.com, remap to x.com
        dom = selenium_cookie.get('domain')
        if isinstance(dom, str) and dom.lstrip('.').lower() == 'twitter.com':
            selenium_cookie['domain'] = '.x.com' if dom.startswith('.') else 'x.com'

        if 'name' in selenium_cookie and 'value' in selenium_cookie:
            try:
                driver.add_cookie(selenium_cookie)
            except Exception as e:
                # Driver errors can echo the submitted cookie payload. Do not
                # place credential-bearing exception text in persistent logs.
                logger.warning(
                    "Could not add a cookie (%s); check domain, sameSite, and expiry.",
                    type(e).__name__,
                )

    # NB: no driver.refresh() here. add_cookie takes effect immediately, and
    # the caller (BrowserManager.get_driver) navigates to /home right after —
    # that load already sends the cookies. The refresh was a redundant full
    # page load on every cold start (3-4 visible reloads; live finding, v2.3).
