"""MCP server exposing the codebase memory graph.

Phase 0-1 surface:
  - ``index_codebase``  build/refresh the structural graph and persist it
  - ``graph_stats``     summarize the persisted graph

Phase 2 surface (read/traversal over the persisted graph):
  - ``search_graph``    named structured queries (callers, callees, dead code, ...)
  - ``get_architecture``  per-module structure and inter-module dependencies
  - ``trace_path``      shortest call chain between two symbols

Phase 3 surface:
  - ``detect_changes``  git diff -> affected symbols, blast radius, risk

Phase 4 surface (close the loop + shareable snapshot):
  - ``ingest_traces``   reconcile runtime HTTP traffic against HTTP_CALLS edges
  - ``save_snapshot`` / ``load_snapshot``  committable .zst graph snapshot

Phase 5 surface (governance — works against any repo, not just this one):
  - ``audit_governance``        actual-vs-policy report (read-only)
  - ``run_quality_checks``      run the CI checks locally
  - ``scaffold_ci``             generate the required-status-check workflow
  - ``apply_branch_protection`` enforce protection on the default branch
  - ``apply_org_ruleset``       define the policy once, org-wide
"""

from __future__ import annotations

import os
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from codebase_memory import queries, traces
from codebase_memory.changes import detect_changes as _detect_changes
from codebase_memory.governance import apply as _apply
from codebase_memory.governance import audit as _audit
from codebase_memory.governance import runner as _runner
from codebase_memory.governance import scaffold as _scaffold
from codebase_memory.governance.policy import required_contexts as _required_contexts
from codebase_memory.graph import CodeGraph, default_db_path, default_snapshot_path
from codebase_memory.indexer import index_project

mcp = FastMCP("codebase-memory")


def _project_root(root: str | None) -> Path:
    """Resolve the project root: explicit arg, else CODEBASE_MEMORY_ROOT, else cwd."""
    return Path(root or os.environ.get("CODEBASE_MEMORY_ROOT") or ".").resolve()


def _load_graph(root: str | None) -> tuple[Path, CodeGraph | None]:
    """Load the persisted graph for ``root``; returns (db_path, graph|None)."""
    db_path = default_db_path(_project_root(root))
    if not db_path.exists():
        return db_path, None
    return db_path, CodeGraph.load(db_path)


@mcp.tool()
def index_codebase(root: str | None = None) -> dict:
    """Index a Python project into the structural graph and persist it.

    Args:
        root: Project root to index. Defaults to CODEBASE_MEMORY_ROOT or the
            current working directory.

    Returns node/edge statistics and a build report (files parsed, parse
    errors, and the count of calls that did not resolve to a project symbol).
    """
    project = _project_root(root)
    graph, report = index_project(project)
    db_path = default_db_path(project)
    graph.save(db_path)
    return {
        "root": str(project),
        "db_path": str(db_path),
        "stats": graph.stats(),
        "report": report.as_dict(),
    }


@mcp.tool()
def graph_stats(root: str | None = None) -> dict:
    """Return node/edge counts for the persisted graph, without re-indexing."""
    db_path, graph = _load_graph(root)
    if graph is None:
        return {"error": f"No graph at {db_path}. Run index_codebase first."}
    return {"db_path": str(db_path), "stats": graph.stats()}


SEARCH_KINDS = ("callers", "callees", "dead_code", "by_name", "by_kind")


@mcp.tool()
def search_graph(kind: str, target: str | None = None, root: str | None = None) -> dict:
    """Query the structural graph.

    Args:
        kind: One of ``callers``, ``callees``, ``dead_code``, ``by_name``,
            ``by_kind``.
        target: The query argument. Required for every kind except
            ``dead_code``: a fully-qualified symbol id for ``callers``/
            ``callees``, a name substring for ``by_name``, or a node kind
            (``module``/``class``/``function``) for ``by_kind``.
        root: Project root (defaults to CODEBASE_MEMORY_ROOT or cwd).

    Note: this is a structured-query surface, not Cypher — the graph backend is
    networkx.
    """
    db_path, graph = _load_graph(root)
    if graph is None:
        return {"error": f"No graph at {db_path}. Run index_codebase first."}
    if kind not in SEARCH_KINDS:
        return {"error": f"Unknown kind {kind!r}. Expected one of {list(SEARCH_KINDS)}."}
    try:
        if kind == "dead_code":
            return queries.find_dead_code(graph)
        if not target:
            return {"error": f"kind {kind!r} requires a 'target' argument."}
        if kind == "callers":
            return {"kind": kind, "target": target, "results": queries.callers_of(graph, target)}
        if kind == "callees":
            return {"kind": kind, "target": target, "results": queries.callees_of(graph, target)}
        if kind == "by_name":
            return {"kind": kind, "target": target, "results": queries.symbols_by_name(graph, target)}
        if kind == "by_kind":
            return {"kind": kind, "target": target, "results": queries.symbols_by_kind(graph, target)}
        return {"error": f"Unhandled kind {kind!r}."}
    except KeyError as exc:
        return {"error": str(exc)}


