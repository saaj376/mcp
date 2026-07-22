"""Phase 4: close the loop with runtime traffic and a committable snapshot.

``ingest_traces`` reconciles observed runtime HTTP calls against the HTTP_CALLS
edges the static indexer inferred:

- **confirmed**        — a static edge that runtime traffic also exercised.
- **runtime_only**     — traffic hit an edge static analysis missed (a
  correction: dynamic dispatch, config-driven routing, non-literal URLs).
- **unconfirmed_static** — a static edge never seen in traffic (suspected
  mis-inference — the graph silently drifting from reality).

Snapshots compress the SQLite graph to ``.codebase-memory/graph.db.zst`` so it
can be committed alongside the repo and shared by every agent session.
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlsplit

from codebase_memory.graph import ENDPOINT, HTTP_CALLS, CodeGraph

HTTP_METHODS = {"get", "post", "put", "patch", "delete", "head", "options", "request"}


def normalize_endpoint(method: str, url: str) -> str:
    """Canonical endpoint id: ``"<METHOD> <scheme>://<host><path>"``.

    Query strings and fragments are dropped and the host is lower-cased so that
    a static literal and a runtime observation of the same route collapse to one
    node. Relative URLs keep just their path.
    """
    parts = urlsplit(url)
    if parts.scheme:
        path = parts.path or "/"
        endpoint = f"{parts.scheme}://{parts.netloc.lower()}{path}"
    else:
        endpoint = parts.path or url
    return f"{method.upper()} {endpoint}"


def ingest_traces(graph: CodeGraph, traces: list[dict]) -> dict:
    """Reconcile runtime HTTP traces against static HTTP_CALLS edges.

    Each trace: ``{"caller": <symbol id>, "method": "GET", "url": "...",
    "count": <int, optional>}``. Mutates ``graph`` in place and returns a
    reconciliation report.
    """
    confirmed: list[dict] = []
    runtime_only: list[dict] = []
    unknown_callers: set[str] = set()
    seen_static: set[tuple[str, str]] = set()

    for trace in traces:
        caller = trace["caller"]
        endpoint = normalize_endpoint(trace.get("method", "GET"), trace["url"])
        count = int(trace.get("count", 1))

        graph.add_symbol(endpoint, ENDPOINT, name=endpoint)
        if not graph.g.has_node(caller):
            graph.add_symbol(caller, "external", name=caller)
            unknown_callers.add(caller)

        existing = graph.edge_attrs(caller, endpoint, HTTP_CALLS)
        if existing and existing.get("source") == "static":
            total = existing.get("count", 0) + count
            graph.add_typed_edge(
                caller, endpoint, HTTP_CALLS,
                source="static", confirmed=True, status="confirmed", count=total,
            )
            seen_static.add((caller, endpoint))
            confirmed.append({"caller": caller, "endpoint": endpoint, "count": total})
        elif existing:  # already a runtime edge — accumulate
            graph.add_typed_edge(
                caller, endpoint, HTTP_CALLS,
                source="runtime", confirmed=True, status="runtime_only",
                count=existing.get("count", 0) + count,
            )
        else:
            graph.add_typed_edge(
                caller, endpoint, HTTP_CALLS,
                source="runtime", confirmed=True, status="runtime_only", count=count,
            )
            runtime_only.append({"caller": caller, "endpoint": endpoint, "count": count})

    # Static edges never exercised by traffic: flag as suspected mis-inference.
    unconfirmed_static: list[dict] = []
    for src, dst in graph.edges_of_type(HTTP_CALLS):
        attrs = graph.edge_attrs(src, dst, HTTP_CALLS) or {}
        if attrs.get("source") == "static" and (src, dst) not in seen_static and not attrs.get("confirmed"):
            graph.add_typed_edge(src, dst, HTTP_CALLS, source="static", confirmed=False, status="unconfirmed", count=0)
            unconfirmed_static.append({"caller": src, "endpoint": dst})

    return {
        "ingested": len(traces),
        "confirmed": confirmed,
        "runtime_only": runtime_only,
        "unconfirmed_static": unconfirmed_static,
        "unknown_callers": sorted(unknown_callers),
    }


# --------------------------------------------------------------------------- #
# Compressed snapshot (.zst)
# --------------------------------------------------------------------------- #
def _zstd():
    try:
        import zstandard
    except ImportError as exc:  # pragma: no cover - dependency guard
        raise RuntimeError("The 'zstandard' package is required for snapshots.") from exc
    return zstandard


def save_snapshot(db_path: str | Path, snapshot_path: str | Path | None = None) -> Path:
    """Compress the graph database to a ``.zst`` snapshot; returns its path."""
    db_path = Path(db_path)
    if not db_path.exists():
        raise FileNotFoundError(f"No graph database at {db_path}")
    snapshot_path = Path(snapshot_path) if snapshot_path else db_path.with_suffix(".db.zst")
    snapshot_path.parent.mkdir(parents=True, exist_ok=True)
    compressed = _zstd().ZstdCompressor().compress(db_path.read_bytes())
    snapshot_path.write_bytes(compressed)
    return snapshot_path


def load_snapshot(snapshot_path: str | Path, db_path: str | Path | None = None) -> CodeGraph:
    """Decompress a ``.zst`` snapshot to ``db_path`` and load it."""
    snapshot_path = Path(snapshot_path)
    if not snapshot_path.exists():
        raise FileNotFoundError(f"No snapshot at {snapshot_path}")
    db_path = Path(db_path) if db_path else snapshot_path.with_name("graph.db")
    db_path.parent.mkdir(parents=True, exist_ok=True)
    data = _zstd().ZstdDecompressor().decompress(snapshot_path.read_bytes())
    db_path.write_bytes(data)
    return CodeGraph.load(db_path)
