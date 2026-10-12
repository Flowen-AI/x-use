"""Actual OS locks, including abnormal process exit; no browser/network."""
import subprocess
import sys

import pytest

from xuse.mcp.ownership import server_home_lock


def test_same_home_excludes_second_owner_and_releases_on_error(tmp_path):
    with pytest.raises(ValueError):
        with server_home_lock(tmp_path):
            with pytest.raises(RuntimeError, match='Another x-use MCP server'):
                with server_home_lock(tmp_path / 'child' / '..'):
                    pytest.fail('second owner entered')
            with server_home_lock(tmp_path / 'other'):
                pass
            raise ValueError('synthetic')
    with server_home_lock(tmp_path):
        pass


def test_process_exit_releases_server_ownership(tmp_path):
    code = ('import sys; from xuse.mcp.ownership import server_home_lock; '
            'lock=server_home_lock(sys.argv[1]); lock.__enter__(); '
            'print("locked",flush=True); sys.stdin.read()')
    child = subprocess.Popen([sys.executable, '-c', code, str(tmp_path)],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == 'locked'
        with pytest.raises(RuntimeError, match='Another x-use MCP server'):
            with server_home_lock(tmp_path):
                pytest.fail('second owner entered')
    finally:
        child.kill()
        child.communicate(timeout=10)
    with server_home_lock(tmp_path):
        pass
