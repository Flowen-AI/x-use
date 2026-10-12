"""Security checks must remain mandatory, fork-safe and limited in privilege."""
import re
from pathlib import Path

import yaml


PATH = Path(__file__).resolve().parents[1] / ".github/workflows/security-review.yml"
# BaseLoader preserves GitHub's `on` key and expression/boolean strings as data.
WORKFLOW = yaml.load(PATH.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)


def test_required_security_checks_have_no_path_or_actor_bypass():
    events = WORKFLOW["on"]
    assert set(events) == {"pull_request", "push", "schedule", "workflow_dispatch"}
    assert events["pull_request"] == {"branches": ["main"]}
    assert events["push"] == {"branches": ["main"]}
    assert len(events["schedule"]) == 1
    assert len(events["schedule"][0]["cron"].split()) == 5
    jobs = WORKFLOW["jobs"]
    assert jobs["codeql"]["name"] == "CodeQL / Python"
    assert jobs["dependency-review"]["name"] == "Dependency review"
    assert "if" not in jobs["codeql"]
    assert jobs["dependency-review"]["if"] == "github.event_name == 'pull_request'"
    assert all("if" not in step for job in jobs.values() for step in job["steps"])


def test_security_jobs_cannot_execute_project_code_or_expose_write_credentials():
    assert WORKFLOW["permissions"] == {"contents": "read"}
    assert "secrets." not in PATH.read_text(encoding="utf-8")
    allowed_actions = {
        "actions/checkout", "github/codeql-action/init",
        "github/codeql-action/analyze", "actions/dependency-review-action",
    }
    for job_id, job in WORKFLOW["jobs"].items():
        expected = {"contents": "read"}
        if job_id == "codeql":
            expected["security-events"] = "write"
        assert job["permissions"] == expected
        assert job["runs-on"] == "ubuntu-latest"
        assert 0 < int(job["timeout-minutes"]) <= 20
        assert "continue-on-error" not in job
        for step in job["steps"]:
            assert "run" not in step
            assert "continue-on-error" not in step
            action, sha = step["uses"].split("@")
            assert action in allowed_actions
            assert re.fullmatch(r"[0-9a-f]{40}", sha)
            if action == "actions/checkout":
                assert step["with"] == {"persist-credentials": "false"}


def test_codeql_uses_interpreted_security_extended_analysis_and_waits_for_results():
    steps = WORKFLOW["jobs"]["codeql"]["steps"]
    init = next(step for step in steps if step["uses"].startswith("github/codeql-action/init@"))
    analyze = next(step for step in steps if step["uses"].startswith("github/codeql-action/analyze@"))
    assert init["with"] == {"languages": "python", "build-mode": "none", "queries": "security-extended"}
    assert init["uses"].split("@", 1)[1] == analyze["uses"].split("@", 1)[1]
    assert analyze["with"]["wait-for-processing"] == "true"
    assert analyze["with"]["category"] == "/language:python"
    assert analyze["with"].get("upload", "always") == "always"


def test_dependency_review_blocks_all_new_advisory_severities_and_scopes():
    step = next(step for step in WORKFLOW["jobs"]["dependency-review"]["steps"]
                if step["uses"].startswith("actions/dependency-review-action@"))
    options = step["with"]
    assert options["fail-on-severity"] == "low"
    assert {scope.strip() for scope in options["fail-on-scopes"].split(",")} == {"runtime", "development", "unknown"}
    assert options["vulnerability-check"] == "true"
    assert options["warn-only"] == "false"
    assert options["comment-summary-in-pr"] == "never"
    assert options["retry-on-snapshot-warnings"] == "true"
    assert 0 < int(options["retry-on-snapshot-warnings-timeout"]) <= 120
    assert not {"allow-ghsas", "base-ref", "head-ref", "config-file"} & options.keys()
