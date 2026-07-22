"""MCP server exposing the codebase memory graph.

Phase 0-1 surface:
  - ``index_codebase``  build/refresh the structural graph and persist it
  - ``graph_stats``     summarize the persisted graph

Read/traversal tools (search_graph, get_architecture, trace_path,
detect_changes, ingest_traces) are introduced in later phases.
"""

from __future__ import annotations

import os
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from codebase_memory.graph import CodeGraph, default_db_path
from codebase_memory.indexer import index_project

mcp = FastMCP("codebase-memory")


def _project_root(root: str | None) -> Path:
    """Resolve the project root: explicit arg, else CODEBASE_MEMORY_ROOT, else cwd."""
    return Path(root or os.environ.get("CODEBASE_MEMORY_ROOT") or ".").resolve()


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
    project = _project_root(root)
    db_path = default_db_path(project)
    if not db_path.exists():
        return {"error": f"No graph at {db_path}. Run index_codebase first."}
    graph = CodeGraph.load(db_path)
    return {"root": str(project), "db_path": str(db_path), "stats": graph.stats()}


def main() -> None:
    """Console-script entry point: run the server over stdio."""
    mcp.run()


if __name__ == "__main__":
    main()
