"""Desired governance state, and the GitHub API payloads that express it.

Keeping the payload builders here (rather than inline in the tools) is what lets
``dry_run`` return exactly the body a real call would send — the preview and the
mutation are built by the same function, so they cannot disagree.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from codebase_memory.governance.checks import Check, blocking_checks

# Job name of the caller workflow when a repo delegates to the org's reusable
# workflow. GitHub reports checks from a called workflow as "<caller job> / <job>".
CALLER_JOB = "quality-gate"

DEFAULT_RULESET_NAME = "student-project-baseline"
REUSABLE_WORKFLOW_PATH = ".github/workflows/reusable-quality-gate.yml"


@dataclass(frozen=True)
class Policy:
    """The protection settings this server considers compliant."""

    required_reviews: int = 1
    require_up_to_date: bool = True
    dismiss_stale_reviews: bool = True
    allow_force_pushes: bool = False
    allow_deletions: bool = False
    enforce_admins: bool = False
    bypass_actors: list[dict] = field(default_factory=list)


DEFAULT_POLICY = Policy()


def required_contexts(mode: str = "standalone", checks: list[Check] | None = None) -> list[str]:
    """Status-check contexts to require, derived from the CHECKS registry.

    A GitHub status-check context is the *job* name, not the step name — which is
    why the generated workflow gives every check its own job. When the repo
    delegates to the org's reusable workflow, the context is additionally
    prefixed with the calling job's name.
    """
    selected = checks if checks is not None else list(blocking_checks())
    ids = [c.id for c in selected if c.blocking]
    if mode == "reusable":
        return [f"{CALLER_JOB} / {i}" for i in ids]
    return ids


def branch_protection_payload(
    contexts: list[str],
    policy: Policy = DEFAULT_POLICY,
) -> dict:
    """Body for ``PUT /repos/{owner}/{repo}/branches/{branch}/protection``.

    ``restrictions`` and ``enforce_admins`` must be present (possibly null) or
    GitHub rejects the request.
    """
    return {
        "required_status_checks": {
            "strict": policy.require_up_to_date,
            "contexts": contexts,
        },
        "enforce_admins": policy.enforce_admins,
        "required_pull_request_reviews": {
            "required_approving_review_count": policy.required_reviews,
            "dismiss_stale_reviews": policy.dismiss_stale_reviews,
        },
        "restrictions": None,
        "allow_force_pushes": policy.allow_force_pushes,
        "allow_deletions": policy.allow_deletions,
    }


def org_ruleset_payload(
    name: str = DEFAULT_RULESET_NAME,
    repo_pattern: str = "~ALL",
    contexts: list[str] | None = None,
    policy: Policy = DEFAULT_POLICY,
    workflow_repo_id: int | None = None,
    workflow_ref: str = "refs/heads/main",
) -> dict:
    """Body for ``POST /orgs/{org}/rulesets``.

    One ruleset, targeting the default branch of every matching repository, so a
    new project inherits the policy on creation with no per-repo setup.
    """
    rules: list[dict] = [
        {"type": "deletion"},
        {"type": "non_fast_forward"},
        {
            "type": "pull_request",
            "parameters": {
                "required_approving_review_count": policy.required_reviews,
                "dismiss_stale_reviews_on_push": policy.dismiss_stale_reviews,
                "require_code_owner_review": False,
                "require_last_push_approval": False,
                "required_review_thread_resolution": False,
            },
        },
        {
            "type": "required_status_checks",
            "parameters": {
                "strict_required_status_checks_policy": policy.require_up_to_date,
                "required_status_checks": [
                    {"context": c} for c in (contexts or required_contexts("reusable"))
                ],
            },
        },
    ]
    if workflow_repo_id is not None:
        rules.append(
            {
                "type": "workflows",
                "parameters": {
                    "workflows": [
                        {
                            "path": REUSABLE_WORKFLOW_PATH,
                            "repository_id": workflow_repo_id,
                            "ref": workflow_ref,
                        }
                    ]
                },
            }
        )
    return {
        "name": name,
        "target": "branch",
        "enforcement": "active",
        "conditions": {
            "ref_name": {"include": ["~DEFAULT_BRANCH"], "exclude": []},
            "repository_name": {"include": [repo_pattern], "exclude": []},
        },
        "rules": rules,
        "bypass_actors": policy.bypass_actors,
    }
