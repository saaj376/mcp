# Phase 5 — Repository Governance (design)

**Status: implemented.** Built as described, with the deviations noted in §12.
This document specifies a fifth phase for
`codebase-memory-mcp`: exposing repository governance — CI gates, branch
protection, org rulesets, and static/security analysis — as MCP tools that work
against **any repository the connected user is working on**, not just this one.

---

## 1. The requirement

Verbatim, the capability being designed:

> - **Required status checks:** every pull request triggers a GitHub Actions
>   workflow that runs linting, type-checking, the unit test suite, and a full
>   build. A PR physically cannot merge until all of these pass.
> - **Branch protection rules:** no direct pushes to `main`, no force-pushes, and
>   at least one required check must be green — enforced at the repository level,
>   not by convention.
> - **Org-level ruleset:** the policy is defined once at the GitHub organization
>   level and a reusable workflow is referenced from every repo, so new student
>   projects inherit the same protection automatically, with zero per-repo setup.
> - **Static/security analysis.**

## 2. The problem this phase solves

None of the above exists in this repository today: there is no `.github/`
directory, no workflow, no linter or type-checker configured, and no protection
on `main`. More importantly, all four bullets describe **GitHub-side policy** —
state that lives in GitHub's API, not in a Python package.

So the naive move (hand-write a `ci.yml` into this repo) satisfies none of the
actual goal, which is *"anyone who connects this MCP can run it for any repo
they're working on."* A mentor onboarding ten student repositories should be able
to say **"audit this repo, then bring it up to policy"** and have it happen.

That reframes the deliverable. The MCP does not *contain* the CI gate. The MCP
**installs, audits, and enforces** the CI gate on a target repository:

```
audit  →  scaffold  →  apply  →  verify
```

### Why this belongs in *this* server

Because the graph is already here. A generic "set up CI" tool is a shell script.
What makes this one worth building is that the generated gate calls back into the
graph the other four phases produce:

| Existing capability | Becomes a governance check |
|---|---|
| `detect_changes` → `gate_should_block` | fails a PR that edits a high-fan-in symbol without touching tests |
| `search_graph(kind="dead_code")` | fails/annotates a PR that adds unreachable code |
| `save_snapshot` / `load_snapshot` | CI restores the committed `graph.db.zst`, so the gate needs no re-index |

That is the design doc's **L1 — CI gate** layer, which the README's mapping table
promises but which was never implemented. Phase 5 is that row.

---

## 3. Tool surface

Five new MCP tools, following the existing conventions in
[server.py](../codebase_memory/server.py): snake_case verbs, an optional
`root` argument resolved by `_project_root()`, and a plain `dict` return with an
`error` key on failure rather than a raised exception.

| Tool | Reads | Writes | Needs `gh` auth |
|---|---|---|:--:|
| `audit_governance` | local `.github/`, remote repo settings | — | optional |
| `run_quality_checks` | working tree | — | no |
| `scaffold_ci` | — | `.github/workflows/*.yml` in the target repo | no |
| `apply_branch_protection` | — | GitHub branch protection | `repo` |
| `apply_org_ruleset` | — | GitHub org ruleset | `admin:org` |

### 3.1 `audit_governance(root=None, repo=None)`

Read-only. The safe entry point, and the tool an agent should always call first.

Reports **actual vs. policy** across three planes:

- **Local** — which workflows exist, which of the four required check categories
  (lint / typecheck / test / build) they actually run, whether a lint or type
  config is present in `pyproject.toml`.
- **Remote** (via `gh`, skipped gracefully when unavailable) — default branch,
  branch protection or repo ruleset, the list of required status check *contexts*,
  whether force-push and direct push are blocked, and the state of secret
  scanning / Dependabot alerts / code scanning.
- **Org** — whether an org ruleset already covers this repo, and whether it
  pins a required workflow.

Returns `{"compliant": bool, "gaps": [{"id", "plane", "detail", "fixed_by"}], ...}`
where `fixed_by` names the tool that closes the gap. Degrades to local-only
results — never an error — when `gh` is missing or unauthenticated.

### 3.2 `run_quality_checks(root=None, checks=None)`

Runs the same checks CI runs, locally, before you push. Returns per-check
`{id, passed, duration_s, output_tail}`.

This is the tool that makes the gate honest: **local and CI execute from one
registry**, so they cannot drift (§4).

### 3.3 `scaffold_ci(root=None, mode="reusable", org=None, python_version="3.11", dry_run=True)`

Writes the caller workflow into the target repo.

- `mode="reusable"` (default) → a ~12-line workflow that delegates to
  `<org>/.github/.github/workflows/quality-gate.yml@v1`. This is the mechanism
  behind the org-inheritance bullet: policy changes once, centrally.
