"""Run the CHECKS registry locally — the same commands CI runs.

This is what keeps the gate honest. Local and CI both execute from
``checks.CHECKS``, so "it passed on my machine" and "it passed in the workflow"
mean the same thing.

A check whose binary is not installed is reported as ``skipped``, never as
``passed``: a green summary that silently omits half the checks is worse than a
red one.
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

from codebase_memory.governance.checks import find_binary, resolve_package, select

TAIL_LINES = 20
DEFAULT_TIMEOUT = 600


def _tail(text: str) -> str:
    lines = text.strip().splitlines()
    return "\n".join(lines[-TAIL_LINES:])


def run_checks(
    root: str | Path,
    ids: list[str] | None = None,
    timeout: int = DEFAULT_TIMEOUT,
) -> dict:
    """Execute the selected checks in ``root`` and summarize the results."""
    root = Path(root)
    checks, unknown = select(ids)
    package = resolve_package(root)
    results: list[dict] = []

    for check in checks:
        cmd = check.resolve(package)
        binary = find_binary(cmd[0])
        if binary is None:
            results.append(
                {
                    "id": check.id,
                    "category": check.category,
                    "blocking": check.blocking,
                    "status": "skipped",
                    "command": " ".join(cmd),
                    "reason": f"{cmd[0]} not installed (pip install {check.install})",
                }
            )
            continue

        started = time.monotonic()
        try:
            proc = subprocess.run(
                [binary, *cmd[1:]], cwd=str(root), capture_output=True, text=True, timeout=timeout
            )
            output, code = proc.stdout + proc.stderr, proc.returncode
        except subprocess.TimeoutExpired:
            output, code = f"timed out after {timeout}s", 124
        except OSError as exc:
            output, code = str(exc), 127

        results.append(
            {
                "id": check.id,
                "category": check.category,
                "blocking": check.blocking,
                "status": "passed" if code == 0 else "failed",
                "exit_code": code,
                "command": " ".join(cmd),
                "duration_s": round(time.monotonic() - started, 2),
                "output_tail": _tail(output),
            }
        )

    failed = [r for r in results if r["status"] == "failed"]
    return {
        "root": str(root.resolve()),
        "package": package,
        "unknown_check_ids": unknown,
        "results": results,
        "summary": {
            "passed": sum(1 for r in results if r["status"] == "passed"),
            "failed": len(failed),
            "skipped": sum(1 for r in results if r["status"] == "skipped"),
        },
        "blocking_failures": [r["id"] for r in failed if r["blocking"]],
        "would_block_merge": any(r["blocking"] for r in failed),
    }
