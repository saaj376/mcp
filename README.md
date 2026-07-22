# codebase-memory-mcp

A persistent, queryable **structural knowledge graph** of a codebase, exposed
over the [Model Context Protocol](https://modelcontextprotocol.io). It gives
CI gates, AI code review, and runtime feedback loops one shared picture of the
code instead of each re-deriving repo structure from scratch.

The graph is a single SQLite file under `.codebase-memory/graph.db`, so it can
be committed alongside the repo and read by every agent session.

## Status — phased build

| Phase | Scope | State |
|------|-------|-------|
| 0 | Package scaffold, MCP server, graph store | ✅ |
| 1 | Python indexer → modules / classes / functions + CONTAINS / IMPORTS / CALLS edges | ✅ |
| 2 | Read tools: `search_graph`, `get_architecture`, `trace_path`, dead-code query | ✅ |
| 3 | `detect_changes` — git diff → blast radius + risk classification | ⏳ |
| 4 | `ingest_traces` — runtime `HTTP_CALLS` validation + `.zst` snapshot | ⏳ |

## Graph model

- **Nodes** — `module`, `class`, `function` (fully-qualified ids, e.g.
  `pkg.mod.Class.method`), each carrying `file` and `line`.
- **Edges** — `CONTAINS` (module→class/function, class→method),
  `IMPORTS` (module→imported module/symbol), `CALLS` (function→resolved
  project symbol). Calls into stdlib/third-party code are counted, not edged.

Call resolution is conservative and best-effort: an edge is only emitted when
the callee binds to a known project symbol. `self.`/`cls.` calls, module-local
calls, and imported calls resolve with high confidence (`resolved=True`). A
`var.method()` call whose method name is **unique project-wide** binds via a
lower-confidence fallback (`resolved=False`), so blast-radius consumers can
filter it out while dead-code detection still benefits. The number of calls
that resolve to no project symbol (stdlib/third-party) is reported so coverage
is never silently hidden.

## Read tools (Phase 2)

- **`search_graph(kind, target)`** — structured queries (not Cypher; the backend
  is networkx). Kinds: `callers`, `callees`, `by_name`, `by_kind`, and
  `dead_code` (functions with no incoming CALLS or IMPORTS; likely entrypoints
  like `main`/`test_*`/dunders are reported separately, not dropped).
- **`get_architecture()`** — per-module classes/functions plus internal
  module→module dependencies collapsed from the import graph.
- **`trace_path(source, target)`** — shortest call chain between two symbols
  over CALLS edges.

## Install

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
```

## Use

Run the tests:

```bash
pytest
```

Index a project from Python:

```python
from codebase_memory.indexer import index_project
from codebase_memory.graph import default_db_path

graph, report = index_project("/path/to/project")
graph.save(default_db_path("/path/to/project"))
print(graph.stats(), report.as_dict())
```

### As an MCP server

The server exposes `index_codebase` and `graph_stats` tools over stdio. Add to
an MCP client (e.g. Claude Code `.mcp.json`):

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

`CODEBASE_MEMORY_ROOT` (or the `root` tool argument) selects which project to
index; it defaults to the current working directory.
