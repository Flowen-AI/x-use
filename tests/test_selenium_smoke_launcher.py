"""Owned-process smoke cleanup, without launching browsers or killing anything."""
import importlib.util
from pathlib import Path
import signal
import subprocess
from unittest.mock import MagicMock

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / 'scripts' / 'selenium_browser_smoke.py'
SPEC = importlib.util.spec_from_file_location('selenium_smoke_launcher', SCRIPT)
smoke = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(smoke)


@pytest.fixture
def launcher(monkeypatch):
    process = MagicMock(pid=43127)
    popen = MagicMock(return_value=process)
    taskkill = MagicMock()
    killpg = MagicMock()
    monkeypatch.setattr(smoke.subprocess, 'Popen', popen)
    monkeypatch.setattr(smoke.subprocess, 'run', taskkill)
    monkeypatch.setattr(smoke.subprocess, 'CREATE_NO_WINDOW', 0x08000000, raising=False)
    monkeypatch.setattr(smoke.os, 'killpg', killpg, raising=False)
    # The simulated POSIX branch also needs its signal constant on Windows.
    monkeypatch.setattr(smoke.signal, 'SIGKILL', 9, raising=False)
    return process, popen, taskkill, killpg


@pytest.mark.parametrize('platform', ['win32', 'linux', 'darwin'])
def test_success_launch_has_platform_specific_ownership(launcher, platform):
    process, popen, taskkill, killpg = launcher
    process.communicate.return_value = ('{"status":"passed"}\n', '')
    assert smoke._run_worker(['python', 'worker.py'], 500, platform=platform) == {'status': 'passed'}
    assert popen.call_args.kwargs['start_new_session'] is (platform != 'win32')
    assert popen.call_args.kwargs['creationflags'] == (0x08000000 if platform == 'win32' else 0)
    process.communicate.assert_called_once_with(timeout=180)
    taskkill.assert_not_called()
    killpg.assert_not_called()


@pytest.mark.parametrize('platform', ['win32', 'linux', 'darwin'])
def test_timeout_terminates_only_owned_tree_or_group(launcher, platform):
    process, popen, taskkill, killpg = launcher
    process.communicate.side_effect = [subprocess.TimeoutExpired('worker', 1), ('', '')]
    result = smoke._run_worker(['python', 'worker.py'], -1, platform=platform)
    assert result == {'status': 'unavailable', 'error_type': 'BoundedStartupOrSmokeTimeout', 'external_actions': 0}
    assert [call.kwargs['timeout'] for call in process.communicate.call_args_list] == [1, 10]
    if platform == 'win32':
        taskkill.assert_called_once_with(
            ['taskkill', '/PID', '43127', '/T', '/F'], capture_output=True, check=False, timeout=10,
        )
        killpg.assert_not_called()
    else:
        assert popen.call_args.kwargs['start_new_session'] is True
        killpg.assert_called_once_with(43127, signal.SIGKILL)
        taskkill.assert_not_called()
    process.kill.assert_not_called()


def test_posix_group_exit_race_still_reaps_worker(launcher):
    process, _popen, taskkill, killpg = launcher
    process.communicate.side_effect = [subprocess.TimeoutExpired('worker', 1), ('', '')]
    killpg.side_effect = ProcessLookupError()
    result = smoke._run_worker(['python', 'worker.py'], 1, platform='linux')
    assert 'cleanup_error' not in result
    assert process.communicate.call_args.kwargs == {'timeout': 10}
    taskkill.assert_not_called()


@pytest.mark.parametrize('platform', ['win32', 'linux'])
def test_inherited_pipe_timeout_does_not_leave_unbounded_wait(launcher, platform):
    process, _popen, _taskkill, _killpg = launcher
    process.communicate.side_effect = subprocess.TimeoutExpired('worker', 1)
    result = smoke._run_worker(['python', 'worker.py'], 1, platform=platform)
    assert result['cleanup_error'] == 'CleanupTimeout'
    process.kill.assert_called_once_with()
    process.stdout.close.assert_called_once_with()
    process.stderr.close.assert_called_once_with()
    process.wait.assert_called_once_with(timeout=5)
