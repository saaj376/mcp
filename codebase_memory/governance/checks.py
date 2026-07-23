"""The single registry of quality checks.

Three consumers read this one list, which is the whole point: the local runner
(``run_quality_checks``) executes it, the workflow generator (``scaffold.py``)
renders it into CI steps, and the branch-protection payload (``policy.py``)
derives its required status-check contexts from it. Adding a check here makes it
appear locally, in CI, and in the protection rule together — they cannot drift.

Commands may contain the ``{pkg}`` placeholder, resolved per-repository by
``resolve_package`` so the registry works against any project, not just this one.
"""

from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

# Directories that are never the project's importable package.
_NOT_PACKAGES = {"tests", "test", "docs", "examples", "scripts", "build", "dist"}


@dataclass(frozen=True)
class Check:
    """One quality check: how to run it, and how strictly it counts."""

    id: str
    cmd: tuple[str, ...]
    category: str
    description: str
    install: str = ""
    blocking: bool = True

    def resolve(self, package: str) -> list[str]:
        """Return the command with ``{pkg}`` substituted."""
        return [part.format(pkg=package) for part in self.cmd]

    @property
    def binary(self) -> str:
        return self.cmd[0]


CHECKS: tuple[Check, ...] = (
    Check(
        id="lint",
        cmd=("ruff", "check", "."),
        category="lint",
        description="Style and correctness lint.",
        install="ruff",
    ),
    Check(
        id="typecheck",
        cmd=("mypy", "{pkg}"),
        category="types",
        description="Static type checking.",
        install="mypy",
    ),
    Check(
        id="test",
        cmd=("pytest", "-q"),
        category="test",
        description="Unit test suite.",
        install="pytest",
    ),
    Check(
        id="build",
        cmd=("python", "-m", "build"),
        category="build",
        description="Full distribution build (sdist + wheel).",
        install="build",
    ),
    Check(
        # --skip-editable: the project under audit is installed from source and
        # has no PyPI record, which would otherwise fail every run. --strict is
        # incompatible with it, since a skipped distribution is itself an error.
        id="deps",
        cmd=("pip-audit", "--skip-editable", "--progress-spinner", "off"),
        category="security",
        description="Known CVEs in the dependency tree.",
        install="pip-audit",
    ),
    Check(
        # Gate on medium+ severity: every LOW finding here is bandit noticing the
        # subprocess calls this package exists to make (git, gh, check commands).
        id="sast",
        cmd=("bandit", "-q", "-r", "{pkg}", "--severity-level", "medium"),
        category="security",
        description="Static application security testing.",
        install="bandit",
    ),
    Check(
        # Advisory on purpose: blast radius is a heuristic with a hardcoded
        # HIGH_FANIN, and a noisy blocking gate only teaches people to bypass it.
        id="graph",
        cmd=("codebase-memory-gate",),
        category="graph",
        description="Structural blast-radius gate from the code graph.",
        install="codebase-memory-mcp",
        blocking=False,
    ),
)

CHECKS_BY_ID = {c.id: c for c in CHECKS}

# The four categories the governance policy requires a repo to cover.
REQUIRED_CATEGORIES = ("lint", "types", "test", "build")


def blocking_checks() -> tuple[Check, ...]:
    return tuple(c for c in CHECKS if c.blocking)


def select(ids: list[str] | None) -> tuple[list[Check], list[str]]:
    """Resolve check ids to Checks. Returns (checks, unknown_ids)."""
    if not ids:
        return list(CHECKS), []
    known = [CHECKS_BY_ID[i] for i in ids if i in CHECKS_BY_ID]
    unknown = [i for i in ids if i not in CHECKS_BY_ID]
    return known, unknown


def resolve_package(root: str | Path) -> str:
    """Best-guess importable package directory for ``root``.

    Returns the first top-level directory containing ``__init__.py`` that is not
    a test or docs directory, else ``"."`` so commands still target something
    valid in a flat or non-package repository.
    """
    root = Path(root)
    for entry in sorted(root.iterdir()) if root.is_dir() else []:
        if not entry.is_dir() or entry.name.startswith((".", "_")):
            continue
        if entry.name in _NOT_PACKAGES:
            continue
        if (entry / "__init__.py").exists():
            return entry.name
    return "."


def find_binary(name: str) -> str | None:
    """Locate a check's command, preferring the interpreter's own environment.

    ``shutil.which`` alone is not enough: the server usually runs from a
    virtualenv whose ``bin`` directory is not on ``PATH``, so every tool would
    look uninstalled even though it sits right next to ``sys.executable``.
    """
    if name == "python":
        return sys.executable
    candidate = Path(sys.executable).parent / name
    if candidate.is_file() and os.access(candidate, os.X_OK):
        return str(candidate)
    return shutil.which(name)


def missing_binaries(checks: list[Check]) -> list[str]:
    """Ids of checks whose command is not installed in this environment."""
    return [c.id for c in checks if find_binary(c.binary) is None]