- `mode="standalone"` → the full workflow inlined, for repos outside an org.

`dry_run=True` returns the rendered YAML without writing. Refuses to clobber an
existing file unless `overwrite=True`.

### 3.4 `apply_branch_protection(repo, branch=None, required_checks=None, dry_run=True)`

`PUT /repos/{owner}/{repo}/branches/{branch}/protection` via `gh api`. Sets:

| Setting | Value | Requirement bullet |
|---|---|---|
| `required_status_checks.strict` | `true` (branch must be up to date) | required checks |
| `required_status_checks.contexts` | the check ids from §4 | required checks |
| `required_pull_request_reviews.required_approving_review_count` | `1` | no direct merge |
| `allow_force_pushes` | `false` | no force-push |
| `allow_deletions` | `false` | no branch deletion |
| direct push to `main` | blocked (protection implies PR-only when reviews are required) | no direct pushes |

`dry_run=True` returns the exact endpoint and JSON body it *would* send, so the
change is reviewable before it touches a real repository.

### 3.5 `apply_org_ruleset(org, name="student-project-baseline", repo_pattern="~ALL", dry_run=True)`

`POST /orgs/{org}/rulesets` — the "define once, inherit automatically" bullet.
One ruleset, targeting all repos (or a name pattern), enforcing `pull_request`,
`required_workflows`, `non_fast_forward`, and `deletion` rules. New student repos
are covered on creation with **zero per-repo setup** — `scaffold_ci` then becomes
a convenience, not a prerequisite.

---

## 4. Single source of truth for checks

The one structural decision worth defending. A `CHECKS` registry —

```python
# codebase_memory/governance/checks.py
Check(id="lint",      cmd=["ruff", "check", "."],            category="lint",     blocking=True)
Check(id="typecheck", cmd=["mypy", "codebase_memory"],       category="types",    blocking=True)
Check(id="test",      cmd=["pytest", "-q"],                  category="test",     blocking=True)
Check(id="build",     cmd=["python", "-m", "build"],         category="build",    blocking=True)
Check(id="deps",      cmd=["pip-audit"],                     category="security", blocking=True)
Check(id="sast",      cmd=["bandit", "-r", "codebase_memory"], category="security", blocking=True)
Check(id="graph",     cmd=[...detect_changes gate...],       category="graph",    blocking=False)
```

— is consumed by **three** things: `run_quality_checks` executes it,
`scaffold_ci` renders it into workflow steps, and `apply_branch_protection`
derives the required-check contexts from it. Add a check in one place and it
appears locally, in CI, and in the protection rule together. Any other layout
guarantees the three drift apart.

## 5. Static / security analysis

The fourth bullet was truncated in the request, so this is the proposed scope:

| Layer | Tool | Why this one |
|---|---|---|
| Lint | `ruff` | fast, single binary, replaces flake8+isort |
| Types | `mypy` | the codebase is already `from __future__ import annotations` throughout |
| Dependency CVEs | `pip-audit` | PyPA-maintained, reads the same `pyproject.toml` |
| SAST | `bandit` | pip-installable, runs anywhere, no GitHub plan requirement |
| SAST (deep) | CodeQL | **opt-in** — free for public repos, requires Advanced Security for private ones |
| Secret scanning / Dependabot | GitHub-native | toggled by `apply_org_ruleset`, not a workflow step |
| Structural | this graph | dead code + blast-radius gate — nothing else provides it |

`bandit` is the default SAST rather than CodeQL specifically so the policy works
on private student repos without a paid plan. CodeQL is offered as a separate
generated workflow when the repo is public.

## 6. Auth model

All GitHub mutation shells out to the **`gh` CLI**, never a hand-rolled REST
client. This means the server holds no token, inherits the user's existing
login (including SSO-authorized orgs), and honours `GH_TOKEN` in CI for free.

Required scopes: `repo` for §3.4, `admin:org` for §3.5. Missing scope returns a
structured `error` naming the scope, not a stack trace.

## 7. Safety rules

Because three of these tools mutate state outside the working tree:

1. Every mutating tool defaults to **`dry_run=True`** and returns the exact call
   it would make.
2. `scaffold_ci` never overwrites an existing workflow without `overwrite=True`.
3. `apply_org_ruleset` is never called implicitly by another tool — org-wide
   blast radius requires an explicit, deliberate invocation.
4. Audit never mutates and never fails the session on missing auth.

## 8. Proposed layout

