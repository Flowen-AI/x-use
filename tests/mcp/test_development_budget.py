"""Development unlock allowance must not reset history or weaken other controls."""
import pytest

from xuse.mcp.safety import PolicyError, SafetyStore


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch):
    monkeypatch.delenv('XUSE_ENV', raising=False)
    monkeypatch.delenv('XUSE_DEV_UNLOCK_DAILY_CAP', raising=False)


def policy(tmp_path, **settings):
    return SafetyStore(tmp_path / 'safety.sqlite3', {
        'read_interval_seconds': 0, 'max_actions_per_minute': 100, **settings}, clock=lambda: 100000)


def test_development_budget_preserves_attempts_and_production_default(tmp_path, monkeypatch):
    original = policy(tmp_path)
    for _ in range(5):
        action = original.reserve('a', 'unlock')
        original.finish('a', action, 'failed')
    with pytest.raises(PolicyError, match='budget exhausted'):
        original.reserve('a', 'unlock')
    monkeypatch.setenv('XUSE_ENV', 'development')
    monkeypatch.setenv('XUSE_DEV_UNLOCK_DAILY_CAP', '20')
    dev = policy(tmp_path)
    status = dev.status('a')
    assert status['daily_remaining']['unlock'] == 15
    assert status['daily_used']['unlock'] == 5
    assert status['daily_reset_at'] == 172800
    assert status['development_unlock_override'] == 20
    assert {k: v for k, v in dev.caps.items() if k != 'unlock'} == {k: v for k, v in original.caps.items() if k != 'unlock'}
    dev.finish('a', dev.reserve('a', 'unlock'), 'succeeded')
    monkeypatch.delenv('XUSE_DEV_UNLOCK_DAILY_CAP')
    restored = policy(tmp_path)
    assert restored.caps['unlock'] == 5
    assert restored.status('a')['daily_used']['unlock'] == 6
    assert restored.status('a')['daily_remaining']['unlock'] == 0
    with pytest.raises(PolicyError):
        restored.reserve('a', 'unlock')


@pytest.mark.parametrize('mode', ['', 'production', 'test', 'Development'])
def test_override_requires_exact_development_mode(tmp_path, monkeypatch, mode):
    monkeypatch.setenv('XUSE_ENV', mode)
    monkeypatch.setenv('XUSE_DEV_UNLOCK_DAILY_CAP', '20')
    with pytest.raises(ValueError, match='requires'):
        policy(tmp_path)


@pytest.mark.parametrize('cap', ['', '0', '-1', '51', '500', '1.5', 'true', ' 20', '2e1'])
def test_invalid_override_fails_closed(tmp_path, monkeypatch, cap):
    monkeypatch.setenv('XUSE_ENV', 'development')
    monkeypatch.setenv('XUSE_DEV_UNLOCK_DAILY_CAP', cap)
    with pytest.raises(ValueError, match='integer'):
        policy(tmp_path)


@pytest.mark.parametrize('reason', ['challenge', 'rate_limited', 'manual_pause', 'account_locked'])
def test_development_does_not_bypass_pauses(tmp_path, monkeypatch, reason):
    monkeypatch.setenv('XUSE_ENV', 'development')
    monkeypatch.setenv('XUSE_DEV_UNLOCK_DAILY_CAP', '20')
    dev = policy(tmp_path)
    dev.pause('a', reason)
    with pytest.raises(PolicyError) as error:
        dev.reserve('a', 'unlock')
    assert error.value.reason == reason


def test_override_cannot_enable_disabled_unlock(tmp_path, monkeypatch):
    monkeypatch.setenv('XUSE_ENV', 'development')
    monkeypatch.setenv('XUSE_DEV_UNLOCK_DAILY_CAP', '20')
    with pytest.raises(ValueError, match='disabled'):
        policy(tmp_path, daily_caps={'unlock': 0})


def test_development_retains_cooldown_and_minute_limits(tmp_path, monkeypatch):
    monkeypatch.setenv('XUSE_ENV', 'development')
    monkeypatch.setenv('XUSE_DEV_UNLOCK_DAILY_CAP', '20')
    dev = policy(tmp_path, read_interval_seconds=2)
    dev.reserve('a', 'unlock')
    with pytest.raises(PolicyError) as error:
        dev.reserve('a', 'unlock')
    assert error.value.reason == 'cooldown'
    limited = policy(tmp_path, max_actions_per_minute=1)
    with pytest.raises(PolicyError) as error:
        limited.reserve('a', 'unlock')
    assert error.value.reason == 'minute_budget'
