"""Execute the required CI gate with real dependency result payloads."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8"))
GATE = WORKFLOW["jobs"]["gate"]
GATE_STEP = next(step for step in GATE["steps"] if "run" in step)
GATE_SCRIPT = GATE_STEP["run"].split("python3 - <<'PY'\n", 1)[1].rsplit("\nPY", 1)[0]
REQUIRED_JOBS = set(WORKFLOW["jobs"]) - {"gate"}


def run_gate(results):
    environment = dict(os.environ, JOB_RESULTS=json.dumps(results))
    return subprocess.run(
        [sys.executable, "-c", GATE_SCRIPT],
        env=environment,
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )


def successful_results():
    return {job: {"result": "success", "outputs": {}} for job in REQUIRED_JOBS}


def test_required_gate_waits_for_every_ci_job_even_after_failure():
    assert REQUIRED_JOBS
    assert set(GATE["needs"]) == REQUIRED_JOBS
    assert len(GATE["needs"]) == len(REQUIRED_JOBS)
    assert GATE["name"] == "CI required"
    assert GATE["if"] == "always()"
    assert GATE_STEP["env"]["JOB_RESULTS"] == "${{ toJSON(needs) }}"
    assert not GATE.get("continue-on-error")
    assert not GATE_STEP.get("continue-on-error")
    # Failed matrix entries must remain failures for the dependency aggregate.
    for name in REQUIRED_JOBS:
        job = WORKFLOW["jobs"][name]
        assert not job.get("continue-on-error"), name


def test_required_gate_accepts_only_complete_success():
    result = run_gate(successful_results())
    assert result.returncode == 0, result.stderr
    assert "All required CI jobs passed." in result.stdout


@pytest.mark.parametrize("job", sorted(REQUIRED_JOBS))
@pytest.mark.parametrize("conclusion", ["failure", "cancelled", "skipped", "unknown"])
def test_required_gate_rejects_every_non_success_dependency(job, conclusion):
    results = successful_results()
    results[job]["result"] = conclusion
    result = run_gate(results)
    assert result.returncode != 0
    assert "All required CI jobs passed." not in result.stdout


@pytest.mark.parametrize("job", sorted(REQUIRED_JOBS))
def test_required_gate_rejects_missing_dependency_even_when_others_pass(job):
    results = successful_results()
    del results[job]
    assert run_gate(results).returncode != 0


@pytest.mark.parametrize("results", [{}, [], None, {"unexpected": {"result": "success"}}])
def test_required_gate_rejects_empty_or_invalid_dependency_mapping(results):
    assert run_gate(results).returncode != 0


def test_required_gate_rejects_unrecognized_extra_dependency():
    results = successful_results()
    results["unexpected"] = {"result": "success"}
    assert run_gate(results).returncode != 0


def test_required_gate_rejects_missing_conclusion():
    results = successful_results()
    results[sorted(REQUIRED_JOBS)[0]] = {"outputs": {}}
    assert run_gate(results).returncode != 0