```
codebase_memory/
  governance/
    __init__.py
    checks.py     # the CHECKS registry (§4)
    policy.py     # desired-state dataclasses
    audit.py      # actual-vs-policy diff
    scaffold.py   # CHECKS -> workflow YAML
    gh.py         # thin `gh` wrapper: run, json, dry_run
  server.py       # +5 @mcp.tool() wrappers
.github/workflows/
  quality-gate.yml          # this repo, dogfooding its own scaffold output
  reusable-quality-gate.yml # the org-shareable definition
tests/
  test_governance.py
```

New dependency: `pyyaml` (parse existing workflows during audit — regex-sniffing
YAML is how audits produce false passes). `gh` is an external binary, not a
Python dep, and every tool degrades cleanly without it.

## 9. Build order

| Step | Scope | Verified by |
|---|---|---|
| 5.1 | `checks.py` + `run_quality_checks` | checks run against this repo; a deliberately-broken file fails `lint` |
| 5.2 | `audit_governance` (local plane) | reports all four categories missing here today |
| 5.3 | `scaffold_ci` + dogfood on this repo | generated workflow is committed and goes green on a real PR |
| 5.4 | `audit_governance` (remote plane) + `apply_branch_protection` | `dry_run` body matches the table in §3.4; audit flips to compliant after apply |
| 5.5 | `apply_org_ruleset` + publish reusable workflow | a fresh repo in the org inherits protection with no per-repo setup |

Each step ships independently — 5.1–5.3 need no GitHub auth at all.

## 10. Known limitations, stated up front

- **Chicken-and-egg on required checks.** GitHub will only accept a status check
  as *required* by a name it has already observed. `apply_branch_protection` must
  warn when a context has never reported, or protection silently blocks every PR
  forever. Ordering is therefore 5.3 before 5.4, always.
- **Org rulesets on private repos** need GitHub Team or Enterprise. Public repos
  are free. `apply_org_ruleset` should detect the plan and say so rather than
  returning an opaque 403.
- **Admin bypass.** For student repos the mentor usually needs a bypass entry;
  otherwise `enforce_admins` locks the instructor out too. Bypass actors are an
  explicit argument, defaulting to none.
- **Python-only.** The check registry and scaffold target Python, matching the
  `ast`-based indexer. A Node or Go repo gets protection and rulesets, but the
  check commands would need a second registry.
- **`detect_changes` misses untracked files**, since it reads `git diff HEAD` —
  a pre-existing Phase 3 limitation that the CI gate inherits. In CI this is
  harmless (everything is committed); locally it is not.
- **The graph check starts advisory** (`blocking=False`). Blast radius is a
  heuristic with a hardcoded `HIGH_FANIN=5`; making it merge-blocking before it
  is calibrated on real PRs would train people to bypass the gate.

---

## 11. Open decisions — as resolved

1. **Org name.** This repository (`saaj376/mcp`) is a personal repo, so it is
   scaffolded in `standalone` mode. `reusable` mode and `apply_org_ruleset` are
   implemented and verified via `dry_run` against `SSN-SNUC-MUN`; applying them
   needs a `<org>/.github` repo holding the reusable workflow.
2. **Blocking vs. advisory for the graph check** — shipped advisory
   (`blocking=False`, `continue-on-error: true`), per §10.
3. **CodeQL** — left out of v1. `bandit` covers SAST without requiring Advanced
   Security on private repos, which is the common case for student projects.
4. **Scope of v1** — all five steps shipped.

## 12. Deviations from this design, as built

- **Three modules beyond the §8 layout.** `runner.py` (the local check runner),
  `apply.py` (the two mutating GitHub calls, kept out of `server.py` so the tool
  wrappers stay thin), and `gate.py` (a `codebase-memory-gate` console script —
  a CI step needs an exit code, not an MCP call).
- **No `tomllib`.** It is 3.11+, and the package supports 3.10. Detecting a
  `[tool.ruff]` / `[tool.mypy]` section is a substring match instead. `pyyaml`
  was still added, as planned, for workflow parsing.
- **`find_binary` prefers the interpreter's own environment.** `shutil.which`
  alone reported every tool as uninstalled, because the server runs from a
  virtualenv whose `bin` is not on `PATH`.
- **The generated `"on":` key is quoted.** YAML 1.1 parses a bare `on` as boolean
  `true`, which broke round-tripping the generated file through our own audit.
- **Check command adjustments** found by running them: `pip-audit
  --skip-editable` without `--strict` (the project under audit has no PyPI
  record, and `--strict` treats a skipped distribution as an error), and
  `bandit --severity-level medium` (every LOW finding was bandit noticing the
  `subprocess` calls this package exists to make).
- **`ruff line-length = 120`**, matching the existing codebase rather than
  forcing a reformat of code that predates the lint gate.
