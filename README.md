# codebase-memory-mcp

A persistent, queryable **structural knowledge graph** of a codebase, exposed
over the [Model Context Protocol](https://modelcontextprotocol.io). It gives a
CI gate, an AI code‑review step, and a runtime feedback loop **one shared
picture** of the code instead of each re-deriving repo structure from grep and
file reads on every run.

The graph is a single SQLite file under `.codebase-memory/graph.db` (and a
compressed, committable `graph.db.zst` snapshot), so every agent session — a
mentor's, a student's, and CI's headless review — reads the same structural
facts.

> **Provenance.** This implements the *Combined Code Quality Strategy* design
> doc: a shared code knowledge graph sitting underneath three enforcement layers
> (CI gate → AI review → canary runtime). Each MCP tool below maps to a specific
> role that document assigns to the graph.

---

## Contents

- [Status](#status)
- [At a glance (metrics)](#at-a-glance-measured)
- [Tool-call routing evaluation](#tool-call-routing-evaluation)
- [How it works](#how-it-works)
- [The graph model](#the-graph-model)
- [MCP tools](#mcp-tools)
- [Governance (Phase 5)](#governance-phase-5)
- [Call-resolution semantics](#call-resolution-semantics)
- [Risk classification](#risk-classification-detect_changes)
- [Install](#install)
- [Usage](#usage)
- [Persistence & schema](#persistence--schema)
- [Testing](#testing)
- [Limitations & deliberate scope choices](#limitations--deliberate-scope-choices)
- [Project layout](#project-layout)

---

## Status

All five phases of the design are implemented, tested, and dogfooded on this
repository.

| Phase | Scope | State |
|------:|-------|:-----:|
| 0 | Package scaffold, MCP server, graph store | ✅ |
| 1 | Python indexer → `module` / `class` / `function` nodes + `CONTAINS` / `IMPORTS` / `CALLS` edges | ✅ |
| 2 | Read tools: `search_graph`, `get_architecture`, `trace_path`, dead-code query | ✅ |
| 3 | `detect_changes` — git diff → blast radius + risk classification | ✅ |
| 4 | `ingest_traces` — runtime `HTTP_CALLS` validation + `.zst` snapshot | ✅ |
| 5 | Governance: required status checks, branch protection, org rulesets, static/security analysis | ✅ |

### How the tools map to the doc's three layers

| Layer (design doc) | What it does | Tools |
|---|---|---|
| **L1 — CI gate** | deterministic pass/fail structural gates | `audit_governance`, `scaffold_ci`, `apply_branch_protection`, `apply_org_ruleset`, `run_quality_checks`, `detect_changes` (blast radius + `gate_should_block`), `search_graph` (dead code) |
| **L2 — AI review** | ground review in the graph, cite real callers | `trace_path`, `get_architecture`, `search_graph` |
| **L3 — runtime** | validate/correct the graph from canary traffic | `ingest_traces` |
| **Shared substrate** | one committed structural index for every session | `save_snapshot` / `load_snapshot` (`graph.db.zst`) |

---

## At a glance (measured)

All figures below are **measured**, not estimated — from the test suite and from
indexing this repository itself (7 source modules + 4 test modules = 11 Python
files).

### Test suite

| Metric | Value |
|---|---|
| Tests | **23 passing** |
| Runtime | **0.22 s** |
| Test code | 452 lines across 4 files |
| Coverage by area | indexer 6 · queries 6 · changes 5 · traces 6 |

### Graph built from this repo (`index_codebase` on `.`)

| Nodes: 119 total | Edges: 329 total |
|---|---|
| `module` 11 | `CONTAINS` 94 |
| `class` 2 | `IMPORTS` 65 |
| `function` 92 | `CALLS` 170 |
| `endpoint` 0¹ | `HTTP_CALLS` 0¹ |

¹ This repo makes no outbound HTTP client calls, so there are no endpoints to
infer — expected.

### Call-resolution coverage (this repo)

| Call class | Count | Edged? |
|---|---:|:---:|
| Confident project calls (`resolved=True`) | 123 | ✅ |
| Heuristic unique-name calls (`resolved=False`) | 47 | ✅ |
| Stdlib / third-party (e.g. `ast.walk`, `conn.execute`) | 349 | ❌ (counted, not edged) |

The 349 unresolved are reported in the build report — coverage is never silently
hidden. Only calls that bind to a **project** symbol become `CALLS` edges,
keeping the graph's blast radius meaningful.

### Dead-code query (this repo)

| Result | Count |
|---|---:|
| Dead-code candidates | 11 |
| Excluded as entrypoints (`main`/`test_*`/dunders) | 25 |

### Snapshot compression

| `graph.db` | `graph.db.zst` | Reduction |
|---:|---:|:---:|
| 126,976 B (124 KB) | 16,046 B (15.7 KB) | **87.4 % smaller (7.9×)** |

Roundtrip verified: decompressing `graph.db.zst` reloads to an identical graph
(`stats()` equal, edge attributes preserved).

---

## Tool-call routing evaluation

A server is only as good as an agent's ability to *pick the right tool with
valid arguments* from its descriptions. We measure this directly.

**Method.** The 9 real tool schemas (exactly what a connected MCP client sees)
plus a labeled benchmark of natural-language queries are handed to a
**model-under-test** that must route each query to a single tool call — blind to
the source (no repo access), so this measures genuine routing, not recall. A
deterministic validator then classifies each call against ground truth:

| Verdict | Meaning |
|---|---|
| `correct` | right tool + args validate against the JSON schema + correct values |
| `correct_refusal` | query needs a capability no tool provides, and the model returned no call |
| `wrong_tool` | picked a tool outside the accept-set (incl. calling on a no-tool query) |
| `hallucinated_tool` / `hallucinated_param` | named a nonexistent tool / passed a key not in the schema |
| `invalid_args` / `malformed` | missing required arg, wrong type/value / arguments not a JSON object |

### Results

**Hard benchmark** — 50 queries (16 direct, several tool-confusion traps, 2
genuinely ambiguous, **13 "no valid tool" traps** to catch hallucination),
routed by three models:

| Model | Success | correct | correct_refusal | wrong_tool | malformed / hallucinated / invalid_args |
|---|---|---:|---:|---:|---:|
| Haiku 4.5 (weak) | **96 %** (48/50) | 35 | 13 | 2 | **0** |
| Sonnet 5 (mid) | **98 %** (49/50) | 36 | 13 | 1 | **0** |
| Opus 4.8 (strong) | **98 %** (49/50) | 36 | 13 | 1 | **0** |
| **Aggregate** | **97.3 %** (146/150) | 107 | 39 | 4 | **0** |

An earlier 20-query smoke benchmark (2 no-tool traps) scored **100 % (60/60)**
across 3 routers.

### What the failures reveal

Across all **150 decisions** there were **zero** malformed calls, hallucinated
tools, hallucinated params, or invalid arguments, and **all 39 no-tool-trap
refusals were correct** (no fabricated `rename_symbol`, `format_code`,
`run_tests`, `cyclomatic_complexity`, `git_blame`, `coverage`, `security_scan`,
or `uml_diagram`). The only failure mode was `wrong_tool`, and it localized to
**two module-granularity queries**:

- *"Which **functions** call into the **module** `db`?"* — all 3 models chose
  `search_graph{kind:callers, target:"db"}`. `CALLS` edges are function→function,
  so callers of a *module* node returns empty. **No tool answers this.**
- *"Which modules import `db`?"* — Haiku made the same mistake; Sonnet/Opus
  correctly used `get_architecture` (whose `module_dependencies` answers it).

So the miss is a **capability gap, not a routing defect**: `search_graph` has no
reverse-`IMPORTS` / module-level fan-in kind, so models over-reach `callers`
onto a module target. Adding a `search_graph` kind such as `importers` would
close it (estimated 100 % across all three models).

*Caveats: one sample per model (variance unmeasured; the three converged except
on one query); N=50, hand-authored; two answer-key judgment calls on the
unanswerable module queries. Read the figures as "these tools are cleanly
routable by a capable agent," not as a universal floor.*

---

## How it works

```
                         ┌───────────────────────────────────────────────┐
   Python source ──ast──▶│  indexer.py    two-pass AST walk               │
                         │   pass 1: collect module/class/function nodes  │
                         │   pass 2: resolve IMPORTS / CALLS / HTTP_CALLS  │
                         └───────────────────────┬───────────────────────┘
                                                 ▼
   git diff ──▶ changes.py ──┐        ┌──────────────────────────┐
                             ├──────▶ │  graph.py                │──save──▶ .codebase-memory/graph.db
   runtime traces ─▶ traces.py ┘      │  MultiDiGraph + SQLite   │──zstd──▶ .codebase-memory/graph.db.zst
                                      └───────────┬──────────────┘
   queries.py (read) ◀──────────────────────────┘
                                                 ▼
                                    server.py  (FastMCP, 9 tools over stdio)
```

- **In memory** the graph is a `networkx.MultiDiGraph`; edges are keyed by type
  so a `CONTAINS` and a `CALLS` edge between the same two symbols coexist.
- **On disk** it is one SQLite file (`nodes` + `edges` tables), plus an optional
  zstd‑compressed snapshot for committing.
- **Every tool** loads the persisted graph rather than re-parsing the repo,
  which is the whole point: structure is derived once and shared.

---

## The graph model

### Nodes

| Kind | Id scheme | Example | Extra attributes |
|---|---|---|---|
| `module` | dotted path | `codebase_memory.indexer` | `file`, `line`, `end_line` |
| `class` | `module.Class` | `codebase_memory.graph.CodeGraph` | `file`, `line`, `end_line` |
| `function` | `module.func` / `module.Class.method` | `codebase_memory.graph.CodeGraph.save` | `file`, `line`, `end_line` |
| `endpoint` | `"<METHOD> <scheme>://<host><path>"` | `GET http://users.svc/v1/user` | (runtime/static observed) |
| `external` | caller id from a trace not in the graph | `gateway` | created by `ingest_traces` |

`line`/`end_line` are the 1‑based inclusive source span — this is what lets a
changed diff line map to its **smallest enclosing** symbol.

### Edges

| Type | From → To | Meaning | Key attributes |
|---|---|---|---|
| `CONTAINS` | module→class/function, class→method | lexical containment | — |
| `IMPORTS` | module → imported symbol/module | binds to the concrete imported symbol when it exists | `resolved` |
| `CALLS` | function → function/class | resolved intra-project call (constructor calls target the class node) | `resolved` (confidence) |
| `HTTP_CALLS` | function/external → endpoint | outbound HTTP call | `source` (`static`/`runtime`), `confirmed`, `status`, `count` |

---

## MCP tools

Fourteen tools over stdio. Every tool accepts an optional `root` (defaults to the
`CODEBASE_MEMORY_ROOT` env var, else the current working directory). Read tools
return `{"error": ...}` if the graph hasn't been built yet.

### `index_codebase(root=None)`
Indexes a Python project and **persists** the graph to `.codebase-memory/graph.db`.
Returns `stats` and a build `report` (files parsed, parse errors, unresolved-call
count).

### `graph_stats(root=None)`
Node/edge counts for the persisted graph, without re-indexing.

### `search_graph(kind, target=None, root=None)`
Structured queries — **not** Cypher; the backend is networkx.

| `kind` | `target` | Returns |
|---|---|---|
| `callers` | symbol id | direct callers (incoming `CALLS`) |
| `callees` | symbol id | direct callees (outgoing `CALLS`) |
| `by_name` | name substring (case-insensitive) | matching symbols |
| `by_kind` | `module`/`class`/`function` | all symbols of that kind |
| `dead_code` | *(none)* | functions with **no** incoming `CALLS` or `IMPORTS`; likely entrypoints reported separately in `excluded_as_entrypoints` |

### `get_architecture(root=None)`
Per-module classes/functions plus internal **module → module** dependencies
(collapsed from the import graph). Dogfooded on this repo: 11 modules, **21
internal dependencies**.

### `trace_path(source, target, root=None)`
Shortest call chain between two fully-qualified symbols over `CALLS` edges.

```jsonc
// trace_path("sample.app.Greeter.loud", "sample.helpers.shout")  — from the tests
{
  "found": true, "hops": 3,
  "path": ["sample.app.Greeter.loud", "sample.app.Greeter.hello",
           "sample.app.greet", "sample.helpers.shout"]
}
```

### `detect_changes(base=None, head=None, root=None)`
Maps a git diff to affected symbols, blast radius, and a risk classification.
`base=None` diffs the working tree vs `HEAD`; a `base` diffs the merge-base
range `base...head`.

```jsonc
// A 5-caller core() modified without touching tests — from the tests
{
  "changed_files": ["app.py"],
  "tests_changed": false,
  "affected_symbols": [
    {"id": "app.core", "kind": "function", "fan_in": 5,
     "direct_callers": ["app.a","app.b","app.c","app.d","app.e"], "changed_lines": [2]}
  ],
  "blast_radius": ["app.a","app.b","app.c","app.d","app.e"],
  "blast_radius_size": 5,
  "risk": "high",
  "gate_should_block": true,
  "risk_reasons": [
    "A changed symbol has 5 transitive callers (>= 5).",
    "High-fan-in change with no accompanying test change."
  ]
}
```

### `ingest_traces(traces_data, root=None)`
Reconciles observed runtime calls against static `HTTP_CALLS` edges. Each trace:
`{"caller": <symbol id>, "method": "GET", "url": "...", "count": <int?>}`.

| Bucket | Meaning |
|---|---|
| `confirmed` | a static edge that traffic also exercised |
| `runtime_only` | traffic hit a route static analysis missed (a **correction** — dynamic dispatch, config routing, non-literal URLs) |
| `unconfirmed_static` | a static edge never seen in traffic (**suspected mis-inference** — the graph drifting from reality) |
| `unknown_callers` | trace callers not present in the graph (added as `external` nodes) |

### `save_snapshot(root=None)` / `load_snapshot(root=None)`
Compress `graph.db` → committable `graph.db.zst` (~87 % smaller here) and
restore it. Commit the `.zst` alongside the repo so CI and every local session
start from the same graph.

---

## Governance (Phase 5)

The first four phases build the graph. Phase 5 is the enforcement layer that
consumes it — and it operates on **any repository the connected user is working
on**, not just this one. Design rationale: [`docs/governance-design.md`](docs/governance-design.md).

### The single source of truth

One registry, [`governance/checks.py`](codebase_memory/governance/checks.py),
is read by three consumers, so local runs, CI, and the required-check list
cannot drift apart:

| Check id | Command | Category | Blocking |
|---|---|---|:--:|
| `lint` | `ruff check .` | lint | ✅ |
| `typecheck` | `mypy <pkg>` | types | ✅ |
| `test` | `pytest -q` | test | ✅ |
| `build` | `python -m build` | build | ✅ |
| `deps` | `pip-audit --skip-editable` | security | ✅ |
| `sast` | `bandit -r <pkg> --severity-level medium` | security | ✅ |
| `graph` | `codebase-memory-gate` | graph | ⚠️ advisory |

`<pkg>` is resolved per repository, so the same registry works on any project.

### `audit_governance(root=None, repo=None)`
Read-only actual-vs-policy report across three planes — workflow files on disk,
branch protection on GitHub, and any org ruleset covering the repo. Every gap
names the tool that closes it. Degrades to the local plane (never an error) when
`gh` is missing or unauthenticated. **Call this first.**

### `run_quality_checks(root=None, checks=None)`
Runs the registry locally — the same commands CI runs. A check whose tool isn't
installed is reported `skipped`, never `passed`.

### `scaffold_ci(root=None, mode="reusable", org=None, dry_run=True)`
Generates the workflow. `mode="reusable"` emits a ~12-line caller delegating to
`<org>/.github`; `"standalone"` inlines everything; `"publish"` writes the
org-side reusable definition. Every check becomes its own **job**, because a
GitHub status-check context is a job name — that's what makes the required-check
list real.

### `apply_branch_protection(repo=None, branch=None, mode="standalone", dry_run=True)`
Required status checks (strict), ≥1 approving review, no force-push, no
deletion. Warns when a required context has never reported — GitHub accepts
unknown context names, and one that never reports blocks every PR forever.

### `apply_org_ruleset(org, repo_pattern="~ALL", workflow_repo=None, dry_run=True)`
Defines the policy once at the org level so new repos inherit it with zero
per-repo setup. Requires `admin:org`. Never called implicitly by another tool.

All three mutating tools default to `dry_run=True` and build their preview from
the same payload function the real call uses, so what you review is exactly what
gets sent.

---

## Call-resolution semantics

Resolution is conservative and **best-effort by design**. An edge is emitted
only when the callee binds to a known project symbol; the confidence is recorded
on the edge so different consumers can choose their tolerance.

| Case | Example | Confidence |
|---|---|---|
| `self.`/`cls.` method | `self.hello()` | `resolved=True` |
| module-local name | `greet()` defined in the same module | `resolved=True` |
| imported symbol | `from a import c; c()` | `resolved=True` |
| module-aliased call | `import a.b as x; x.f()` | `resolved=True` |
| **unique-name fallback** | `repo.persist()` where `persist` is the only `persist` project-wide | `resolved=False` (heuristic) |
| stdlib / third-party / non-unique | `conn.execute(...)`, `text.upper()` | *no edge* (counted in report) |

The `resolved=False` fallback is what makes the **dead-code** query usable:
method-calls-on-variables would otherwise leave every such method looking
uncalled. Blast-radius (`detect_changes`) currently includes these edges — a
conservative choice (over-estimate impact rather than under-estimate); the flag
is available if you want confident-only.

---

## Risk classification (`detect_changes`)

Deterministic — no model judgment — so it can drive a CI gate.

| Risk | Condition |
|---|---|
| `high` | a changed symbol has **≥ 5** transitive callers (`HIGH_FANIN`) |
| `medium` | changed symbols have ≥ 1 caller |
| `low` | changed symbols have no in-project callers, or no code symbols changed |

`gate_should_block = (risk == "high") and not tests_changed` — the design doc's
Layer 1 signal: *"PRs that touch high-fan-in functions without a corresponding
test change."*

---

## Install

Requires Python ≥ 3.10 (developed on **3.12.3**).

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
```

Dependencies: `mcp` (server), `networkx` 3.x (graph), `zstandard` (snapshots),
`pytest` (dev). No external database or server process.

---

## Usage

### Python API

```python
from codebase_memory.indexer import index_project
from codebase_memory.graph import default_db_path
from codebase_memory import queries

graph, report = index_project(".")
graph.save(default_db_path("."))

print(graph.stats())                       # node/edge counts
print(queries.find_dead_code(graph))       # zero-caller functions
print(queries.get_architecture(graph))     # module dependency map
```

### As an MCP server

Add to an MCP client (e.g. Claude Code `.mcp.json`):

```json
{
  "mcpServers": {
    "codebase-memory": {
      "command": "codebase-memory-mcp",
      "env": { "CODEBASE_MEMORY_ROOT": "/path/to/project" }
    }
  }
}
```

### Bringing a repository up to policy

The intended order matters — GitHub only accepts a status check as *required* by
a name it has already observed, so the workflow must run once before protection
is applied:

```text
audit_governance()                          # what's missing, and what fixes it
scaffold_ci(mode="standalone", dry_run=False)
  → commit, open a PR, let the workflow run once
apply_branch_protection(dry_run=True)       # review the payload
apply_branch_protection(dry_run=False)      # enforce
audit_governance()                          # compliant: true
```

For an organization, do it once instead of per repo:

```text
scaffold_ci(mode="publish")   # write the reusable workflow into <org>/.github
apply_org_ruleset("<org>", workflow_repo="<org>/.github", dry_run=False)
```

### Structural gate in CI

`codebase-memory-gate` indexes the head state and runs `detect_changes`, exiting
non-zero when `gate_should_block` is true:

```bash
codebase-memory-gate --base origin/main        # exit 1 blocks the build
codebase-memory-gate --base origin/main --json # full report
codebase-memory-gate --advisory                # report only, always exit 0
```

Note: a brand-new **untracked** file is absent from `git diff HEAD` — use the
`base...head` range for committed PR analysis.

---

## Persistence & schema

SQLite, two tables:

```sql
nodes(id TEXT PRIMARY KEY, kind TEXT, name TEXT, file TEXT, line INT, end_line INT)
edges(src TEXT, dst TEXT, type TEXT, attrs TEXT)   -- attrs = JSON of edge attributes
      -- indexed on src, dst, type
```

Edge attributes are stored as a JSON blob (`attrs`) rather than fixed columns,
so structural edges (`resolved`) and `HTTP_CALLS` edges
(`source`/`confirmed`/`status`/`count`) share one schema. The `.zst` snapshot is
just this file, zstd‑compressed.

---

## Testing

```bash
pytest            # 55 tests, ~0.32 s
pytest -v         # per-test breakdown
```

| File | Tests | What it pins |
|---|---:|---|
| `test_governance.py` | 32 | registry/package resolution, generated workflow parses with one job per check, contexts match real job names, protection + ruleset payload shape, audit gap detection, `dry_run` makes no API call |
| `test_indexer.py` | 6 | symbol collection, `CONTAINS`/`IMPORTS`/`CALLS`, unique-name fallback (`resolved=False`), persistence roundtrip |
| `test_queries.py` | 6 | callers/callees, dead code + entrypoint exclusion, `by_name`/`by_kind`, `trace_path` (found & none), architecture |
| `test_changes.py` | 5 | diff parsing (modify/add/delete), high-risk blocks without tests, low-risk when no callers, test-change suppresses block — over a **real git repo** |
| `test_traces.py` | 6 | endpoint normalization, static HTTP inference, confirmed/runtime_only/unconfirmed buckets, unknown callers, `.zst` roundtrip |

---

## Limitations & deliberate scope choices

These are intentional given the design and the chosen backend, not oversights:

- **Structured queries, not Cypher.** The backend is SQLite + networkx (chosen
  over Kùzu/Neo4j), so `search_graph` exposes named query kinds rather than an
  open query language.
- **Python only.** Indexing uses the stdlib `ast` module. Multi-language would
  mean adding tree-sitter.
- **Static call resolution is conservative.** Method calls on variables resolve
  only via the unique-name heuristic; non-unique or dynamically-dispatched calls
  are left to runtime (`ingest_traces`) — exactly the loop the doc describes.
- **`HTTP_CALLS` static inference is literal-URL only.** `requests`/`httpx`/
  `aiohttp`/`urllib` calls with a string-literal URL. f-string / config-driven
  URLs are the gap runtime traces fill by design.
- **Dead code isn't decorator-aware.** `@property` accessors and
  `@mcp.tool()`-style framework entrypoints show as zero-caller (correct by the
  doc's definition, but noise if you want them excluded).
- **`HIGH_FANIN = 5`** is a hardcoded threshold.
- **Untracked new files** don't appear in `git diff HEAD` (a git limitation);
  use `base...head` for committed PRs.
- **The graph check is advisory**, not merge-blocking. Blast radius is a
  heuristic; a noisy blocking gate only teaches people to bypass it. Promote it
  after calibrating `HIGH_FANIN` on real PRs.
- **Governance checks are Python-only**, matching the indexer. A Node or Go repo
  still gets protection and rulesets, but needs a second check registry.
- **Branch protection on private repos** requires a paid GitHub plan; org
  rulesets covering private repos require Team/Enterprise. Both are free for
  public repositories, and both tools surface this rather than a bare 403.
- **`gh` is required for the remote plane.** Without it, `audit_governance`
  reports the local plane only and the apply tools work in `dry_run` alone.

---

## Project layout

```
codebase_memory/
  __init__.py      7    package exports
  graph.py       214    CodeGraph: MultiDiGraph + SQLite persistence + snapshot paths
  indexer.py     339    two-pass AST indexer, call resolution, static HTTP inference
  queries.py     196    read/traversal queries (search_graph, architecture, trace_path)
  changes.py     214    detect_changes: git diff → blast radius → risk
  traces.py      135    ingest_traces reconciliation + .zst snapshot save/load
  server.py      382    FastMCP server wiring the 14 tools
  governance/
    checks.py    159    the CHECKS registry — single source of truth
    policy.py    139    desired state + GitHub API payload builders
    scaffold.py  214    CHECKS → workflow YAML (standalone/reusable/caller)
    audit.py     297    actual-vs-policy across local, remote, and org planes
    apply.py     158    branch protection + org ruleset, dry_run by default
    gh.py        153    thin `gh` CLI wrapper (no token ever held)
    gate.py       57    codebase-memory-gate CLI for the CI step
    runner.py     93    run the registry locally
.github/workflows/
  quality-gate.yml           generated by scaffold_ci — this repo dogfoods it
  reusable-quality-gate.yml  the org-shareable definition
tests/           730    55 tests (indexer, queries, changes, traces, governance)
```

*Source: 2,763 lines across 15 modules; tests: 730 lines. (Measured via `wc -l`.)*
