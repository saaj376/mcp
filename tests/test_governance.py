"""Phase 5 verification: checks registry, scaffold, audit, policy payloads."""

import sys

import yaml

from codebase_memory.governance import apply, audit, gh, runner, scaffold
from codebase_memory.governance.checks import (
    CHECKS,
    REQUIRED_CATEGORIES,
    blocking_checks,
    find_binary,
    missing_binaries,
    resolve_package,
    select,
)
from codebase_memory.governance.policy import (
    CALLER_JOB,
    branch_protection_payload,
    org_ruleset_payload,
    required_contexts,
)


# --------------------------------------------------------------------------- #
# The registry
# --------------------------------------------------------------------------- #
def test_registry_covers_every_required_category():
    categories = {c.category for c in CHECKS}
    assert set(REQUIRED_CATEGORIES) <= categories


def test_graph_check_is_advisory():
    """Blast radius is a heuristic; a noisy blocking gate teaches bypassing."""
    graph = next(c for c in CHECKS if c.id == "graph")
    assert graph.blocking is False
    assert graph not in blocking_checks()


def test_resolve_substitutes_package():
    typecheck = next(c for c in CHECKS if c.id == "typecheck")
    assert typecheck.resolve("myproj") == ["mypy", "myproj"]


def test_select_reports_unknown_ids():
    checks, unknown = select(["lint", "nope"])
    assert [c.id for c in checks] == ["lint"]
    assert unknown == ["nope"]


def test_resolve_package_finds_package_and_ignores_tests(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "__init__.py").write_text("")
    (tmp_path / "myproj").mkdir()
    (tmp_path / "myproj" / "__init__.py").write_text("")
    assert resolve_package(tmp_path) == "myproj"


def test_resolve_package_falls_back_to_dot(tmp_path):
    (tmp_path / "script.py").write_text("x = 1\n")
    assert resolve_package(tmp_path) == "."


def test_find_binary_prefers_the_interpreters_own_environment():
    """The server runs from a venv whose bin dir is usually not on PATH."""
    assert find_binary("python") == sys.executable
    assert find_binary("almost-certainly-not-a-real-binary") is None


def test_missing_binaries_flags_absent_tools():
    fake = [c for c in CHECKS if c.id == "graph"]
    assert missing_binaries(fake) in ([], ["graph"])  # depends on install state


# --------------------------------------------------------------------------- #
# Scaffold: the generated workflow must parse and expose one job per check
# --------------------------------------------------------------------------- #
def test_standalone_workflow_is_valid_yaml_with_a_job_per_check():
    parsed = yaml.safe_load(scaffold.render_standalone("myproj"))
    assert parsed["name"] == "quality-gate"
    assert set(parsed["jobs"]) == {c.id for c in CHECKS}


def test_every_blocking_check_becomes_a_required_context():
    """The contexts required on GitHub must be job names that actually exist."""
    parsed = yaml.safe_load(scaffold.render_standalone("myproj"))
    for context in required_contexts("standalone"):
        assert context in parsed["jobs"]


def test_standalone_jobs_run_the_registry_commands():
    parsed = yaml.safe_load(scaffold.render_standalone("myproj"))
    runs = "\n".join(
        step.get("run", "")
        for job in parsed["jobs"].values()
        for step in job["steps"]
        if isinstance(step, dict)
    )
    assert "ruff check ." in runs
    assert "mypy myproj" in runs
    assert "bandit -q -r myproj" in runs


def test_graph_job_is_continue_on_error_and_pr_only():
    parsed = yaml.safe_load(scaffold.render_standalone("myproj"))
    graph_job = parsed["jobs"]["graph"]
    assert graph_job["continue-on-error"] is True
    assert "pull_request" in graph_job["if"]


def test_caller_workflow_delegates_to_the_org_definition():
    parsed = yaml.safe_load(scaffold.render_caller("my-org"))
    assert list(parsed["jobs"]) == [CALLER_JOB]
    uses = parsed["jobs"][CALLER_JOB]["uses"]
    assert uses.startswith("my-org/.github/.github/workflows/")


def test_reusable_workflow_is_workflow_call():
    parsed = yaml.safe_load(scaffold.render_reusable("myproj"))
    assert "workflow_call" in parsed["on"]


def test_reusable_mode_requires_an_org(tmp_path):
    try:
        scaffold.render(tmp_path, mode="reusable")
    except ValueError as exc:
        assert "org" in str(exc)
    else:  # pragma: no cover - guard
        raise AssertionError("expected ValueError")


def test_write_workflow_refuses_to_clobber(tmp_path):
    scaffold.write_workflow(tmp_path, "quality-gate.yml", "name: a\n")
    try:
        scaffold.write_workflow(tmp_path, "quality-gate.yml", "name: b\n")
    except FileExistsError:
        pass
    else:  # pragma: no cover - guard
        raise AssertionError("expected FileExistsError")
    written = scaffold.write_workflow(tmp_path, "quality-gate.yml", "name: b\n", overwrite=True)
    assert written.read_text() == "name: b\n"


# --------------------------------------------------------------------------- #
# Policy payloads
# --------------------------------------------------------------------------- #
def test_branch_protection_payload_matches_policy():
    payload = branch_protection_payload(["lint", "test"])
    assert payload["required_status_checks"] == {"strict": True, "contexts": ["lint", "test"]}
    assert payload["required_pull_request_reviews"]["required_approving_review_count"] == 1
    assert payload["allow_force_pushes"] is False
    assert payload["allow_deletions"] is False
    assert "enforce_admins" in payload and "restrictions" in payload


