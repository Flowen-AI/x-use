"""Audit-owned temporary aliases resolve before private state or child launch."""
from contextlib import asynccontextmanager, contextmanager
import importlib.util
import json
from pathlib import Path

import pytest

from xuse.core.local_state import PrivateStateError, private_state_file


SCRIPT = Path(__file__).resolve().parents[1] / 'scripts' / 'audit_mcp.py'
SPEC = importlib.util.spec_from_file_location('audit_mcp_launcher', SCRIPT)
audit_script = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(audit_script)


@pytest.mark.parametrize('real_link', [False, True], ids=['mapped-temp-alias', 'real-linked-parent'])
def test_owned_temp_home_is_canonical_for_state_and_stdio(tmp_path, monkeypatch, real_link):
    canonical = tmp_path.resolve() / 'real-parent' / 'audit-home'
    canonical.mkdir(parents=True)
    alias_parent = tmp_path / 'temp-alias'
    alias = alias_parent / canonical.name
    if real_link:
        try:
            alias_parent.symlink_to(canonical.parent, target_is_directory=True)
        except OSError:
            pytest.skip('Creating a directory symlink is unavailable on this host')
        # The launcher correction must not relax production path validation.
        with pytest.raises(PrivateStateError, match='links or reparse points'):
            private_state_file(alias / 'config' / 'rejected.json')
    else:
        # Deterministic coverage on Windows hosts without symlink privileges.
        resolve = Path.resolve

        def resolve_alias(path, *args, **kwargs):
            return canonical if path == alias else resolve(path, *args, **kwargs)

        monkeypatch.setattr(Path, 'resolve', resolve_alias)

    events = []

    @contextmanager
    def temporary_directory(*, prefix):
        assert prefix == 'xuse-protocol-audit-'
        events.append('temporary-open')
        yield str(alias)
        events.append('temporary-closed')

    @asynccontextmanager
    async def stdio(parameters, *, errlog):
        assert parameters.command == 'synthetic-python'
        assert parameters.args == ['-m', 'xuse.mcp.server']
        assert parameters.cwd == str(canonical)
        assert parameters.env['X_USE_HOME'] == str(canonical)
        assert 'PYTHONPATH' not in parameters.env
        assert Path(errlog.name) == canonical / 'stderr.log'
        events.append('stdio-open')
        yield 'read-stream', 'write-stream'

    class Session:
        def __init__(self, read, write):
            assert (read, write) == ('read-stream', 'write-stream')

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def initialize(self):
            events.append('initialized')

    async def audit(directory, python):
        assert directory == canonical
        assert events == ['temporary-open']
        for name in ('settings.json', 'accounts.json'):
            target = directory / 'config' / name
            private_state_file(target)
            target.write_text('{}', encoding='utf-8')
        async with audit_script.connect(directory, python):
            pass
        return {'success': True, 'tool_count': 0, 'distinct_tools_called': 0,
                'external_writes': 0}

    report = tmp_path / 'report.json'
    monkeypatch.setattr(audit_script.tempfile, 'TemporaryDirectory', temporary_directory)
    monkeypatch.setattr(audit_script, 'audit', audit)
    monkeypatch.setattr(audit_script, 'stdio_client', stdio)
    monkeypatch.setattr(audit_script, 'ClientSession', Session)
    monkeypatch.setenv('PYTHONPATH', 'must-not-reach-child')
    monkeypatch.setattr(audit_script.sys, 'argv', [str(SCRIPT), '--python', 'synthetic-python',
                                                '--report', str(report)])
    audit_script.main()
    assert json.loads(report.read_text(encoding='utf-8'))['success'] is True
    assert events == ['temporary-open', 'stdio-open', 'initialized', 'temporary-closed']
    assert (canonical / 'config' / 'settings.json').is_file()
    assert (canonical / 'config' / 'accounts.json').is_file()
