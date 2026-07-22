"""The code knowledge graph: an in-memory MultiDiGraph backed by SQLite.

Nodes represent code symbols (modules, classes, functions). Edges represent
structural relationships between them (CONTAINS, IMPORTS, CALLS). Later phases
add runtime-derived edges (HTTP_CALLS) on top of the same store.

Persistence is a single SQLite file under ``.codebase-memory/graph.db`` so the
graph can be committed alongside the repo and shared by every agent session,
per the design doc's "shared structural graph" property.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Iterable, Iterator

import networkx as nx

DEFAULT_DIR = ".codebase-memory"
DB_NAME = "graph.db"

# Node kinds
MODULE = "module"
CLASS = "class"
FUNCTION = "function"

# Edge types
CONTAINS = "CONTAINS"
IMPORTS = "IMPORTS"
CALLS = "CALLS"


def default_db_path(root: str | Path) -> Path:
    """Location of the graph database for a project rooted at ``root``."""
    return Path(root) / DEFAULT_DIR / DB_NAME


class CodeGraph:
    """A structural graph of a codebase.

    Wraps a ``networkx.MultiDiGraph`` (for traversal) with SQLite persistence.
    Edges are keyed by their ``type`` so that, e.g., a CONTAINS and a CALLS edge
    between the same pair of symbols coexist without clobbering each other.
    """

    def __init__(self) -> None:
        self.g: nx.MultiDiGraph = nx.MultiDiGraph()

    # -- construction -----------------------------------------------------
    def add_symbol(
        self,
        node_id: str,
        kind: str,
        *,
        name: str = "",
        file: str = "",
        line: int = 0,
        end_line: int = 0,
    ) -> None:
        """Add or update a symbol node. Idempotent on ``node_id``.

        ``line``/``end_line`` are the 1-based inclusive source span, used to map
        a changed diff line to its smallest enclosing symbol.
        """
        self.g.add_node(
            node_id, kind=kind, name=name, file=file, line=line, end_line=end_line
        )

    def add_relation(
        self,
        src: str,
        dst: str,
        edge_type: str,
        *,
        resolved: bool = True,
    ) -> None:
        """Add a typed edge between two symbols.

        ``resolved`` marks whether ``dst`` was matched to a known symbol in the
        graph (True) or is a best-effort name we could not bind (False).
        Using ``edge_type`` as the multigraph key dedupes repeated relations.
        """
        self.g.add_edge(src, dst, key=edge_type, type=edge_type, resolved=resolved)

    # -- introspection ----------------------------------------------------
    @property
    def num_nodes(self) -> int:
        return self.g.number_of_nodes()

    @property
    def num_edges(self) -> int:
        return self.g.number_of_edges()

    def nodes_of_kind(self, kind: str) -> Iterator[str]:
        for node_id, attrs in self.g.nodes(data=True):
            if attrs.get("kind") == kind:
                yield node_id

    def edges_of_type(self, edge_type: str) -> Iterator[tuple[str, str]]:
        for src, dst, etype in self.g.edges(keys=True):
            if etype == edge_type:
                yield src, dst

    def stats(self) -> dict[str, int]:
        """Node/edge counts broken down by kind and type."""
        out: dict[str, int] = {"nodes": self.num_nodes, "edges": self.num_edges}
        for kind in (MODULE, CLASS, FUNCTION):
            out[f"nodes.{kind}"] = sum(1 for _ in self.nodes_of_kind(kind))
        for etype in (CONTAINS, IMPORTS, CALLS):
            out[f"edges.{etype}"] = sum(1 for _ in self.edges_of_type(etype))
        return out

    # -- persistence ------------------------------------------------------
    def save(self, db_path: str | Path) -> None:
        db_path = Path(db_path)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(db_path)
        try:
            conn.executescript(
                """
                DROP TABLE IF EXISTS nodes;
                DROP TABLE IF EXISTS edges;
                CREATE TABLE nodes (
                    id       TEXT PRIMARY KEY,
                    kind     TEXT NOT NULL,
                    name     TEXT,
                    file     TEXT,
                    line     INTEGER,
                    end_line INTEGER
                );
                CREATE TABLE edges (
                    src      TEXT NOT NULL,
                    dst      TEXT NOT NULL,
                    type     TEXT NOT NULL,
                    resolved INTEGER NOT NULL DEFAULT 1
                );
                CREATE INDEX idx_edges_src ON edges(src);
                CREATE INDEX idx_edges_dst ON edges(dst);
                CREATE INDEX idx_edges_type ON edges(type);
                """
            )
            conn.executemany(
                "INSERT INTO nodes (id, kind, name, file, line, end_line) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    (
                        n,
                        a.get("kind", ""),
                        a.get("name", ""),
                        a.get("file", ""),
                        a.get("line", 0),
                        a.get("end_line", 0),
                    )
                    for n, a in self.g.nodes(data=True)
                ),
            )
            conn.executemany(
                "INSERT INTO edges (src, dst, type, resolved) VALUES (?, ?, ?, ?)",
                (
                    (s, d, a.get("type", k), int(a.get("resolved", True)))
                    for s, d, k, a in self.g.edges(keys=True, data=True)
                ),
            )
            conn.commit()
        finally:
            conn.close()

    @classmethod
    def load(cls, db_path: str | Path) -> "CodeGraph":
        db_path = Path(db_path)
        if not db_path.exists():
            raise FileNotFoundError(f"No graph database at {db_path}")
        graph = cls()
        conn = sqlite3.connect(db_path)
        try:
            for node_id, kind, name, file, line, end_line in conn.execute(
                "SELECT id, kind, name, file, line, end_line FROM nodes"
            ):
                graph.add_symbol(
                    node_id,
                    kind,
                    name=name or "",
                    file=file or "",
                    line=line or 0,
                    end_line=end_line or 0,
                )
            for src, dst, etype, resolved in conn.execute(
                "SELECT src, dst, type, resolved FROM edges"
            ):
                graph.add_relation(src, dst, etype, resolved=bool(resolved))
        finally:
            conn.close()
        return graph
