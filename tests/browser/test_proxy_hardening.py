"""Proxy configuration and legacy launch tests: synthetic URLs, no network."""
import hashlib
import json
from types import SimpleNamespace

import pytest
from selenium.webdriver.chrome.options import Options as ChromeOptions
from selenium.webdriver.firefox.options import Options as FirefoxOptions

from xuse.core.browser_manager import options as browser_options
from xuse.core.browser_manager.service import BrowserManager
from xuse.utils import proxy_manager
from xuse.utils.proxy_manager import ProxyManager, validate_proxy_url


@pytest.mark.parametrize('value', [
    None, 0, False, [], {}, '', ' ', 'none', 'false', 'direct://',
    'ftp://private-user:private-secret@proxy.invalid',
    'http://proxy.invalid:0', 'http://proxy.invalid:65536', 'http://proxy.invalid:abc',
    'http://proxy.invalid:', 'http://:8080', 'http://proxy.invalid/private-secret',
    'http://proxy.invalid?private-secret', 'http://proxy.invalid#private-secret',
    'http://proxy.invalid?', 'http://proxy.invalid#', 'http://proxy.invalid\\evil',
    'http://private-user:pri vate-secret@proxy.invalid',
    'http://private-user:private@secret@proxy.invalid',
    'http://private-user:%0asecret@proxy.invalid', 'http://private-user:%zz@proxy.invalid',
    'http://:private-secret@proxy.invalid', 'http://[::1', 'http://[fe80::1%25eth0]',
    'http://proxy.invalid:80,direct://', '\nhttp://proxy.invalid',
    'http://proxy.invalid\r\n', 'http://' + 'x' * 64 + '.invalid',
    'http://${NOT CLOSED}:80', 'http://proxy.invalid/' + 'x' * 8200,
])
def test_invalid_proxy_syntax_is_rejected_without_echoing_input(value):
    with pytest.raises(ValueError) as caught:
        validate_proxy_url(value)
    assert 'private' not in str(caught.value)


@pytest.mark.parametrize('value', [
    'http://proxy.invalid', 'https://proxy.invalid:443/', 'socks4://proxy.invalid:1080',
    'socks5://127.0.0.1:1080', 'http://[::1]:80', 'http://proxy_internal:8080',
    'http://private-user:percent%40encoded%2Fsecret@proxy.invalid:80',
    'http://${XUSE_HOST}:${XUSE_PORT}', 'https://${XUSE_USER}:${XUSE_SECRET}@proxy.invalid',
    'http://[${XUSE_IPV6}]:80',
])
def test_valid_config_and_environment_templates_remain_accepted(value):
    assert validate_proxy_url(value) == value


def manager(settings=None):
    return ProxyManager(SimpleNamespace(get_settings=lambda: {'browser_settings': settings or {}}))


@pytest.mark.parametrize('value', [False, 0, [], {}, ' ', 'none', 'false'])
def test_invalid_account_override_cannot_silently_inherit_global_or_go_direct(value):
    with pytest.raises(ValueError):
        manager({'proxy': 'http://global.invalid:80'}).resolve(value, 'account')


@pytest.mark.parametrize('value', ['\nhttp://proxy.invalid:80', 'http://proxy.invalid:80\n', 'pool:work\n'])
def test_resolution_does_not_trim_away_control_characters(value):
    with pytest.raises(ValueError):
        manager({'proxy_pools': {'work': ['http://proxy.invalid:80']}}).resolve(value, 'account')


@pytest.mark.parametrize('value', [None, ''])
def test_empty_account_value_uses_global_fallback(value):
    assert manager({'proxy': 'http://global.invalid:80'}).resolve(value, 'account') == 'http://global.invalid:80'
    assert manager().resolve(value, 'account') is None


def test_explicit_account_route_wins_over_invalid_global():
    assert manager({'proxy': 'malformed-global'}).resolve('http://account.invalid:80') == 'http://account.invalid:80'


@pytest.mark.parametrize('pool', [None, {}, [], 'http://proxy.invalid', [None], [''], [False]])
def test_missing_or_malformed_pool_never_resolves_direct(pool):
    with pytest.raises(ValueError):
        manager({'proxy_pools': {'work': pool}}).resolve('pool:work', 'account')


