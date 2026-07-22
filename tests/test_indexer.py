"""Phase 1 verification: the indexer produces a correct structural graph."""

from pathlib import Path

from codebase_memory.graph import CALLS, CLASS, CONTAINS, FUNCTION, IMPORTS, MODULE, CodeGraph
from codebase_memory.indexer import index_project

SAMPLE = '''\
import os

from sample.helpers import shout


def greet(name):
    return shout(name)


class Greeter:
    def __init__(self, name):
        self.name = name

    def hello(self):
        return greet(self.name)

    def loud(self):
        return self.hello()
'''

HELPERS = '''\
def shout(text):
    return text.upper()
'''


def _make_project(tmp_path: Path) -> Path:
    pkg = tmp_path / "sample"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "app.py").write_text(SAMPLE)
    (pkg / "helpers.py").write_text(HELPERS)
    return tmp_path


def test_symbols_collected(tmp_path):
    graph, report = index_project(_make_project(tmp_path))
    assert report.parse_errors == []

    assert graph.g.nodes["sample.app"]["kind"] == MODULE
    assert graph.g.nodes["sample.app.greet"]["kind"] == FUNCTION
    assert graph.g.nodes["sample.app.Greeter"]["kind"] == CLASS
    assert graph.g.nodes["sample.app.Greeter.hello"]["kind"] == FUNCTION


def test_contains_edges(tmp_path):
    graph, _ = index_project(_make_project(tmp_path))
    contains = set(graph.edges_of_type(CONTAINS))
    assert ("sample.app", "sample.app.greet") in contains
    assert ("sample.app", "sample.app.Greeter") in contains
    assert ("sample.app.Greeter", "sample.app.Greeter.hello") in contains


def test_import_edges(tmp_path):
    graph, _ = index_project(_make_project(tmp_path))
    imports = set(graph.edges_of_type(IMPORTS))
    # from sample.helpers import shout  -> edge to the source module
    assert ("sample.app", "sample.helpers.shout") in imports
    assert ("sample.app", "os") in imports


def test_calls_resolved(tmp_path):
    graph, _ = index_project(_make_project(tmp_path))
    calls = set(graph.edges_of_type(CALLS))
    # greet() calls the imported shout()
    assert ("sample.app.greet", "sample.helpers.shout") in calls
    # Greeter.hello() calls module-level greet()
    assert ("sample.app.Greeter.hello", "sample.app.greet") in calls
    # Greeter.loud() calls self.hello()
    assert ("sample.app.Greeter.loud", "sample.app.Greeter.hello") in calls


def test_unique_name_method_fallback(tmp_path):
    """var.method() binds by unique short name, marked lower-confidence."""
    src = (
        "class Repo:\n"
        "    def persist(self):\n"
        "        return 1\n"
        "\n\n"
        "def run():\n"
        "    r = Repo()\n"
        "    return r.persist()\n"
    )
    (tmp_path / "m.py").write_text(src)
    graph, _ = index_project(tmp_path)
    assert ("m.run", "m.Repo.persist") in set(graph.edges_of_type(CALLS))
    # the fallback edge is flagged resolved=False (heuristic, not confident)
    edge = graph.g.get_edge_data("m.run", "m.Repo.persist", key=CALLS)
    assert edge["resolved"] is False


def test_persistence_roundtrip(tmp_path):
    graph, _ = index_project(_make_project(tmp_path))
    db = tmp_path / "graph.db"
    graph.save(db)
    reloaded = CodeGraph.load(db)
    assert reloaded.stats() == graph.stats()
    assert set(reloaded.edges_of_type(CALLS)) == set(graph.edges_of_type(CALLS))
