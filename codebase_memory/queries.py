"""Read/traversal queries over a :class:`CodeGraph`.

These are the structured equivalents of the design doc's ``search_graph`` /
``get_architecture`` / ``trace_path`` operations. Because the backend is
networkx (not a Cypher engine), ``search_graph`` exposes a small set of named
query kinds rather than an open query language.

All functions are pure: they read a graph and return plain dict/list data ready
to serialize over MCP.
"""

from __future__ import annotations

import networkx as nx

from codebase_memory.graph import (
    CALLS,
    CLASS,
    CONTAINS,
    FUNCTION,
    IMPORTS,
    MODULE,
    CodeGraph,
)


def _node_view(graph: CodeGraph, node_id: str) -> dict:
    attrs = graph.g.nodes[node_id]
    return {
        "id": node_id,
        "kind": attrs.get("kind", ""),
        "name": attrs.get("name", ""),
        "file": attrs.get("file", ""),
        "line": attrs.get("line", 0),
    }


def _typed_subgraph(graph: CodeGraph, edge_type: str) -> nx.DiGraph:
    """A simple DiGraph of just the edges of one type, for traversal."""
    sub = nx.DiGraph()
    sub.add_nodes_from(graph.g.nodes())
    sub.add_edges_from(graph.edges_of_type(edge_type))
    return sub


# --------------------------------------------------------------------------- #
# search_graph query kinds
# --------------------------------------------------------------------------- #
def callers_of(graph: CodeGraph, symbol: str) -> list[dict]:
    """Direct callers of ``symbol`` (incoming CALLS edges)."""
    _require_node(graph, symbol)
    return [
        _node_view(graph, src)
        for src, dst in graph.edges_of_type(CALLS)
        if dst == symbol
    ]


def callees_of(graph: CodeGraph, symbol: str) -> list[dict]:
    """Direct project symbols called by ``symbol`` (outgoing CALLS edges)."""
    _require_node(graph, symbol)
    return [
        _node_view(graph, dst)
        for src, dst in graph.edges_of_type(CALLS)
        if src == symbol
    ]


def symbols_by_name(graph: CodeGraph, name: str) -> list[dict]:
    """Symbols whose short name contains ``name`` (case-insensitive)."""
    needle = name.lower()
    return [
        _node_view(graph, n)
        for n, a in graph.g.nodes(data=True)
        if needle in a.get("name", "").lower()
    ]


def symbols_by_kind(graph: CodeGraph, kind: str) -> list[dict]:
    """All symbols of a given kind (module/class/function)."""
    return [_node_view(graph, n) for n in graph.nodes_of_kind(kind)]


def find_dead_code(graph: CodeGraph) -> dict:
    """Functions with no incoming CALLS and no incoming IMPORTS.

    Likely entrypoints (``main``, ``test_*``, dunder methods) are reported
    separately rather than silently dropped, since they legitimately have no
    in-project callers.
    """
    called = {dst for _, dst in graph.edges_of_type(CALLS)}
    imported = {dst for _, dst in graph.edges_of_type(IMPORTS)}
    dead: list[dict] = []
    excluded: list[dict] = []
    for node_id in graph.nodes_of_kind(FUNCTION):
        if node_id in called or node_id in imported:
            continue
        view = _node_view(graph, node_id)
        (excluded if _is_probable_entrypoint(view["name"]) else dead).append(view)
    return {"dead_code": dead, "excluded_as_entrypoints": excluded}


def _is_probable_entrypoint(name: str) -> bool:
    return (
        name == "main"
        or name.startswith("test_")
        or (name.startswith("__") and name.endswith("__"))
    )


# --------------------------------------------------------------------------- #
# trace_path
# --------------------------------------------------------------------------- #
def trace_path(graph: CodeGraph, source: str, target: str) -> dict:
    """Shortest call chain from ``source`` to ``target`` over CALLS edges."""
    _require_node(graph, source)
    _require_node(graph, target)
    calls = _typed_subgraph(graph, CALLS)
    try:
        path = nx.shortest_path(calls, source, target)
    except nx.NetworkXNoPath:
        return {"source": source, "target": target, "path": None, "found": False}
    return {
        "source": source,
        "target": target,
        "path": [_node_view(graph, n) for n in path],
        "hops": len(path) - 1,
        "found": True,
    }


# --------------------------------------------------------------------------- #
# get_architecture
# --------------------------------------------------------------------------- #
def get_architecture(graph: CodeGraph) -> dict:
    """High-level structure: per-module contents and inter-module dependencies."""
    # Map every symbol to its owning module (walk CONTAINS up to a module).
    owner = _symbol_to_module(graph)

    modules = [_module_summary(graph, mod) for mod in sorted(graph.nodes_of_kind(MODULE))]

    # Collapse symbol-level IMPORTS to internal module -> module dependencies.
    deps: set[tuple[str, str]] = set()
    for src_mod, dst in graph.edges_of_type(IMPORTS):
        dst_mod = owner.get(dst, dst if graph.g.nodes.get(dst, {}).get("kind") == MODULE else None)
        if dst_mod and dst_mod != src_mod and graph.g.has_node(dst_mod):
            deps.add((src_mod, dst_mod))

    return {
        "modules": modules,
        "module_dependencies": sorted([list(d) for d in deps]),
        "stats": graph.stats(),
    }


def _module_summary(graph: CodeGraph, mod: str) -> dict:
    classes, functions = [], []
    for src, dst in graph.edges_of_type(CONTAINS):
        if src != mod:
            continue
        kind = graph.g.nodes[dst].get("kind")
        if kind == CLASS:
            classes.append(graph.g.nodes[dst].get("name", dst))
        elif kind == FUNCTION:
            functions.append(graph.g.nodes[dst].get("name", dst))
    return {
        "id": mod,
        "file": graph.g.nodes[mod].get("file", ""),
        "classes": sorted(classes),
        "functions": sorted(functions),
        "num_symbols": len(classes) + len(functions),
    }


def _symbol_to_module(graph: CodeGraph) -> dict[str, str]:
    """Map each symbol id to the module that (transitively) contains it."""
    parent: dict[str, str] = {}
    for src, dst in graph.edges_of_type(CONTAINS):
        parent[dst] = src
    owner: dict[str, str] = {}
    for node_id, a in graph.g.nodes(data=True):
        if a.get("kind") == MODULE:
            owner[node_id] = node_id
            continue
        cur = node_id
        while cur in parent:
            cur = parent[cur]
        if graph.g.nodes.get(cur, {}).get("kind") == MODULE:
            owner[node_id] = cur
    return owner


# --------------------------------------------------------------------------- #
def _require_node(graph: CodeGraph, node_id: str) -> None:
    if not graph.g.has_node(node_id):
        raise KeyError(f"Symbol not in graph: {node_id!r}")
