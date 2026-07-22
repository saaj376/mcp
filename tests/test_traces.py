"""Phase 4 verification: static HTTP inference, trace reconciliation, snapshot."""

from pathlib import Path

from codebase_memory.graph import ENDPOINT, HTTP_CALLS, CodeGraph, default_db_path
from codebase_memory.indexer import index_project
from codebase_memory.traces import (
    ingest_traces,
    load_snapshot,
    normalize_endpoint,
    save_snapshot,
)

# app.py makes one literal-URL HTTP call and one non-literal (unresolvable) one.
APP = '''\
import requests


def fetch_user():
    return requests.get("http://users.svc/v1/user?id=1")


def fetch_dynamic(name):
    return requests.get(f"http://users.svc/v1/{name}")
'''


def _build(tmp_path: Path) -> CodeGraph:
    (tmp_path / "app.py").write_text(APP)
    graph, _ = index_project(tmp_path)
    return graph


def test_normalize_endpoint_drops_query_and_lowercases_host():
    assert normalize_endpoint("get", "http://Users.SVC/v1/user?id=1") == "GET http://users.svc/v1/user"
    assert normalize_endpoint("POST", "/local/path?x=1") == "POST /local/path"


def test_static_http_inference(tmp_path):
    graph = _build(tmp_path)
    endpoint = "GET http://users.svc/v1/user"
    assert graph.g.nodes[endpoint]["kind"] == ENDPOINT
    attrs = graph.edge_attrs("app.fetch_user", endpoint, HTTP_CALLS)
    assert attrs["source"] == "static" and attrs["confirmed"] is False
    # the f-string URL is not a literal, so no static edge exists for it
    assert not any(dst == "GET http://users.svc/v1/{name}" for _, dst in graph.edges_of_type(HTTP_CALLS))


def test_ingest_confirms_static_and_adds_runtime(tmp_path):
    graph = _build(tmp_path)
    report = ingest_traces(
        graph,
        [
            # confirms the statically-inferred edge
            {"caller": "app.fetch_user", "method": "GET", "url": "http://users.svc/v1/user", "count": 3},
            # a route static analysis missed (the dynamic one) -> runtime_only correction
            {"caller": "app.fetch_dynamic", "method": "GET", "url": "http://users.svc/v1/bob", "count": 2},
        ],
    )
    assert [c["caller"] for c in report["confirmed"]] == ["app.fetch_user"]
    assert report["confirmed"][0]["count"] == 3
    assert {r["caller"] for r in report["runtime_only"]} == {"app.fetch_dynamic"}

    confirmed = graph.edge_attrs("app.fetch_user", "GET http://users.svc/v1/user", HTTP_CALLS)
    assert confirmed["status"] == "confirmed" and confirmed["confirmed"] is True


def test_ingest_flags_unconfirmed_static(tmp_path):
    graph = _build(tmp_path)
    # traffic never touches the inferred endpoint
    report = ingest_traces(graph, [{"caller": "app.fetch_dynamic", "method": "GET", "url": "http://other.svc/x"}])
    flagged = {u["endpoint"] for u in report["unconfirmed_static"]}
    assert "GET http://users.svc/v1/user" in flagged
    attrs = graph.edge_attrs("app.fetch_user", "GET http://users.svc/v1/user", HTTP_CALLS)
    assert attrs["status"] == "unconfirmed"


def test_ingest_unknown_caller_recorded(tmp_path):
    graph = _build(tmp_path)
    report = ingest_traces(graph, [{"caller": "gateway", "method": "GET", "url": "http://x/y"}])
    assert report["unknown_callers"] == ["gateway"]
    assert graph.g.nodes["gateway"]["kind"] == "external"


def test_snapshot_roundtrip(tmp_path):
    graph = _build(tmp_path)
    ingest_traces(graph, [{"caller": "app.fetch_user", "method": "GET", "url": "http://users.svc/v1/user"}])
    db = default_db_path(tmp_path)
    graph.save(db)

    snap = save_snapshot(db)
    assert snap.exists() and snap.suffix == ".zst"
    db.unlink()  # ensure load rebuilds from the snapshot

    restored = load_snapshot(snap, db)
    assert restored.stats() == graph.stats()
    assert restored.edge_attrs("app.fetch_user", "GET http://users.svc/v1/user", HTTP_CALLS)["confirmed"] is True
