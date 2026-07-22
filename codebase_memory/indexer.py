"""Build a :class:`CodeGraph` from a Python codebase using the ``ast`` module.

Two passes:

1. **Collect symbols** — every module, class, and function/method becomes a node
   with a fully-qualified id, and a per-module symbol table is built for call
   resolution.
2. **Resolve relations** — module-level imports become IMPORTS edges; calls
   inside function bodies are resolved against project symbols and become CALLS
   edges. Calls that resolve to standard-library or third-party code are counted
   but not edged (they are outside the project's blast radius).

Call resolution is deliberately best-effort and conservative: we only emit a
CALLS edge when the callee binds to a known project symbol. The number of
unresolved calls is reported so the coverage is never silently hidden.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path

from codebase_memory.graph import (
    CALLS,
    CLASS,
    CONTAINS,
    FUNCTION,
    IMPORTS,
    MODULE,
    CodeGraph,
)

DEFAULT_EXCLUDES = {
    ".git",
    ".codebase-memory",
    "__pycache__",
    ".venv",
    "venv",
    "env",
    "node_modules",
    "build",
    "dist",
    ".pytest_cache",
    ".mypy_cache",
    ".tox",
}


@dataclass
class BuildReport:
    files_parsed: int = 0
    parse_errors: list[tuple[str, str]] = field(default_factory=list)
    unresolved_calls: int = 0

    def as_dict(self) -> dict:
        return {
            "files_parsed": self.files_parsed,
            "parse_errors": [{"file": f, "error": e} for f, e in self.parse_errors],
            "unresolved_calls": self.unresolved_calls,
        }


def module_name_for(root: Path, path: Path) -> str:
    """Dotted module path of ``path`` relative to ``root`` (drops __init__)."""
    rel = path.relative_to(root).with_suffix("")
    parts = list(rel.parts)
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts) if parts else path.stem


def _iter_python_files(root: Path, excludes: set[str]):
    for path in sorted(root.rglob("*.py")):
        if any(part in excludes for part in path.relative_to(root).parts):
            continue
        yield path


def index_project(
    root: str | Path,
    excludes: set[str] | None = None,
) -> tuple[CodeGraph, BuildReport]:
    """Index the Python project rooted at ``root`` into a :class:`CodeGraph`."""
    root = Path(root).resolve()
    excludes = DEFAULT_EXCLUDES if excludes is None else excludes
    graph = CodeGraph()
    report = BuildReport()

    files: list[tuple[Path, str, ast.Module]] = []
    # Pass 1: collect symbols.
    for path in _iter_python_files(root, excludes):
        rel = str(path.relative_to(root))
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
        except (SyntaxError, UnicodeDecodeError) as exc:
            report.parse_errors.append((rel, str(exc)))
            continue
        report.files_parsed += 1
        mod = module_name_for(root, path)
        files.append((path, mod, tree))
        graph.add_symbol(mod, MODULE, name=mod, file=rel, line=1)
        _collect_symbols(graph, tree, mod, rel)

    # Index of module -> {short_name -> node_id} for resolving local names, and
    # a project-wide {short_name -> [ids]} of functions for unique-name fallback.
    module_index = _build_module_index(graph)
    fn_by_short_name = _build_short_name_index(graph)

    # Pass 2: relations.
    for path, mod, tree in files:
        rel = str(path.relative_to(root))
        _resolve_imports(graph, tree, mod)
        aliases = _import_aliases(tree)
        _resolve_calls(graph, tree, mod, rel, module_index, aliases, fn_by_short_name, report)

    return graph, report


# --------------------------------------------------------------------------- #
# Pass 1 helpers
# --------------------------------------------------------------------------- #
def _collect_symbols(graph: CodeGraph, tree: ast.Module, mod: str, rel: str) -> None:
    """Walk top-level defs, recursing into classes, emitting CONTAINS edges."""

    def visit(body: list[ast.stmt], parent_id: str) -> None:
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                node_id = f"{parent_id}.{node.name}"
                graph.add_symbol(node_id, FUNCTION, name=node.name, file=rel, line=node.lineno)
                graph.add_relation(parent_id, node_id, CONTAINS)
            elif isinstance(node, ast.ClassDef):
                node_id = f"{parent_id}.{node.name}"
                graph.add_symbol(node_id, CLASS, name=node.name, file=rel, line=node.lineno)
                graph.add_relation(parent_id, node_id, CONTAINS)
                visit(node.body, node_id)

    visit(tree.body, mod)


def _build_short_name_index(graph: CodeGraph) -> dict[str, list[str]]:
    """Map each function short name to every function node id that has it."""
    index: dict[str, list[str]] = {}
    for node_id in graph.nodes_of_kind(FUNCTION):
        index.setdefault(graph.g.nodes[node_id].get("name", ""), []).append(node_id)
    return index


def _build_module_index(graph: CodeGraph) -> dict[str, dict[str, str]]:
    """For each module, map the short name of each contained symbol to its id."""
    index: dict[str, dict[str, str]] = {}
    for src, dst in graph.edges_of_type(CONTAINS):
        table = index.setdefault(src, {})
        short = graph.g.nodes[dst].get("name", dst.rsplit(".", 1)[-1])
        table[short] = dst
    return index


# --------------------------------------------------------------------------- #
# Pass 2 helpers
# --------------------------------------------------------------------------- #
def _resolve_imports(graph: CodeGraph, tree: ast.Module, mod: str) -> None:
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                target = alias.name
                graph.add_relation(mod, target, IMPORTS, resolved=graph.g.has_node(target))
        elif isinstance(node, ast.ImportFrom):
            if node.module is None:  # relative "from . import x" — skip target
                continue
            for alias in node.names:
                symbol = f"{node.module}.{alias.name}"
                # Prefer the concrete imported symbol node; fall back to the module.
                if graph.g.has_node(symbol):
                    graph.add_relation(mod, symbol, IMPORTS, resolved=True)
                else:
                    graph.add_relation(mod, node.module, IMPORTS, resolved=graph.g.has_node(node.module))


def _import_aliases(tree: ast.Module) -> dict[str, str]:
    """Map local binding -> dotted source for module-level imports.

    ``import a.b as c``    -> {"c": "a.b"}
    ``import a.b``         -> {"a": "a"}  (a.b.f accessed via the head)
    ``from a.b import c``  -> {"c": "a.b.c"}
    """
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname:
                    aliases[alias.asname] = alias.name
                else:
                    aliases[alias.name.split(".")[0]] = alias.name.split(".")[0]
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            for alias in node.names:
                bind = alias.asname or alias.name
                aliases[bind] = f"{node.module}.{alias.name}"
    return aliases


def _dotted(node: ast.expr) -> str | None:
    """Flatten an attribute/name chain (``a.b.c``) to a dotted string."""
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return None


def _resolve_calls(
    graph: CodeGraph,
    tree: ast.Module,
    mod: str,
    rel: str,
    module_index: dict[str, dict[str, str]],
    aliases: dict[str, str],
    fn_by_short_name: dict[str, list[str]],
    report: BuildReport,
) -> None:
    """Emit CALLS edges from each function to the project symbols it calls.

    Returns a ``(target_id, confident)`` pair per resolved call. ``confident``
    is False for the unique-short-name fallback (a method call on a variable
    whose type we can't infer), which is stored as ``resolved=False`` so
    precision-sensitive consumers can filter it out.
    """
    locals_ = module_index.get(mod, {})

    def resolve(func: ast.expr, class_id: str | None) -> tuple[str, bool] | None:
        # self.method()  /  cls.method()
        if (
            isinstance(func, ast.Attribute)
            and isinstance(func.value, ast.Name)
            and func.value.id in ("self", "cls")
            and class_id is not None
        ):
            candidate = f"{class_id}.{func.attr}"
            return (candidate, True) if graph.g.has_node(candidate) else None
        # bare name: foo()
        if isinstance(func, ast.Name):
            if func.id in locals_:
                return locals_[func.id], True
            if func.id in aliases and graph.g.has_node(aliases[func.id]):
                return aliases[func.id], True
            return None
        # dotted: alias.func(), pkg.mod.func()
        dotted = _dotted(func)
        if dotted is None:
            return None
        head, _, rest = dotted.partition(".")
        if head in aliases and rest:
            candidate = f"{aliases[head]}.{rest}"
            if graph.g.has_node(candidate):
                return candidate, True
        if graph.g.has_node(dotted):
            return dotted, True
        # Fallback: var.method() where method name is unique project-wide.
        if isinstance(func, ast.Attribute):
            matches = fn_by_short_name.get(func.attr, [])
            if len(matches) == 1:
                return matches[0], False
        return None

    def walk_scope(body: list[ast.stmt], class_id: str | None) -> None:
        # Mirrors symbol collection: descend into classes, treat a function as a
        # leaf scope. Calls inside nested closures are attributed to the
        # enclosing collected function.
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                fn_id = f"{class_id}.{node.name}" if class_id else f"{mod}.{node.name}"
                for call in (n for n in ast.walk(node) if isinstance(n, ast.Call)):
                    resolved = resolve(call.func, class_id)
                    if resolved is None:
                        report.unresolved_calls += 1
                    elif resolved[0] != fn_id:
                        graph.add_relation(fn_id, resolved[0], CALLS, resolved=resolved[1])
            elif isinstance(node, ast.ClassDef):
                cid = f"{class_id}.{node.name}" if class_id else f"{mod}.{node.name}"
                walk_scope(node.body, cid)

    walk_scope(tree.body, None)
