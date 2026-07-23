"""Compare a repository's actual governance state against the policy.

Read-only, and the tool an agent should always call first. The audit spans three
planes — the workflow files on disk, the repository's protection settings on
GitHub, and any organization ruleset that covers it — because a repo can hold a
perfectly good workflow and still be unprotected, or be covered by an org rule
while having no workflow at all.

Missing or unauthenticated ``gh`` degrades the report to the local plane. It is
never an error: an audit that fails closed on missing auth is useless in CI.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from codebase_memory.governance import gh
from codebase_memory.governance.checks import CHECKS, REQUIRED_CATEGORIES, resolve_package
from codebase_memory.governance.scaffold import WORKFLOW_DIR

_CONFIG_FILES = {
    "lint": ("ruff.toml", ".ruff.toml"),
    "types": ("mypy.ini", ".mypy.ini"),
}
_PYPROJECT_TOOLS = {"lint": "ruff", "types": "mypy"}


def _workflow_files(root: Path) -> list[Path]:
    directory = root / WORKFLOW_DIR
    if not directory.is_dir():
        return []
    return sorted(p for p in directory.iterdir() if p.suffix in (".yml", ".yaml"))


def _run_commands(workflow: dict) -> list[str]:
    """Every ``run:`` script in a parsed workflow."""
    commands: list[str] = []
    for job in (workflow.get("jobs") or {}).values():
        if not isinstance(job, dict):
            continue
        for step in job.get("steps") or []:
            if isinstance(step, dict) and isinstance(step.get("run"), str):
                commands.append(step["run"])
    return commands


def _delegations(workflow: dict) -> list[str]:
    """Job-level ``uses:`` — the repo delegating to a reusable workflow."""
    out = []
    for job in (workflow.get("jobs") or {}).values():
        if isinstance(job, dict) and isinstance(job.get("uses"), str):
            out.append(job["uses"])
    return out


def _covered_categories(commands: list[str]) -> set[str]:
    text = "\n".join(commands)
    covered = set()
    for check in CHECKS:
        pattern = rf"(?<![\w./-]){re.escape(check.binary)}(?![\w-])"
        if re.search(pattern, text):
            covered.add(check.category)
    return covered


def _tool_configs(root: Path) -> dict[str, bool]:
    """Whether lint/type tools are actually configured (not just runnable).

    Looks for the ``[tool.X]`` section header rather than parsing the TOML:
    ``tomllib`` is 3.11+, and this package supports 3.10.
    """
    configured = {}
    path = root / "pyproject.toml"
    try:
        text = path.read_text(encoding="utf-8") if path.is_file() else ""
    except OSError:
        text = ""
    for category, names in _CONFIG_FILES.items():
        in_pyproject = f"[tool.{_PYPROJECT_TOOLS[category]}" in text
        configured[category] = in_pyproject or any((root / n).is_file() for n in names)
    return configured


def audit_local(root: str | Path) -> dict:
    """Inspect workflow files and tool configuration on disk."""
    root = Path(root)
    files = _workflow_files(root)
    workflows: list[dict] = []
    covered: set[str] = set()
    delegated: list[str] = []

    for path in files:
        try:
            parsed = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except (yaml.YAMLError, OSError) as exc:
            workflows.append({"file": path.name, "error": str(exc)[:200]})
            continue
        if not isinstance(parsed, dict):
            workflows.append({"file": path.name, "error": "not a workflow mapping"})
            continue
        commands = _run_commands(parsed)
        uses = _delegations(parsed)
        categories = _covered_categories(commands)
        covered |= categories
        delegated += uses
        workflows.append(
            {
                "file": path.name,
                "name": parsed.get("name", path.stem),
                "jobs": sorted((parsed.get("jobs") or {}).keys()),
                "categories": sorted(categories),
                "delegates_to": uses,
            }
        )

    return {
        "workflow_dir_exists": (root / WORKFLOW_DIR).is_dir(),
        "workflows": workflows,
        "categories_covered": sorted(covered),
        "categories_missing": sorted(set(REQUIRED_CATEGORIES) - covered),
        "delegates_to": delegated,
        "tool_configured": _tool_configs(root),
        "package": resolve_package(root),
    }


def audit_remote(repo: str) -> dict:
    """Read branch protection, rulesets, and security settings from GitHub."""
    status = gh.auth_status()
    if not status.get("authenticated"):
        return {"checked": False, "reason": status.get("reason") or "not authenticated"}

    info = gh.api(f"repos/{repo}")
    if not info["ok"]:
        return {"checked": False, "reason": info["error"]}
    data = info["data"] if isinstance(info["data"], dict) else {}
    branch = (data.get("default_branch") or "main")

    protection = gh.api(f"repos/{repo}/branches/{branch}/protection")
    rulesets = gh.api(f"repos/{repo}/rulesets?includes_parents=true")
    security = data.get("security_and_analysis") or {}

    result: dict = {
        "checked": True,
        "repo": repo,
        "private": bool(data.get("private")),
        "default_branch": branch,
        "protected": bool(protection["ok"]),
        "security_and_analysis": {
            k: (v or {}).get("status") for k, v in security.items() if isinstance(v, dict)
        },
    }

    if protection["ok"] and isinstance(protection["data"], dict):
        payload = protection["data"]
        checks = payload.get("required_status_checks") or {}
        reviews = payload.get("required_pull_request_reviews") or {}
        result["protection"] = {
            "required_contexts": checks.get("contexts") or [
                c.get("context") for c in (checks.get("checks") or [])
            ],
            "strict": checks.get("strict"),
            "required_approving_review_count": reviews.get("required_approving_review_count"),
            "allow_force_pushes": (payload.get("allow_force_pushes") or {}).get("enabled"),
            "allow_deletions": (payload.get("allow_deletions") or {}).get("enabled"),
            "enforce_admins": (payload.get("enforce_admins") or {}).get("enabled"),
        }
    else:
        result["protection"] = None
        result["protection_error"] = protection.get("error")

    if rulesets["ok"] and isinstance(rulesets["data"], list):
        result["rulesets"] = [
            {
                "id": r.get("id"),
                "name": r.get("name"),
                "source": r.get("source"),
                "source_type": r.get("source_type"),
                "enforcement": r.get("enforcement"),
            }
            for r in rulesets["data"]
            if isinstance(r, dict)
        ]
        result["org_ruleset_covers_repo"] = any(
            r.get("source_type") == "Organization" for r in result["rulesets"]
        )
    else:
        result["rulesets"] = []
        result["org_ruleset_covers_repo"] = False

    return result


def _gaps(local: dict, remote: dict) -> list[dict]:
    gaps: list[dict] = []

    for category in local["categories_missing"]:
        if local["delegates_to"]:
            continue  # covered inside a reusable workflow we cannot see
        gaps.append(
            {
                "id": f"no-{category}-check",
                "plane": "local",
                "detail": f"No workflow step runs a {category} check.",
                "fixed_by": "scaffold_ci",
            }
        )
    for category, configured in local["tool_configured"].items():
        if not configured:
            gaps.append(
                {
                    "id": f"{category}-not-configured",
                    "plane": "local",
                    "detail": f"No {category} configuration found in pyproject.toml or a config file.",
                    "fixed_by": "manual",
                }
            )

    if not remote.get("checked"):
        gaps.append(
            {
                "id": "remote-unverified",
                "plane": "remote",
                "detail": f"GitHub state not checked: {remote.get('reason')}",
                "fixed_by": "gh auth login",
            }
        )
        return gaps

    protection = remote.get("protection")
    if not protection and not remote.get("org_ruleset_covers_repo"):
        gaps.append(
            {
                "id": "branch-unprotected",
                "plane": "remote",
                "detail": f"No branch protection or org ruleset on '{remote.get('default_branch')}'.",
                "fixed_by": "apply_branch_protection",
            }
        )
    elif protection:
        if not protection.get("required_contexts"):
            gaps.append(
                {
                    "id": "no-required-checks",
                    "plane": "remote",
                    "detail": "Branch is protected but requires no status checks.",
                    "fixed_by": "apply_branch_protection",
                }
            )
        if protection.get("allow_force_pushes"):
            gaps.append(
                {
                    "id": "force-push-allowed",
                    "plane": "remote",
                    "detail": "Force pushes are permitted on the default branch.",
                    "fixed_by": "apply_branch_protection",
                }
            )
        if not protection.get("required_approving_review_count"):
            gaps.append(
                {
                    "id": "no-required-review",
                    "plane": "remote",
                    "detail": "Pull requests can merge without an approving review.",
                    "fixed_by": "apply_branch_protection",
                }
            )

    if not remote.get("org_ruleset_covers_repo"):
        gaps.append(
            {
                "id": "no-org-ruleset",
                "plane": "org",
                "detail": "No organization ruleset covers this repository; policy is per-repo.",
                "fixed_by": "apply_org_ruleset",
            }
        )
    return gaps


def audit(root: str | Path, repo: str | None = None) -> dict:
    """Full actual-vs-policy report across the local, remote, and org planes."""
    local = audit_local(root)
    target = repo or gh.current_repo(root)
    remote = audit_remote(target) if target else {"checked": False, "reason": "no GitHub remote"}
    gaps = _gaps(local, remote)
    return {
        "root": str(Path(root).resolve()),
        "repo": target,
        "compliant": not gaps,
        "gaps": gaps,
        "local": local,
        "remote": remote,
    }
