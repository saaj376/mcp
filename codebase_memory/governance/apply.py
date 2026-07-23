"""Apply the policy to GitHub: branch protection and organization rulesets.

Both entry points default to ``dry_run=True`` and build their preview from the
same payload function the real call uses, so what you review is exactly what
gets sent.
"""

from __future__ import annotations

from pathlib import Path

from codebase_memory.governance import gh
from codebase_memory.governance.policy import (
    DEFAULT_RULESET_NAME,
    Policy,
    branch_protection_payload,
    org_ruleset_payload,
    required_contexts,
)


def observed_contexts(repo: str, ref: str) -> tuple[set[str], str | None]:
    """Check-run names GitHub has actually seen on ``ref``.

    GitHub will accept any string as a required context, including one that no
    workflow ever reports — and a required check that never reports blocks every
    pull request forever. This is how the caller warns about that.
    """
    result = gh.api(f"repos/{repo}/commits/{ref}/check-runs")
    if not result["ok"] or not isinstance(result.get("data"), dict):
        return set(), result.get("error")
    runs = result["data"].get("check_runs") or []
    names = {str(r["name"]) for r in runs if isinstance(r, dict) and r.get("name")}
    return names, None


def apply_branch_protection(
    repo: str | None = None,
    branch: str | None = None,
    contexts: list[str] | None = None,
    mode: str = "standalone",
    policy: Policy | None = None,
    dry_run: bool = True,
    root: str | Path | None = None,
) -> dict:
    """Enforce required checks, reviews, and no force-push on ``branch``."""
    target = repo or gh.current_repo(root)
    if not target:
        return {"error": "Could not determine the repository; pass repo='owner/name'."}

    if branch is None:
        info = gh.api(f"repos/{target}")
        if not info["ok"]:
            return {"error": info["error"], "repo": target}
        data = info["data"] if isinstance(info["data"], dict) else {}
        branch = data.get("default_branch") or "main"

    wanted = contexts or required_contexts(mode)
    payload = branch_protection_payload(wanted, policy or Policy())
    endpoint = f"repos/{target}/branches/{branch}/protection"

    warnings: list[str] = []
    seen, seen_error = observed_contexts(target, branch)
    if seen_error is None:
        never_reported = [c for c in wanted if c not in seen]
        if never_reported:
            warnings.append(
                "These contexts have never reported on "
                f"{branch}: {never_reported}. Requiring them before the workflow "
                "has run once will block every pull request. Run scaffold_ci, "
                "merge the workflow, and let it run first."
            )

    if dry_run:
        return {
            "repo": target,
            "branch": branch,
            "required_contexts": wanted,
            "warnings": warnings,
            **gh.preview(endpoint, "PUT", payload),
        }

    result = gh.api(endpoint, method="PUT", body=payload)
    if not result["ok"]:
        return {
            "repo": target,
            "branch": branch,
            "error": result["error"],
            "hint": (
                "Branch protection on a private repository requires a paid plan; "
                "public repositories are free. 'repo' scope is required."
            ),
            "warnings": warnings,
        }
    return {
        "repo": target,
        "branch": branch,
        "applied": True,
        "required_contexts": wanted,
        "warnings": warnings,
    }


def apply_org_ruleset(
    org: str,
    name: str = DEFAULT_RULESET_NAME,
    repo_pattern: str = "~ALL",
    contexts: list[str] | None = None,
    workflow_repo: str | None = None,
    workflow_ref: str = "refs/heads/main",
    policy: Policy | None = None,
    dry_run: bool = True,
) -> dict:
    """Create the org-level ruleset so new repositories inherit the policy."""
    workflow_repo_id = None
    notes: list[str] = []
    if workflow_repo:
        workflow_repo_id = gh.repo_id(workflow_repo)
        if workflow_repo_id is None:
            notes.append(
                f"Could not resolve the numeric id of {workflow_repo!r}; the "
                "required-workflow rule is omitted. Create the repository and "
                "commit the reusable workflow first (scaffold_ci mode='publish')."
            )

    payload = org_ruleset_payload(
        name=name,
        repo_pattern=repo_pattern,
        contexts=contexts or required_contexts("reusable"),
        policy=policy or Policy(),
        workflow_repo_id=workflow_repo_id,
        workflow_ref=workflow_ref,
    )
    endpoint = f"orgs/{org}/rulesets"

    if dry_run:
        return {"org": org, "notes": notes, **gh.preview(endpoint, "POST", payload)}

    result = gh.api(endpoint, method="POST", body=payload)
    if not result["ok"]:
        return {
            "org": org,
            "error": result["error"],
            "hint": (
                "Org rulesets covering private repositories require GitHub Team "
                "or Enterprise; they are free for public repositories. "
                "'admin:org' scope is required."
            ),
            "notes": notes,
        }
    created = result["data"] if isinstance(result["data"], dict) else {}
    return {
        "org": org,
        "applied": True,
        "ruleset_id": created.get("id"),
        "name": created.get("name", name),
        "notes": notes,
    }