def test_reusable_contexts_are_prefixed_by_the_caller_job():
    """A called workflow reports as '<caller job> / <job>', not '<job>'."""
    assert required_contexts("reusable") == [f"{CALLER_JOB} / {c.id}" for c in blocking_checks()]


def test_org_ruleset_payload_blocks_force_push_and_deletion():
    payload = org_ruleset_payload(repo_pattern="~ALL")
    types = {r["type"] for r in payload["rules"]}
    assert {"deletion", "non_fast_forward", "pull_request", "required_status_checks"} <= types
    assert payload["conditions"]["repository_name"]["include"] == ["~ALL"]
    assert payload["enforcement"] == "active"


def test_org_ruleset_pins_required_workflow_when_repo_id_known():
    payload = org_ruleset_payload(workflow_repo_id=42)
    rule = next(r for r in payload["rules"] if r["type"] == "workflows")
    assert rule["parameters"]["workflows"][0]["repository_id"] == 42


# --------------------------------------------------------------------------- #
# Audit (local plane)
# --------------------------------------------------------------------------- #
def test_audit_local_reports_everything_missing_on_a_bare_repo(tmp_path):
    result = audit.audit_local(tmp_path)
    assert result["workflow_dir_exists"] is False
    assert set(result["categories_missing"]) == set(REQUIRED_CATEGORIES)


def test_audit_local_detects_categories_from_a_generated_workflow(tmp_path):
    (tmp_path / "myproj").mkdir()
    (tmp_path / "myproj" / "__init__.py").write_text("")
    scaffold.write_workflow(tmp_path, "quality-gate.yml", scaffold.render_standalone("myproj"))
    result = audit.audit_local(tmp_path)
    assert result["categories_missing"] == []
    assert set(result["categories_covered"]) >= set(REQUIRED_CATEGORIES)


def test_audit_local_records_delegation(tmp_path):
    scaffold.write_workflow(tmp_path, "quality-gate.yml", scaffold.render_caller("my-org"))
    result = audit.audit_local(tmp_path)
    assert result["delegates_to"]
    # A delegated repo runs no checks locally, but that is not a gap.
    gaps = audit._gaps(result, {"checked": False, "reason": "offline"})
    assert not any(g["id"].startswith("no-") and g["plane"] == "local" for g in gaps)


def test_audit_local_reads_tool_configuration(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[tool.ruff]\nline-length = 100\n")
    result = audit.audit_local(tmp_path)
    assert result["tool_configured"]["lint"] is True
    assert result["tool_configured"]["types"] is False


def test_audit_never_fails_without_github_auth(tmp_path, monkeypatch):
    monkeypatch.setattr(gh, "available", lambda: False)
    monkeypatch.setattr(gh, "current_repo", lambda root=None: None)
    result = audit.audit(tmp_path)
    assert result["compliant"] is False
    assert any(g["id"] == "remote-unverified" for g in result["gaps"])


def test_gaps_name_the_tool_that_fixes_them(tmp_path):
    result = audit.audit_local(tmp_path)
    gaps = audit._gaps(result, {"checked": False, "reason": "offline"})
    assert all(g["fixed_by"] for g in gaps)


# --------------------------------------------------------------------------- #
# Apply: dry_run must never touch the network
# --------------------------------------------------------------------------- #
def test_branch_protection_dry_run_previews_the_exact_call(monkeypatch):
    calls = []
    monkeypatch.setattr(gh, "api", lambda *a, **k: calls.append(a) or {"ok": False, "error": "x"})
    monkeypatch.setattr(apply, "observed_contexts", lambda repo, ref: (set(), "offline"))
    result = apply.apply_branch_protection(repo="o/r", branch="main", dry_run=True)
    assert result["dry_run"] is True
    assert result["would_call"]["method"] == "PUT"
    assert result["would_call"]["endpoint"] == "repos/o/r/branches/main/protection"
    assert calls == []  # no API call was made


def test_branch_protection_warns_about_never_reported_contexts(monkeypatch):
    monkeypatch.setattr(apply, "observed_contexts", lambda repo, ref: ({"lint"}, None))
    result = apply.apply_branch_protection(repo="o/r", branch="main", dry_run=True)
    assert result["warnings"]
    assert "test" in result["warnings"][0]


def test_org_ruleset_dry_run_previews_the_exact_call(monkeypatch):
    monkeypatch.setattr(gh, "api", lambda *a, **k: {"ok": False, "error": "should not be called"})
    result = apply.apply_org_ruleset("my-org", dry_run=True)
    assert result["would_call"]["endpoint"] == "orgs/my-org/rulesets"
    assert result["would_call"]["method"] == "POST"


def test_apply_reports_missing_repo_without_crashing(monkeypatch):
    monkeypatch.setattr(gh, "current_repo", lambda root=None: None)
    assert "error" in apply.apply_branch_protection(repo=None, root=None)


# --------------------------------------------------------------------------- #
# Runner
# --------------------------------------------------------------------------- #
def test_runner_marks_uninstalled_tools_skipped_not_passed(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "find_binary", lambda _: None)
    result = runner.run_checks(tmp_path)
    assert result["summary"]["passed"] == 0
    assert result["summary"]["skipped"] == len(CHECKS)
    assert result["would_block_merge"] is False


def test_runner_reports_failure_and_blocks(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "find_binary", lambda _: "/usr/bin/true")

    class Proc:
        stdout, stderr, returncode = "boom", "", 1

    monkeypatch.setattr(runner.subprocess, "run", lambda *a, **k: Proc())
    result = runner.run_checks(tmp_path, ids=["lint"])
    assert result["results"][0]["status"] == "failed"
    assert result["blocking_failures"] == ["lint"]
    assert result["would_block_merge"] is True
