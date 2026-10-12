"""Guard privileged contribution automation against executing PR-controlled code."""
from pathlib import Path
import re

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = yaml.safe_load((ROOT / '.github/workflows/contributor-triage.yml').read_text(encoding='utf-8'))
LABELS = yaml.safe_load((ROOT / '.github/labeler.yml').read_text(encoding='utf-8'))


def test_privileged_pr_labeler_is_main_only_and_never_runs_repository_code():
    triggers = WORKFLOW.get('on', WORKFLOW.get(True))
    assert set(triggers) == {'pull_request_target', 'issues'}
    assert triggers['pull_request_target']['branches'] == ['main']
    assert set(triggers['pull_request_target']['types']) == {
        'opened', 'reopened', 'synchronize', 'edited', 'ready_for_review'}
    assert WORKFLOW['permissions'] == {}
    job = WORKFLOW['jobs']['label-pull-request']
    assert job['if'] == "github.event_name == 'pull_request_target' && github.event.pull_request.base.ref == 'main'"
    assert job['permissions'] == {'contents': 'read', 'pull-requests': 'write'}
    assert job['timeout-minutes'] <= 5
    assert len(job['steps']) == 1
    step = job['steps'][0]
    assert re.fullmatch(r'actions/labeler@[0-9a-f]{40}', step['uses'])
    assert 'run' not in step
    assert step['with'] == {'repo-token': '${{ github.token }}',
                            'configuration-path': '.github/labeler.yml', 'sync-labels': True}
    # Neither checkout, local actions, artifacts nor shell execution belong in
    # this write-token workflow. The action gets the base configuration by API.
    for configured_job in WORKFLOW['jobs'].values():
        for configured_step in configured_job['steps']:
            assert 'run' not in configured_step
            assert re.fullmatch(r'actions/(?:labeler|github-script)@[0-9a-f]{40}', configured_step['uses'])


def test_issue_triage_only_adds_a_fixed_label_and_preserves_contributor_content():
    triggers = WORKFLOW.get('on', WORKFLOW.get(True))
    assert triggers['issues']['types'] == ['opened', 'reopened']
    job = WORKFLOW['jobs']['triage-issue']
    assert job['if'] == "github.event_name == 'issues'"
    assert job['permissions'] == {'issues': 'write'}
    assert job['timeout-minutes'] <= 5
    assert len(job['steps']) == 1
    step = job['steps'][0]
    assert re.fullmatch(r'actions/github-script@[0-9a-f]{40}', step['uses'])
    script = step['with']['script']
    assert '${{' not in script
    # An allowlisted single API operation cannot comment, close, replace labels,
    # interpret issue text, import a file, or execute a subprocess.
    assert re.fullmatch(
        r"await github\.rest\.issues\.addLabels\(\{\s*"
        r"owner: context\.repo\.owner,\s*repo: context\.repo\.repo,\s*"
        r"issue_number: context\.issue\.number,\s*labels: \['needs-triage'\]\s*\}\);\s*",
        script,
    )


@pytest.mark.parametrize('label,required_patterns', [
    ('documentation', {'*.md', 'docs/**', 'src/xuse/skills_pack/**'}),
    ('tests', {'tests/**', 'scripts/audit_*.py'}),
    ('browser', {'src/xuse/browser/**', 'src/xuse/core/browser_manager/**', 'tests/browser/**'}),
    ('mcp', {'src/xuse/mcp/**', 'tests/mcp/**', 'server.json'}),
    ('dependencies', {'pyproject.toml', 'uv.lock', '.github/dependabot.yml'}),
    ('ci', {'.github/workflows/**', '.github/labeler.yml'}),
])
def test_path_label_configuration_uses_supported_file_rules(label, required_patterns):
    assert set(LABELS) == {'documentation', 'tests', 'browser', 'mcp', 'dependencies', 'ci'}
    entries = LABELS[label]
    assert len(entries) == 1 and set(entries[0]) == {'changed-files'}
    rules = entries[0]['changed-files']
    assert len(rules) == 1 and set(rules[0]) == {'any-glob-to-any-file'}
    patterns = rules[0]['any-glob-to-any-file']
    assert required_patterns <= set(patterns)
    assert all(isinstance(pattern, str) and pattern and '${{' not in pattern for pattern in patterns)
