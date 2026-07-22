"""MCP server exposing the codebase memory graph.

Phase 0-1 surface:
  - ``index_codebase``  build/refresh the structural graph and persist it
  - ``graph_stats``     summarize the persisted graph

Phase 2 surface (read/traversal over the persisted graph):
  - ``search_graph``    named structured queries (callers, callees, dead code, ...)
  - ``get_architecture``  per-module structure and inter-module dependencies
  - ``trace_path``      shortest call chain between two symbols

detect_changes and ingest_traces are introduced in later phases.
"""

from __future__ import annotations

import os
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from codebase_memory import queries
from codebase_memory.graph import CodeGraph, default_db_path
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
    if kind != "dead_code" and not target:
        return {"error": f"kind {kind!r} requires a 'target' argument."}
    try:
        if kind == "dead_code":
            return queries.find_dead_code(graph)
        if kind == "callers":
            return {"kind": kind, "target": target, "results": queries.callers_of(graph, target)}
        if kind == "callees":
            return {"kind": kind, "target": target, "results": queries.callees_of(graph, target)}
        if kind == "by_name":
            return {"kind": kind, "target": target, "results": queries.symbols_by_name(graph, target)}
        if kind == "by_kind":
            return {"kind": kind, "target": target, "results": queries.symbols_by_kind(graph, target)}
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


def main() -> None:
    """Console-script entry point: run the server over stdio."""
    mcp.run()


if __name__ == "__main__":
    main()
