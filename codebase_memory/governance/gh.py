"""A thin wrapper around the ``gh`` CLI.

Shelling out to ``gh`` rather than hand-rolling a REST client means this server
never holds a token: it inherits the user's existing login, including
SSO-authorized organizations, and picks up ``GH_TOKEN`` in CI for free.

Every function returns a plain dict and never raises, matching the error-as-value
convention the rest of the MCP surface uses.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

TIMEOUT = 30
_SCOPES = re.compile(r"Token scopes:\s*(.+)")


def available() -> bool:
    """True when the ``gh`` binary is on PATH."""
    return shutil.which("gh") is not None


def _run(args: list[str], cwd: str | Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["gh", *args],
        capture_output=True,
        text=True,
        timeout=TIMEOUT,
        cwd=str(cwd) if cwd else None,
    )


def auth_status() -> dict:
    """Report whether ``gh`` is installed and logged in, plus token scopes."""
    if not available():
        return {"available": False, "authenticated": False, "reason": "gh CLI not on PATH"}
    try:
        result = _run(["auth", "status"])
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"available": True, "authenticated": False, "reason": str(exc)}
    text = result.stdout + result.stderr
    scopes_match = _SCOPES.search(text)
    scopes = (
        [s.strip().strip("'\"") for s in scopes_match.group(1).split(",")]
        if scopes_match
        else []
    )
    return {
        "available": True,
        "authenticated": result.returncode == 0,
        "scopes": scopes,
        "reason": None if result.returncode == 0 else text.strip()[:400],
    }


def has_scope(scope: str, status: dict | None = None) -> bool:
    """Whether the active token carries ``scope``.

    ``gh`` does not report scopes for every credential type (a fine-grained or
    Actions token prints none), so an empty scope list is treated as unknown
    rather than absent — let the API return the authoritative 403.
    """
    status = status or auth_status()
    scopes = status.get("scopes") or []
    if not scopes:
        return bool(status.get("authenticated"))
    return scope in scopes


def api(
    path: str,
    method: str = "GET",
    body: dict | None = None,
    cwd: str | Path | None = None,
) -> dict:
    """Call the GitHub API. Returns ``{ok, data|error, endpoint, method}``."""
    if not available():
        return {
            "ok": False,
            "error": "gh CLI not on PATH; install it or run with dry_run=True",
            "endpoint": path,
            "method": method,
        }
    args = ["api", path, "--method", method, "-H", "Accept: application/vnd.github+json"]
    stdin = None
    if body is not None:
        args += ["--input", "-"]
        stdin = json.dumps(body)
    try:
        result = subprocess.run(
            ["gh", *args],
            input=stdin,
            capture_output=True,
            text=True,
            timeout=TIMEOUT,
            cwd=str(cwd) if cwd else None,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"ok": False, "error": str(exc), "endpoint": path, "method": method}

    out = result.stdout.strip()
    parsed: object | None = None
    if out:
        try:
            parsed = json.loads(out)
        except json.JSONDecodeError:
            parsed = out

    if result.returncode != 0:
        message = result.stderr.strip() or out
        if isinstance(parsed, dict) and parsed.get("message"):
            message = str(parsed["message"])
        return {"ok": False, "error": message[:600], "endpoint": path, "method": method}
    return {"ok": True, "data": parsed, "endpoint": path, "method": method}


def preview(path: str, method: str, body: dict | None) -> dict:
    """The exact call a mutating tool *would* make, for ``dry_run=True``."""
    return {
        "dry_run": True,
        "would_call": {"method": method, "endpoint": path, "body": body},
        "hint": "Re-run with dry_run=False to apply.",
    }


def current_repo(root: str | Path | None = None) -> str | None:
    """``owner/name`` for the repository at ``root``, or None if undeterminable."""
    if not available():
        return None
    try:
        result = _run(["repo", "view", "--json", "nameWithOwner"], cwd=root)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    try:
        return json.loads(result.stdout).get("nameWithOwner")
    except (json.JSONDecodeError, AttributeError):
        return None


def repo_id(repo: str) -> int | None:
    """Numeric database id of ``owner/name`` — required by the workflows rule."""
    result = api(f"repos/{repo}")
    if result["ok"] and isinstance(result.get("data"), dict):
        value = result["data"].get("id")
        return int(value) if isinstance(value, int) else None
    return None