@pytest.mark.parametrize('replacement', [None, '', 'private-secret/invalid', 'private-secret\n', 'x' * 8200])
def test_environment_expansion_is_required_bounded_and_secret_safe(monkeypatch, replacement):
    if replacement is None:
        monkeypatch.delenv('XUSE_PROXY_SYNTHETIC', raising=False)
    else:
        monkeypatch.setenv('XUSE_PROXY_SYNTHETIC', replacement)
    with pytest.raises(ValueError) as caught:
        manager().resolve('http://${XUSE_PROXY_SYNTHETIC}:80')
    assert 'private-secret' not in str(caught.value)


def test_pool_selection_is_stable_and_validates_expanded_selected_route(monkeypatch):
    monkeypatch.setenv('XUSE_PROXY_SYNTHETIC', 'first.invalid')
    choices = ['http://${XUSE_PROXY_SYNTHETIC}:80', 'http://second.invalid:80']
    pool = manager({'proxy_pools': {'work': choices}})
    for account in ('alpha', 'beta', 'gamma'):
        chosen = choices[int(hashlib.sha256(account.encode()).hexdigest(), 16) % 2]
        expected = chosen.replace('${XUSE_PROXY_SYNTHETIC}', 'first.invalid')
        assert pool.resolve('pool:work', account) == expected
        assert pool.resolve('pool:work', account) == expected


def test_failed_round_robin_choice_does_not_advance_cursor(tmp_path):
    path = tmp_path / 'proxy-state.json'
    path.write_text('{"work": 0}')
    pool = manager({'proxy_pools': {'work': ['http://proxy.invalid:0', 'http://other.invalid:80']},
                    'proxy_pool_strategy': 'round_robin', 'proxy_pool_state_file': str(path)})
    with pytest.raises(ValueError):
        pool.resolve('pool:work', 'account')
    assert json.loads(path.read_text()) == {'work': 0}


def test_failed_round_robin_state_replace_preserves_old_cursor(tmp_path, monkeypatch):
    path = tmp_path / 'proxy-state.json'
    path.write_text('{"work": 0}')
    pool = manager({'proxy_pools': {'work': ['http://proxy.invalid:80', 'http://other.invalid:80']},
                    'proxy_pool_strategy': 'round_robin', 'proxy_pool_state_file': str(path)})
    def fail(*args):
        raise OSError('private-filesystem-secret')
    monkeypatch.setattr(proxy_manager.os, 'replace', fail)
    with pytest.raises(ValueError) as caught:
        pool.resolve('pool:work', 'account')
    assert 'private-filesystem-secret' not in str(caught.value)
    assert json.loads(path.read_text()) == {'work': 0}
    assert list(tmp_path.glob('*.tmp')) == []


def test_preview_routes_never_reads_or_writes_round_robin_state(tmp_path, monkeypatch):
    path = tmp_path / 'no-created-directory' / 'state.json'
    pool = manager({'proxy': 'pool:work', 'proxy_pools': {'work': ['http://one.invalid', 'socks5://two.invalid']},
                    'proxy_pool_strategy': 'round_robin', 'proxy_pool_state_file': str(path)})
    def forbidden(*args):
        pytest.fail('Configuration preview touched rotation state')
    monkeypatch.setattr(pool, '_load_state', forbidden)
    monkeypatch.setattr(pool, '_save_state', forbidden)
    assert pool.preview_routes() == ['http://one.invalid', 'socks5://two.invalid']
    assert not path.parent.exists()


def test_preview_validates_every_candidate_without_selecting_a_healthy_member():
    pool = manager({'proxy_pools': {'work': ['http://valid.invalid', 'http://invalid.invalid:0']}})
    with pytest.raises(ValueError):
        pool.preview_routes('pool:work')


def launch_options(browser, proxy, additional=None):
    options = ChromeOptions() if browser == 'chrome' else FirefoxOptions()
    return browser_options.configure_driver_options(
        options, browser, proxy=proxy, headless=True, window_size=None,
        additional_options=additional, custom_user_agent='synthetic-fixture')


