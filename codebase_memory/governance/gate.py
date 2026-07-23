"""``codebase-memory-gate``: the structural check, as a CI-invocable command.

A GitHub Actions step needs an exit code, not an MCP tool call. This wraps
``index_codebase`` + ``detect_changes`` into one command that exits non-zero when
``gate_should_block`` is true — the Layer 1 gate from the design doc.
"""

from __future__ import annotations

import argparse
import json
import sys

from codebase_memory.changes import detect_changes
from codebase_memory.graph import default_db_path
from codebase_memory.indexer import index_project


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="codebase-memory-gate", description=__doc__)
    parser.add_argument("--root", default=".", help="Project root / git repository.")
    parser.add_argument("--base", default=None, help="Base ref (e.g. origin/main).")
    parser.add_argument("--head", default=None, help="Head ref (defaults to HEAD).")
    parser.add_argument("--json", action="store_true", help="Emit the full report as JSON.")
    parser.add_argument(
        "--advisory",
        action="store_true",
        help="Always exit 0; report the verdict without failing the build.",
    )
    args = parser.parse_args(argv)

    graph, _ = index_project(args.root)
    graph.save(default_db_path(args.root))
    try:
        report = detect_changes(graph, args.root, base=args.base, head=args.head)
    except RuntimeError as exc:
        print(f"gate: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(f"risk={report['risk']} blast_radius={report['blast_radius_size']}")
        for symbol in report["affected_symbols"]:
            print(f"  {symbol['id']} (fan-in {symbol['fan_in']})")
        for reason in report["risk_reasons"]:
            print(f"  - {reason}")

    if report["gate_should_block"]:
        print("gate: BLOCK — high-fan-in change with no accompanying test change.")
        return 0 if args.advisory else 1
    print("gate: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
