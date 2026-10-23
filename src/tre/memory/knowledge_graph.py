"""The research knowledge graph.

A typed graph over the study's artifacts — papers, concepts, hypotheses,
experiments, results, tickets, ADRs, reports — persisted in SQLite inside the
docs folder, so lineage questions ("what evidence bears on HYP-002?") are one
query, and every workflow leaves a machine-readable trail.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

NODE_TYPES = {
    "paper", "concept", "hypothesis", "experiment", "result", "dataset",
    "model", "ticket", "adr", "report", "artifact", "stub",
}

EDGE_TYPES = {
    "cites", "about", "tests", "supports", "refutes", "inconclusive",
    "depends_on", "produced", "uses", "derived_from", "implements",
    "blocks", "informs", "supersedes",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class KnowledgeGraph:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS nodes (
                id TEXT PRIMARY KEY,
                type TEXT NOT NULL,
                title TEXT NOT NULL,
                data TEXT NOT NULL DEFAULT '{}',
                created TEXT NOT NULL,
                updated TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS edges (
                src TEXT NOT NULL,
                dst TEXT NOT NULL,
                type TEXT NOT NULL,
                data TEXT NOT NULL DEFAULT '{}',
                created TEXT NOT NULL,
                PRIMARY KEY (src, dst, type)
            );
            CREATE INDEX IF NOT EXISTS idx_nodes_type ON nodes(type);
            CREATE INDEX IF NOT EXISTS idx_edges_src ON edges(src);
            CREATE INDEX IF NOT EXISTS idx_edges_dst ON edges(dst);
            """
        )
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    # -- writes ------------------------------------------------------------

    def add_node(self, node_id: str, type_: str, title: str, **data: Any) -> dict:
        if type_ not in NODE_TYPES:
            raise ValueError(f"unknown node type {type_!r}; expected one of {sorted(NODE_TYPES)}")
        now = _now()
        existing = self.get(node_id)
        if existing:
            merged = {**existing["data"], **data}
            self._conn.execute(
                "UPDATE nodes SET type=?, title=?, data=?, updated=? WHERE id=?",
                (type_, title, json.dumps(merged), now, node_id),
            )
        else:
            self._conn.execute(
                "INSERT INTO nodes (id, type, title, data, created, updated) VALUES (?,?,?,?,?,?)",
                (node_id, type_, title, json.dumps(data), now, now),
            )
        self._conn.commit()
        return self.get(node_id)  # type: ignore[return-value]

    def add_edge(self, src: str, dst: str, type_: str, **data: Any) -> dict:
        if type_ not in EDGE_TYPES:
            raise ValueError(f"unknown edge type {type_!r}; expected one of {sorted(EDGE_TYPES)}")
        for endpoint in (src, dst):
            if not self.get(endpoint):
                # forgiving: agents may link before describing; stubs are visible in stats
                self.add_node(endpoint, "stub", endpoint)
        self._conn.execute(
            "INSERT OR REPLACE INTO edges (src, dst, type, data, created) VALUES (?,?,?,?,?)",
            (src, dst, type_, json.dumps(data), _now()),
        )
        self._conn.commit()
        return {"src": src, "dst": dst, "type": type_, "data": data}

    # -- reads -------------------------------------------------------------

    def get(self, node_id: str) -> Optional[dict]:
        row = self._conn.execute("SELECT * FROM nodes WHERE id=?", (node_id,)).fetchone()
        return self._node(row) if row else None

    def nodes(self, type_: Optional[str] = None, q: Optional[str] = None) -> list[dict]:
        sql, args = "SELECT * FROM nodes", []
        clauses = []
        if type_:
            clauses.append("type=?")
            args.append(type_)
        if q:
            clauses.append("(title LIKE ? OR id LIKE ? OR data LIKE ?)")
            args += [f"%{q}%"] * 3
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY id"
        return [self._node(r) for r in self._conn.execute(sql, args)]

    def edges(self, src: Optional[str] = None, dst: Optional[str] = None,
              type_: Optional[str] = None) -> list[dict]:
        sql, args, clauses = "SELECT * FROM edges", [], []
        for col, val in (("src", src), ("dst", dst), ("type", type_)):
            if val:
                clauses.append(f"{col}=?")
                args.append(val)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        return [self._edge(r) for r in self._conn.execute(sql, args)]

    def neighbors(self, node_id: str, direction: str = "both") -> list[dict]:
        out: list[dict] = []
        if direction in ("out", "both"):
            out += self.edges(src=node_id)
        if direction in ("in", "both"):
            out += self.edges(dst=node_id)
        return out

    def lineage(self, node_id: str, depth: int = 3) -> dict:
        """BFS in both directions: the evidence neighborhood of a node."""
        seen_nodes: dict[str, dict] = {}
        seen_edges: list[dict] = []
        seen_pairs: set[tuple[str, str, str]] = set()
        frontier = [node_id]
        for _ in range(depth):
            nxt: list[str] = []
            for nid in frontier:
                node = self.get(nid)
                if node and nid not in seen_nodes:
                    seen_nodes[nid] = node
                for edge in self.neighbors(nid):
                    key = (edge["src"], edge["dst"], edge["type"])
                    if key in seen_pairs:
                        continue
                    seen_pairs.add(key)
                    seen_edges.append(edge)
                    for other in (edge["src"], edge["dst"]):
                        if other not in seen_nodes:
                            nxt.append(other)
            frontier = nxt
            if not frontier:
                break
        for nid in frontier:
            node = self.get(nid)
            if node and nid not in seen_nodes:
                seen_nodes[nid] = node
        return {"nodes": list(seen_nodes.values()), "edges": seen_edges}

    def stats(self) -> dict:
        by_type = {r["type"]: r["n"] for r in self._conn.execute(
            "SELECT type, COUNT(*) n FROM nodes GROUP BY type")}
        n_edges = self._conn.execute("SELECT COUNT(*) n FROM edges").fetchone()["n"]
        return {"nodes": sum(by_type.values()), "edges": n_edges, "by_type": by_type}

    # -- export ------------------------------------------------------------

    def to_mermaid(self, subgraph: Optional[dict] = None) -> str:
        data = subgraph or {"nodes": self.nodes(), "edges": self.edges()}
        classes = {
            "paper": "paper", "hypothesis": "hyp", "experiment": "exp",
            "result": "res", "ticket": "tck", "adr": "adr", "report": "rep",
        }
        def nid(s: str) -> str:
            return "n_" + "".join(c if c.isalnum() else "_" for c in s)
        lines = ["graph LR"]
        for node in data["nodes"]:
            label = node["title"][:48].replace('"', "'")
            cls = classes.get(node["type"], "misc")
            lines.append(f'    {nid(node["id"])}["{node["id"]}<br/>{label}"]:::{cls}')
        for edge in data["edges"]:
            lines.append(f'    {nid(edge["src"])} -->|{edge["type"]}| {nid(edge["dst"])}')
        lines += [
            "    classDef paper fill:#e0f2fe,stroke:#0284c7,color:#0c4a6e",
            "    classDef hyp fill:#fef9c3,stroke:#ca8a04,color:#713f12",
            "    classDef exp fill:#dcfce7,stroke:#16a34a,color:#14532d",
            "    classDef res fill:#fce7f3,stroke:#db2777,color:#831843",
            "    classDef tck fill:#f1f5f9,stroke:#64748b,color:#0f172a",
            "    classDef adr fill:#ede9fe,stroke:#7c3aed,color:#4c1d95",
            "    classDef rep fill:#ffedd5,stroke:#ea580c,color:#7c2d12",
            "    classDef misc fill:#f8fafc,stroke:#94a3b8,color:#334155",
        ]
        return "\n".join(lines)

    def export_json(self, path: Path) -> None:
        payload = {"nodes": self.nodes(), "edges": self.edges(), "stats": self.stats()}
        Path(path).write_text(json.dumps(payload, indent=2))

    # -- rows --------------------------------------------------------------

    @staticmethod
    def _node(row: sqlite3.Row) -> dict:
        return {"id": row["id"], "type": row["type"], "title": row["title"],
                "data": json.loads(row["data"]), "created": row["created"],
                "updated": row["updated"]}

    @staticmethod
    def _edge(row: sqlite3.Row) -> dict:
        return {"src": row["src"], "dst": row["dst"], "type": row["type"],
                "data": json.loads(row["data"]), "created": row["created"]}