@pytest.mark.parametrize('browser', ['chrome', 'firefox'])
@pytest.mark.parametrize('url', [
    'http://private-user:private-secret@proxy.invalid:80',
    'socks5://private-user:private-secret@proxy.invalid:1080',
])
def test_legacy_proxy_auth_fails_before_credentials_reach_browser_options(browser, url, caplog):
    with pytest.raises(ValueError, match='authentication is unsupported') as caught:
        launch_options(browser, url)
    assert 'private-secret' not in str(caught.value) + caplog.text
    assert 'private-user' not in str(caught.value) + caplog.text


@pytest.mark.parametrize('browser', ['chrome', 'firefox'])
@pytest.mark.parametrize('url', ['http://proxy.invalid:0', 'http://proxy.invalid/path', False, 'none'])
def test_legacy_invalid_proxy_never_becomes_direct_options(browser, url):
    with pytest.raises(ValueError):
        launch_options(browser, url)


@pytest.mark.parametrize('scheme,port', [('http', 80), ('https', 443), ('socks4', 1080), ('socks5', 1080)])
def test_chrome_proxy_default_ports_and_webrtc_route_policy(scheme, port):
    options = launch_options('chrome', f'{scheme}://proxy.invalid')
    assert f'--proxy-server={scheme}://proxy.invalid:{port}' in options.arguments
    assert '--webrtc-ip-handling-policy=disable_non_proxied_udp' in options.arguments


def test_firefox_http_default_port_is_configured_instead_of_silently_direct():
    options = launch_options('firefox', 'http://proxy.invalid')
    assert options.preferences['network.proxy.type'] == 1
    assert options.preferences['network.proxy.http'] == 'proxy.invalid'
    assert options.preferences['network.proxy.http_port'] == 80
    assert options.preferences['network.proxy.ssl_port'] == 80


def test_firefox_socks5_uses_remote_dns_without_enabling_direct_webrtc():
    options = launch_options('firefox', 'socks5://proxy.invalid')
    assert options.preferences['network.proxy.socks_port'] == 1080
    assert options.preferences['network.proxy.socks_remote_dns'] is True
    assert options.preferences['media.peerconnection.enabled'] is False


def test_firefox_tls_proxy_is_not_silently_downgraded_to_plaintext():
    with pytest.raises(ValueError, match='TLS connection'):
        launch_options('firefox', 'https://proxy.invalid:443')


@pytest.mark.parametrize('override', ['--proxy-server=http://other.invalid', '--no-proxy-server',
    '--proxy-pac-url=http://config.invalid', '--proxy-bypass-list=*', '--proxy-auto-detect'])
@pytest.mark.parametrize('proxy', [None, 'http://proxy.invalid:80'])
def test_additional_options_cannot_override_proxy_route(override, proxy):
    with pytest.raises(ValueError, match='not additional browser options'):
        launch_options('chrome', proxy, [override])


def test_browser_manager_resolves_global_and_rejects_false_override_before_launch(make_config_loader, tmp_path):
    settings = {'browser_settings': {'proxy': 'http://global.invalid:80',
                'webdriver_manager_cache_path': str(tmp_path / 'driver-cache')}}
    config = make_config_loader(settings=settings)
    assert BrowserManager({'account_id': 'a'}, config).effective_proxy == 'http://global.invalid:80'
    with pytest.raises(ValueError):
        BrowserManager({'account_id': 'a', 'proxy': False}, config)


@pytest.mark.parametrize('field', ['cookies', 'cookie_file_path'])
def test_legacy_cookie_source_is_relative_to_accounts_configuration(make_config_loader, tmp_path, field):
    import xuse.core.browser_manager.service as service
    account_home = tmp_path / 'isolated-account-home'
    account_home.mkdir()
    (account_home / 'private-session.json').write_text(json.dumps([
        {'name': 'auth_token', 'value': 'synthetic'}, {'name': 'ct0', 'value': 'synthetic'}]))
    config = make_config_loader(settings={'browser_settings': {
        'webdriver_manager_cache_path': str(tmp_path / 'driver-cache')}})
    config.accounts_file = account_home / 'accounts.json'
    instance = service.BrowserManager({'account_id': 'a', field: 'private-session.json'}, config)
    assert instance.cookies_data and len(instance.cookies_data) == 2
