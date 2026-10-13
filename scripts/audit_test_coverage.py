"""Trace MCP wrapper outcomes in passing pytest cases, without recording payloads.

This is functional invocation evidence, not line/branch coverage or live-X proof.
Usage: python scripts/audit_test_coverage.py --report report.json --inventory protocol.json -- <pytest args>
"""
import argparse
import json
from pathlib import Path
from unittest.mock import patch

import pytest
from mcp.server.mcpserver import MCPServer


class Observer:
    def __init__(self):
        self.current = None
        self.calls = []
        self.outcomes = {}

    def pytest_runtest_setup(self, item):
        self.current = item.nodeid

    def pytest_runtest_logreport(self, report):
        self.outcomes.setdefault(report.nodeid, {})[report.when] = report.outcome


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report', type=Path, required=True)
    parser.add_argument('--inventory', type=Path, required=True)
    options, arguments = parser.parse_known_args()
    protocol = json.loads(options.inventory.read_text(encoding='utf-8'))
    names = protocol['tools']
    stdio_successes = {call['tool'] for call in protocol['tool_calls'] if call['ok']}
    observer = Observer()
    original = MCPServer.call_tool

    async def observed(server, name, *args, **kwargs):
        record = {'tool': name, 'node': observer.current, 'outcome': 'raised'}
        try:
            result = await original(server, name, *args, **kwargs)
            content = result.content if hasattr(result, 'content') else result[0] if isinstance(result, tuple) else result
            try:
                payload = json.loads(content[0].text)
                record['outcome'] = 'ok' if payload.get('ok') is True else 'error'
            except (AttributeError, IndexError, TypeError, ValueError):
                record['outcome'] = 'unclassified'
            return result
        finally:
            observer.calls.append(record)

    with patch.object(MCPServer, 'call_tool', observed):
        code = pytest.main(arguments[1:] if arguments[:1] == ['--'] else arguments, plugins=[observer])
    rows = []
    for name in sorted(names):
        calls = [call for call in observer.calls if call['tool'] == name and
                 observer.outcomes.get(call['node'], {}).get('call') == 'passed' and
                 all(v == 'passed' for v in observer.outcomes.get(call['node'], {}).values())]
        rows.append({'tool': name, 'calls_in_passing_tests': len(calls),
                     'successful_stdio_call': name in stdio_successes,
                     'outcomes': {key: sum(c['outcome'] == key for c in calls)
                                  for key in ('ok', 'error', 'raised', 'unclassified')},
                     'tests': sorted({c['node'].split('[')[0] for c in calls})})
    report = {'pytest_exit_code': int(code), 'scope': 'in-process MCPServer.call_tool in passing isolated tests',
              'live_X_verified': False, 'arguments_or_payloads_recorded': False,
              'tool_count': len(rows), 'tools': rows}
    report['missing_positive_evidence'] = [row['tool'] for row in rows
                                         if not row['outcomes']['ok'] and not row['successful_stdio_call']]
    report['all_tools_have_positive_evidence'] = not report['missing_positive_evidence']
    options.report.parent.mkdir(parents=True, exist_ok=True)
    options.report.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    return int(code) or (1 if report['missing_positive_evidence'] else 0)


if __name__ == '__main__':
    raise SystemExit(main())
