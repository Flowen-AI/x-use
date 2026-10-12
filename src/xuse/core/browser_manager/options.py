import logging
from typing import Union, Optional
from urllib.parse import urlsplit

from selenium.webdriver.chrome.options import Options as ChromeOptions
from selenium.webdriver.firefox.options import Options as FirefoxOptions

from .ua import get_user_agent
from xuse.utils.proxy_manager import validate_proxy_url

logger = logging.getLogger(__name__)


def validate_legacy_proxy(proxy: Optional[str], browser_type: str, additional_options=None):
    """Side-effect-free launch compatibility check; parsed credentials stay private."""
    if isinstance(additional_options, list) and any(
            isinstance(option, str) and option.strip().lower().lstrip('-').split('=', 1)[0] in {
                'proxy-server', 'proxy-pac-url', 'proxy-auto-detect',
                'no-proxy-server', 'proxy-bypass-list'}
            for option in additional_options):
        raise ValueError("Configure proxy routing through account or browser proxy settings, not additional browser options.")
    proxy_parts = None
    if proxy is not None and proxy != "":
        proxy_parts = urlsplit(validate_proxy_url(proxy, allow_environment=False))
        # These legacy launch options have no authentication callback. Chrome
        # explicitly ignores URL userinfo in manual proxy settings; Firefox's
        # host/port preferences cannot carry it either. Reject before launch.
        if proxy_parts.username is not None or proxy_parts.password is not None:
            raise ValueError("Authenticated proxies require the patchright or playwright backend; legacy Selenium proxy authentication is unsupported.")
        if browser_type not in ('chrome', 'firefox'):
            raise ValueError("Proxy configuration is unsupported for this legacy browser.")
        if browser_type == 'firefox' and proxy_parts.scheme == 'https':
            raise ValueError("The legacy Firefox proxy preferences do not support a TLS connection to the proxy; use HTTP/SOCKS or the async browser backend.")
    return proxy_parts


def configure_driver_options(
    options: Union[ChromeOptions, FirefoxOptions],
    browser_type: str,
    *,
    headless: bool,
    window_size: Optional[str],
    proxy: Optional[str],
    additional_options: Optional[list],
    custom_user_agent: Optional[str] = None,
    for_undetected: bool = False,
) -> Union[ChromeOptions, FirefoxOptions]:
    proxy_parts = validate_legacy_proxy(proxy, browser_type, additional_options)
    user_agent = get_user_agent(custom_user_agent)
    if browser_type == 'chrome':
        # Chromium only treats '--'-prefixed tokens as switches; a bare
        # 'user-agent=...' token is parsed as a positional URL argument.
        # undetected-chromedriver manages its own User-Agent — passing one
        # through its capability flow can conflict, so leave UC alone.
        if not for_undetected:
            options.add_argument(f"--user-agent={user_agent}")
    elif browser_type == 'firefox':
        # Firefox has no user-agent CLI switch; the override preference is
        # the only mechanism.
        options.set_preference("general.useragent.override", user_agent)

    if headless:
        if browser_type == 'chrome':
            options.add_argument('--headless=new')
        else:
            options.add_argument('--headless')
        options.add_argument('--disable-gpu')

    if window_size:
        options.add_argument(f"--window-size={window_size}")

    if proxy_parts is not None:
        scheme = proxy_parts.scheme
        host = proxy_parts.hostname
        port = proxy_parts.port or {'http': 80, 'https': 443, 'socks4': 1080, 'socks5': 1080}[scheme]
        if browser_type == 'chrome':
            authority = f'[{host}]' if ':' in host else host
            options.add_argument(f"--proxy-server={scheme}://{authority}:{port}")
            options.add_argument('--webrtc-ip-handling-policy=disable_non_proxied_udp')
            options.add_argument('--force-webrtc-ip-handling-policy=disable_non_proxied_udp')
        elif browser_type == 'firefox':
            options.set_preference('network.proxy.type', 1)
            options.set_preference('network.proxy.no_proxies_on', '')
            options.set_preference('media.peerconnection.enabled', False)
            if scheme.startswith('socks'):
                options.set_preference('network.proxy.socks', host)
                options.set_preference('network.proxy.socks_port', port)
                options.set_preference('network.proxy.socks_version', 4 if scheme == 'socks4' else 5)
                options.set_preference('network.proxy.socks_remote_dns', scheme == 'socks5')
            else:
                options.set_preference('network.proxy.http', host)
                options.set_preference('network.proxy.http_port', port)
                options.set_preference('network.proxy.ssl', host)
                options.set_preference('network.proxy.ssl_port', port)

    # Reduce automation fingerprints for Chrome — plain chromedriver only.
    # undetected-chromedriver applies its own equivalents internally, and
    # passing excludeSwitches through its capability flow makes modern
    # chromedriver reject the session ("unrecognized chrome option").
    if browser_type == 'chrome' and not for_undetected:
        try:
            options.add_experimental_option("excludeSwitches", ["enable-automation"])
            options.add_experimental_option('useAutomationExtension', False)
            options.add_argument("--disable-blink-features=AutomationControlled")
        except Exception:
            pass

    if isinstance(additional_options, list):
        for opt in additional_options:
            if isinstance(opt, str):
                options.add_argument(opt)
            else:
                logger.warning("Ignoring a non-string driver option.")
    elif additional_options is not None:
        logger.warning("'driver_options' in config is not a list.")

    return options

