"""Phase 2 verification: read/traversal queries over the graph."""

from pathlib import Path

from codebase_memory import queries
from codebase_memory.graph import CLASS, FUNCTION
from codebase_memory.indexer import index_project

# app.py:
#   greet() -> shout()               (imported)
#   Greeter.hello() -> greet()
#   Greeter.loud() -> Greeter.hello()
#   orphan() is defined but never called or imported
APP = '''\
from sample.helpers import shout


def greet(name):
    return shout(name)


def orphan():
    return 42


def main():
    return greet("world")


class Greeter:
    def hello(self):
        return greet("x")

    def loud(self):
        return self.hello()
'''

HELPERS = 'def shout(text):\n    return text.upper()\n'


def _project(tmp_path: Path) -> Path:
    pkg = tmp_path / "sample"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "app.py").write_text(APP)
    (pkg / "helpers.py").write_text(HELPERS)
    graph, _ = index_project(tmp_path)
    return graph


def test_callers_and_callees(tmp_path):
    graph = _project(tmp_path)
    callers = {c["id"] for c in queries.callers_of(graph, "sample.app.greet")}
    assert callers == {"sample.app.Greeter.hello", "sample.app.main"}
    callees = {c["id"] for c in queries.callees_of(graph, "sample.app.Greeter.loud")}
    assert callees == {"sample.app.Greeter.hello"}


def test_dead_code(tmp_path):
    graph = _project(tmp_path)
    result = queries.find_dead_code(graph)
    dead_ids = {d["id"] for d in result["dead_code"]}
    excluded_ids = {d["id"] for d in result["excluded_as_entrypoints"]}
    # orphan() is genuinely dead
    assert "sample.app.orphan" in dead_ids
    # main() has no callers but is a recognized entrypoint, not flagged as dead
    assert "sample.app.main" in excluded_ids
    assert "sample.app.main" not in dead_ids
    # called functions are never dead
    assert "sample.app.greet" not in dead_ids


def test_by_name_and_kind(tmp_path):
    graph = _project(tmp_path)
    names = {s["id"] for s in queries.symbols_by_name(graph, "greet")}
    assert names == {"sample.app.greet", "sample.app.Greeter"}
    classes = {s["id"] for s in queries.symbols_by_kind(graph, CLASS)}
    assert classes == {"sample.app.Greeter"}


def test_trace_path(tmp_path):
    graph = _project(tmp_path)
    result = queries.trace_path(graph, "sample.app.Greeter.loud", "sample.helpers.shout")
    assert result["found"] is True
    path_ids = [n["id"] for n in result["path"]]
    assert path_ids == [
        "sample.app.Greeter.loud",
        "sample.app.Greeter.hello",
        "sample.app.greet",
        "sample.helpers.shout",
    ]
    assert result["hops"] == 3


def test_trace_path_none(tmp_path):
    graph = _project(tmp_path)
    result = queries.trace_path(graph, "sample.helpers.shout", "sample.app.greet")
    assert result["found"] is False
    assert result["path"] is None


def test_get_architecture(tmp_path):
    graph = _project(tmp_path)
    arch = queries.get_architecture(graph)
    mods = {m["id"]: m for m in arch["modules"]}
    assert "sample.app" in mods
    assert mods["sample.app"]["classes"] == ["Greeter"]
    assert set(mods["sample.app"]["functions"]) == {"greet", "orphan", "main"}
    # app imports helpers -> internal module dependency
    assert ["sample.app", "sample.helpers"] in arch["module_dependencies"]