@mcp.tool()
def get_architecture(root: str | None = None) -> dict:
    """Return per-module structure and internal module-to-module dependencies."""
    db_path, graph = _load_graph(root)
    if graph is None:
        return {"error": f"No graph at {db_path}. Run index_codebase first."}
    return queries.get_architecture(graph)


@mcp.tool()
def trace_path(source: str, target: str, root: str | None = None) -> dict:
    """Shortest call chain from ``source`` to ``target`` (fully-qualified ids)."""
    db_path, graph = _load_graph(root)
    if graph is None:
        return {"error": f"No graph at {db_path}. Run index_codebase first."}
    try:
        return queries.trace_path(graph, source, target)
    except KeyError as exc:
        return {"error": str(exc)}


@mcp.tool()
def detect_changes(
    base: str | None = None,
    head: str | None = None,
    root: str | None = None,
) -> dict:
    """Map a git diff to affected symbols, blast radius, and a risk classification.

    Args:
        base: Base git ref. If omitted, diffs the working tree against HEAD.
            If set, diffs the merge-base range ``base...head``.
        head: Head git ref (defaults to HEAD when ``base`` is given).
        root: Project root / git repo (defaults to CODEBASE_MEMORY_ROOT or cwd).

    The persisted graph must reflect the ``head`` state, so run
    ``index_codebase`` first. Returns a deterministic ``gate_should_block`` flag
    for CI: true when a high-fan-in symbol changed without a test change.
    """
    project = _project_root(root)
    db_path, graph = _load_graph(root)
    if graph is None:
        return {"error": f"No graph at {db_path}. Run index_codebase first."}
    try:
        return _detect_changes(graph, project, base=base, head=head)
    except RuntimeError as exc:
        return {"error": str(exc)}


@mcp.tool()
def ingest_traces(traces_data: list[dict], root: str | None = None) -> dict:
    """Reconcile runtime HTTP traces against the graph's HTTP_CALLS edges.

    Args:
        traces_data: Observed outbound calls, each
            ``{"caller": <symbol id>, "method": "GET", "url": "...",
            "count": <int, optional>}``.
        root: Project root (defaults to CODEBASE_MEMORY_ROOT or cwd).

    Confirms static edges seen in traffic, adds runtime-only corrections, and
    flags static edges never exercised. Persists the updated graph.
    """
    db_path, graph = _load_graph(root)
    if graph is None:
        return {"error": f"No graph at {db_path}. Run index_codebase first."}
    report = traces.ingest_traces(graph, traces_data)
    graph.save(db_path)
    return report


@mcp.tool()
def save_snapshot(root: str | None = None) -> dict:
    """Compress the graph database to the committable ``graph.db.zst`` snapshot."""
    project = _project_root(root)
    db_path = default_db_path(project)
    if not db_path.exists():
        return {"error": f"No graph at {db_path}. Run index_codebase first."}
    snapshot = traces.save_snapshot(db_path, default_snapshot_path(project))
    return {
        "snapshot": str(snapshot),
        "db_bytes": db_path.stat().st_size,
        "snapshot_bytes": snapshot.stat().st_size,
    }


@mcp.tool()
def load_snapshot(root: str | None = None) -> dict:
    """Restore ``graph.db`` from the committed ``graph.db.zst`` snapshot."""
    project = _project_root(root)
    snapshot = default_snapshot_path(project)
    if not snapshot.exists():
        return {"error": f"No snapshot at {snapshot}."}
    graph = traces.load_snapshot(snapshot, default_db_path(project))
    return {"snapshot": str(snapshot), "stats": graph.stats()}


@mcp.tool()
def audit_governance(root: str | None = None, repo: str | None = None) -> dict:
    """Report a repository's governance state against the policy. Read-only.

    Call this first. Covers three planes: the workflow files on disk, the
    repository's branch protection on GitHub, and any organization ruleset that
    covers it. Each gap names the tool that closes it.

    Args:
        root: Project root (defaults to CODEBASE_MEMORY_ROOT or cwd).
        repo: ``owner/name``. Inferred from the git remote when omitted.

    Degrades to the local plane — never an error — when the ``gh`` CLI is
    missing or unauthenticated.
    """
    return _audit.audit(_project_root(root), repo=repo)


