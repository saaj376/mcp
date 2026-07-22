"""``detect_changes``: map a git diff to affected symbols and their blast radius.

Given a diff (working tree vs HEAD, or a base..head range), this:

1. parses the changed line ranges per file,
2. maps each changed line to its smallest enclosing symbol in the graph,
3. computes the blast radius — the transitive callers of every affected symbol
   (plus direct importers, for module-level changes), and
4. classifies risk from the largest fan-in touched and whether tests changed.

The output is deterministic (no model judgment), so it can drive a CI gate:
``gate_should_block`` is true when a high-fan-in symbol changed without any
accompanying test change — exactly the Layer 1 signal from the design doc.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import networkx as nx

from codebase_memory.graph import CALLS, IMPORTS, MODULE, CodeGraph

_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")

# A high-fan-in change touching this many transitive callers is "high" risk.
HIGH_FANIN = 5


def run_git_diff(root: str | Path, base: str | None, head: str | None) -> str:
    """Return the unified (context-0) diff for the requested range."""
    if base is None:
        rng = ["HEAD"]  # working tree vs HEAD
    else:
        rng = [f"{base}...{head or 'HEAD'}"]  # merge-base range, PR-style
    cmd = ["git", "-C", str(root), "diff", "--no-color", "--unified=0", *rng]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"git diff failed: {result.stderr.strip()}")
    return result.stdout


def _strip_prefix(path: str) -> str:
    for pre in ("a/", "b/"):
        if path.startswith(pre):
            return path[len(pre):]
    return path


def parse_unified_diff(text: str) -> dict:
    """Parse a context-0 unified diff.

    Returns ``changed_lines`` (new-side line numbers per file), plus the sets of
    added and deleted file paths.
    """
    changed: dict[str, set[int]] = {}
    added: set[str] = set()
    deleted: set[str] = set()
    old: str | None = None
    new: str | None = None

    for line in text.splitlines():
        if line.startswith("--- "):
            p = line[4:].strip()
            old = None if p == "/dev/null" else _strip_prefix(p)
        elif line.startswith("+++ "):
            p = line[4:].strip()
            new = None if p == "/dev/null" else _strip_prefix(p)
            if new is not None and old is None:
                added.add(new)
            if new is None and old is not None:
                deleted.add(old)
        elif line.startswith("@@"):
            m = _HUNK.match(line)
            if not m or new is None:
                continue
            start = int(m.group(1))
            count = int(m.group(2)) if m.group(2) is not None else 1
            for ln in range(start, start + count):  # count 0 => pure deletion, skipped
                changed.setdefault(new, set()).add(ln)

    return {"changed_lines": changed, "added_files": added, "deleted_files": deleted}


def _is_test_path(path: str) -> bool:
    base = path.rsplit("/", 1)[-1]
    return (
        base.startswith("test_")
        or base.endswith("_test.py")
        or "/tests/" in f"/{path}"
        or path.startswith("tests/")
    )


def _symbols_by_file(graph: CodeGraph) -> dict[str, list[tuple[int, int, str]]]:
    by_file: dict[str, list[tuple[int, int, str]]] = {}
    for node_id, a in graph.g.nodes(data=True):
        f = a.get("file")
        if f:
            by_file.setdefault(f, []).append((a.get("line", 0), a.get("end_line", 0), node_id))
    return by_file


def _enclosing_symbol(candidates: list[tuple[int, int, str]], line: int) -> str | None:
    """Smallest-span symbol whose [start, end] contains ``line``."""
    best: str | None = None
    best_span = None
    for start, end, node_id in candidates:
        if start <= line <= end:
            span = end - start
            if best_span is None or span < best_span:
                best, best_span = node_id, span
    return best


def detect_changes(
    graph: CodeGraph,
    root: str | Path,
    base: str | None = None,
    head: str | None = None,
) -> dict:
    """Map the diff to affected symbols, blast radius, and a risk classification."""
    diff = run_git_diff(root, base, head)
    parsed = parse_unified_diff(diff)
    changed_lines = parsed["changed_lines"]

    by_file = _symbols_by_file(graph)
    calls = nx.DiGraph()
    calls.add_nodes_from(graph.g.nodes())
    calls.add_edges_from(graph.edges_of_type(CALLS))

    # Reverse IMPORTS for module-level blast radius (who imports this module).
    importers: dict[str, set[str]] = {}
    for src_mod, dst in graph.edges_of_type(IMPORTS):
        importers.setdefault(dst, set()).add(src_mod)

    affected: dict[str, dict] = {}
    unmapped: list[dict] = []
    for path, lines in sorted(changed_lines.items()):
        if not path.endswith(".py"):
            unmapped.append({"file": path, "reason": "non-Python file"})
            continue
        candidates = by_file.get(path)
        if not candidates:
            unmapped.append({"file": path, "reason": "file not in graph (run index_codebase?)"})
            continue
        for ln in lines:
            sym = _enclosing_symbol(candidates, ln)
            if sym is None:
                continue
            affected.setdefault(sym, {"lines": set()})["lines"].add(ln)

    blast: set[str] = set()
    symbols_out: list[dict] = []
    max_fanin = 0
    for sym in sorted(affected):
        attrs = graph.g.nodes[sym]
        transitive = nx.ancestors(calls, sym) if calls.has_node(sym) else set()
        if attrs.get("kind") == MODULE:
            transitive = transitive | importers.get(sym, set())
        direct = sorted(set(calls.predecessors(sym)) if calls.has_node(sym) else set())
        max_fanin = max(max_fanin, len(transitive))
        blast |= transitive
        symbols_out.append(
            {
                "id": sym,
                "kind": attrs.get("kind", ""),
                "file": attrs.get("file", ""),
                "changed_lines": sorted(affected[sym]["lines"]),
                "fan_in": len(transitive),
                "direct_callers": direct,
            }
        )

    tests_changed = any(
        _is_test_path(p) for p in set(changed_lines) | parsed["added_files"] | parsed["deleted_files"]
    )
    risk, reasons = _classify(symbols_out, max_fanin, tests_changed)

    return {
        "base": base,
        "head": head or "HEAD",
        "changed_files": sorted(changed_lines),
        "added_files": sorted(parsed["added_files"]),
        "deleted_files": sorted(parsed["deleted_files"]),
        "tests_changed": tests_changed,
        "affected_symbols": symbols_out,
        "blast_radius": sorted(blast - set(affected)),
        "blast_radius_size": len(blast - set(affected)),
        "risk": risk,
        "risk_reasons": reasons,
        "gate_should_block": risk == "high" and not tests_changed,
        "unmapped_changes": unmapped,
    }


def _classify(symbols: list[dict], max_fanin: int, tests_changed: bool) -> tuple[str, list[str]]:
    reasons: list[str] = []
    if not symbols:
        return "low", ["No code symbols affected by the diff."]
    if max_fanin >= HIGH_FANIN:
        risk = "high"
        reasons.append(f"A changed symbol has {max_fanin} transitive callers (>= {HIGH_FANIN}).")
    elif max_fanin >= 1:
        risk = "medium"
        reasons.append(f"Changed symbols have callers (max fan-in {max_fanin}).")
    else:
        risk = "low"
        reasons.append("Changed symbols have no in-project callers.")
    if risk == "high" and not tests_changed:
        reasons.append("High-fan-in change with no accompanying test change.")
    return risk, reasons
