"""Phase 3 verification: diff parsing and detect_changes blast radius/risk."""

import subprocess
from pathlib import Path

from codebase_memory.changes import detect_changes, parse_unified_diff
from codebase_memory.indexer import index_project


# --------------------------------------------------------------------------- #
# Pure diff parsing
# --------------------------------------------------------------------------- #
def test_parse_modification():
    diff = (
        "diff --git a/app.py b/app.py\n"
        "--- a/app.py\n"
        "+++ b/app.py\n"
        "@@ -2 +2 @@ def core():\n"
        "-    return 1\n"
        "+    return 2\n"
    )
    p = parse_unified_diff(diff)
    assert p["changed_lines"] == {"app.py": {2}}
    assert p["added_files"] == set()
    assert p["deleted_files"] == set()


def test_parse_added_and_deleted():
    diff = (
        "--- /dev/null\n"
        "+++ b/new.py\n"
        "@@ -0,0 +1,2 @@\n"
        "+def x():\n"
        "+    pass\n"
        "--- a/old.py\n"
        "+++ /dev/null\n"
        "@@ -1,2 +0,0 @@\n"
        "-def y():\n"
        "-    pass\n"
    )
    p = parse_unified_diff(diff)
    assert p["added_files"] == {"new.py"}
    assert p["deleted_files"] == {"old.py"}
    assert p["changed_lines"]["new.py"] == {1, 2}


# --------------------------------------------------------------------------- #
# detect_changes over a real git repo
# --------------------------------------------------------------------------- #
def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
    )


def _init_repo(root: Path, files: dict[str, str]) -> None:
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@t.test")
    _git(root, "config", "user.name", "t")
    for name, content in files.items():
        (root / name).write_text(content)
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "init")


# core() is called by five functions -> high fan-in.
APP = '''\
def core():
    return 1


def a():
    return core()


def b():
    return core()


def c():
    return core()


def d():
    return core()


def e():
    return core()


def orphan():
    return 0
'''


def test_high_risk_change_blocks_without_tests(tmp_path):
    _init_repo(tmp_path, {"app.py": APP})
    # Modify core()'s body (line 2) without touching tests.
    (tmp_path / "app.py").write_text(APP.replace("return 1", "return 99"))
    graph, _ = index_project(tmp_path)

    result = detect_changes(graph, tmp_path)

    affected = {s["id"]: s for s in result["affected_symbols"]}
    assert "app.core" in affected
    assert affected["app.core"]["fan_in"] == 5
    assert result["risk"] == "high"
    assert result["tests_changed"] is False
    assert result["gate_should_block"] is True
    # blast radius = the five callers, excluding core itself
    assert set(result["blast_radius"]) == {"app.a", "app.b", "app.c", "app.d", "app.e"}


def test_low_risk_when_no_callers(tmp_path):
    _init_repo(tmp_path, {"app.py": APP})
    (tmp_path / "app.py").write_text(APP.replace("return 0", "return 123"))
    graph, _ = index_project(tmp_path)

    result = detect_changes(graph, tmp_path)

    assert {s["id"] for s in result["affected_symbols"]} == {"app.orphan"}
    assert result["risk"] == "low"
    assert result["gate_should_block"] is False


def test_test_change_prevents_block(tmp_path):
    _init_repo(tmp_path, {"app.py": APP, "test_app.py": "def test_core():\n    pass\n"})
    (tmp_path / "app.py").write_text(APP.replace("return 1", "return 99"))
    (tmp_path / "test_app.py").write_text("def test_core():\n    return 1\n")
    graph, _ = index_project(tmp_path)

    result = detect_changes(graph, tmp_path)

    assert result["risk"] == "high"
    assert result["tests_changed"] is True
    assert result["gate_should_block"] is False