@mcp.tool()
def run_quality_checks(root: str | None = None, checks: list[str] | None = None) -> dict:
    """Run the same lint / typecheck / test / build / security checks CI runs.

    Args:
        root: Project root (defaults to CODEBASE_MEMORY_ROOT or cwd).
        checks: Check ids to run — ``lint``, ``typecheck``, ``test``, ``build``,
            ``deps``, ``sast``, ``graph``. Runs all of them when omitted.

    A check whose tool is not installed is reported as ``skipped``, never as
    passed. ``would_block_merge`` mirrors what the CI gate would decide.
    """
    return _runner.run_checks(_project_root(root), ids=checks)


@mcp.tool()
def scaffold_ci(
    root: str | None = None,
    mode: str = "reusable",
    org: str | None = None,
    python_version: str = "3.11",
    default_branch: str = "main",
    dry_run: bool = True,
    overwrite: bool = False,
) -> dict:
    """Generate the GitHub Actions workflow that backs the required status checks.

    Args:
        root: Target repository root (defaults to CODEBASE_MEMORY_ROOT or cwd).
        mode: ``reusable`` — a short caller workflow delegating to
            ``<org>/.github`` (requires ``org``); ``standalone`` — the full
            workflow inlined; ``publish`` — the org-side reusable definition.
        org: GitHub organization, required for ``mode='reusable'``.
        python_version: Python version the workflow sets up.
        default_branch: Branch the ``push`` trigger watches.
        dry_run: Return the rendered YAML without writing it.
        overwrite: Replace an existing workflow file.

    Every check becomes its own job, so each one is a separately required status
    check context.
    """
    project = _project_root(root)
    try:
        filename, content = _scaffold.render(
            project,
            mode=mode,
            org=org,
            python_version=python_version,
            default_branch=default_branch,
        )
    except ValueError as exc:
        return {"error": str(exc)}

    target = project / _scaffold.WORKFLOW_DIR / filename
    result = {
        "mode": mode,
        "path": str(target),
        "content": content,
        "required_contexts": _required_contexts(
            "reusable" if mode == "reusable" else "standalone"
        ),
    }
    if dry_run:
        return {**result, "dry_run": True, "hint": "Re-run with dry_run=False to write."}
    try:
        written = _scaffold.write_workflow(project, filename, content, overwrite=overwrite)
    except FileExistsError as exc:
        return {"error": str(exc)}
    return {**result, "written": str(written)}


@mcp.tool()
def apply_branch_protection(
    repo: str | None = None,
    branch: str | None = None,
    mode: str = "standalone",
    dry_run: bool = True,
    root: str | None = None,
) -> dict:
    """Protect a branch: required checks, ≥1 review, no force-push, no deletion.

    Args:
        repo: ``owner/name``. Inferred from the git remote when omitted.
        branch: Branch to protect. Defaults to the repository's default branch.
        mode: ``standalone`` or ``reusable`` — determines the context names, since
            a called workflow reports as ``quality-gate / <job>``.
        dry_run: Return the exact API call instead of making it.
        root: Project root used to infer ``repo``.

    Warns when a required context has never reported: GitHub accepts unknown
    context names, and one that never reports blocks every pull request forever.
    Run ``scaffold_ci`` and let the workflow run once first.
    """
    return _apply.apply_branch_protection(
        repo=repo, branch=branch, mode=mode, dry_run=dry_run, root=_project_root(root)
    )


@mcp.tool()
def apply_org_ruleset(
    org: str,
    name: str = "student-project-baseline",
    repo_pattern: str = "~ALL",
    workflow_repo: str | None = None,
    dry_run: bool = True,
) -> dict:
    """Define the policy once at the organization level, inherited by every repo.

    Args:
        org: GitHub organization login.
        name: Ruleset name.
        repo_pattern: Repositories to target; ``~ALL`` covers every repo.
        workflow_repo: ``owner/name`` hosting the reusable workflow. When given,
            the ruleset also pins it as a required workflow.
        dry_run: Return the exact API call instead of making it.

    Requires ``admin:org`` scope. Never called implicitly by another tool — the
    blast radius is org-wide, so it takes a deliberate invocation.
    """
    return _apply.apply_org_ruleset(
        org=org,
        name=name,
        repo_pattern=repo_pattern,
        workflow_repo=workflow_repo,
        dry_run=dry_run,
    )


def main() -> None:
    """Console-script entry point: run the server over stdio."""
    mcp.run()


if __name__ == "__main__":
    main()
