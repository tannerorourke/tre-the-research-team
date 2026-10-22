"""Directed workflow graphs: nodes, deterministic edges, conditional branches,
human gates, splicing, validation, and Mermaid export.

Execution semantics (see runner.py): one node at a time, conditional routing,
bounded loops. Fan-out happens *inside* nodes via ``parallel_map`` — the graph
stays deterministic and resumable while map nodes run their items concurrently.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

from .state import GraphError, GraphState

START = "__start__"
END = "__end__"

NodeFn = Callable[[GraphState, Any], Awaitable[Optional[dict]]]
RouterFn = Callable[[GraphState], str]
AskFn = Callable[[GraphState], str]

KINDS = {"task", "agent", "tool", "gate", "subgraph"}


@dataclass
class Node:
    name: str
    fn: Optional[NodeFn]
    kind: str = "task"
    retries: int = 0
    timeout: Optional[float] = None
    max_visits: int = 8
    doc: str = ""
    # gate-only
    ask: Optional[AskFn] = None
    field_name: str = "human_decision"


@dataclass
class Branch:
    router: RouterFn
    targets: dict[str, str]
    doc: str = ""


@dataclass
class Graph:
    name: str
    state_model: type[GraphState]
    doc: str = ""
    nodes: dict[str, Node] = field(default_factory=dict)
    edges: dict[str, str] = field(default_factory=dict)          # src -> dst
    branches: dict[str, Branch] = field(default_factory=dict)    # src -> Branch

    # -- construction ------------------------------------------------------

    def add(
        self,
        name: str,
        fn: NodeFn,
        *,
        kind: str = "task",
        retries: int = 0,
        timeout: Optional[float] = None,
        max_visits: int = 8,
        doc: str = "",
    ) -> "Graph":
        if kind not in KINDS:
            raise GraphError(f"unknown node kind {kind!r}")
        if name in self.nodes or name in (START, END):
            raise GraphError(f"duplicate node {name!r} in graph {self.name!r}")
        self.nodes[name] = Node(
            name=name, fn=fn, kind=kind, retries=retries,
            timeout=timeout, max_visits=max_visits, doc=doc,
        )
        return self

    def task(self, name: str, fn: NodeFn, **kw: Any) -> "Graph":
        return self.add(name, fn, kind="task", **kw)

    def agent(self, name: str, fn: NodeFn, **kw: Any) -> "Graph":
        return self.add(name, fn, kind="agent", **kw)

    def tool(self, name: str, fn: NodeFn, **kw: Any) -> "Graph":
        return self.add(name, fn, kind="tool", **kw)

    def gate(self, name: str, ask: AskFn, *, field_name: str = "human_decision", doc: str = "") -> "Graph":
        """Human approval gate. Pauses the run (GraphInterrupt) until an answer
        for `name` is provided; the answer lands in state[field_name]."""
        if name in self.nodes:
            raise GraphError(f"duplicate node {name!r}")
        self.nodes[name] = Node(
            name=name, fn=None, kind="gate", ask=ask,
            field_name=field_name, max_visits=99, doc=doc,
        )
        return self

    def edge(self, src: str, dst: str) -> "Graph":
        if src in self.edges or src in self.branches:
            raise GraphError(f"node {src!r} already has an outgoing edge")
        self.edges[src] = dst
        return self

    def chain(self, *names: str) -> "Graph":
        for a, b in zip(names, names[1:]):
            self.edge(a, b)
        return self

    def branch(self, src: str, router: RouterFn, targets: dict[str, str], doc: str = "") -> "Graph":
        if src in self.edges or src in self.branches:
            raise GraphError(f"node {src!r} already has an outgoing edge")
        self.branches[src] = Branch(router=router, targets=dict(targets), doc=doc)
        return self

    # -- composition -------------------------------------------------------

    def splice(self, other: "Graph", prefix: str = "") -> tuple[str, list[str]]:
        """Copy another graph's nodes/edges into this one (optionally
        prefixed). Returns (entry, exits): the copied entry node and the nodes
        that pointed at other's END, for the caller to wire."""
        ren = {n: (f"{prefix}{n}" if prefix else n) for n in other.nodes}
        for old, new in ren.items():
            if new in self.nodes:
                raise GraphError(f"splice collision on node {new!r}")
        for old, node in other.nodes.items():
            copied = Node(**{**node.__dict__, "name": ren[old]})
            self.nodes[ren[old]] = copied
        entry = ""
        exits: list[str] = []
        for src, dst in other.edges.items():
            if src == START:
                entry = ren[dst]
                continue
            if dst == END:
                exits.append(ren[src])
                continue
            self.edges[ren[src]] = ren[dst]
        for src, br in other.branches.items():
            targets = {}
            for label, dst in br.targets.items():
                if dst == END:
                    # caller re-wires END targets; keep a sentinel edge
                    exits.append(ren[src])
                    targets[label] = END
                else:
                    targets[label] = ren[dst]
            self.branches[ren[src]] = Branch(router=br.router, targets=targets, doc=br.doc)
        if not entry:
            raise GraphError(f"graph {other.name!r} has no START edge to splice")
        return entry, exits

    def rewire_end(self, node: str, dst: str) -> "Graph":
        """Point a spliced exit node (or its branch END targets) at `dst`."""
        if node in self.edges:
            self.edges[node] = dst
        elif node in self.branches:
            for label, t in self.branches[node].targets.items():
                if t == END:
                    self.branches[node].targets[label] = dst
        else:
            self.edges[node] = dst
        return self

    # -- validation --------------------------------------------------------

    @property
    def entry(self) -> str:
        e = self.edges.get(START)
        if not e:
            raise GraphError(f"graph {self.name!r} has no START edge")
        return e

    def next_of(self, name: str, state: GraphState) -> str:
        if name in self.edges:
            return self.edges[name]
        if name in self.branches:
            br = self.branches[name]
            label = br.router(state)
            if label not in br.targets:
                raise GraphError(
                    f"router at {name!r} returned {label!r}; "
                    f"expected one of {sorted(br.targets)}"
                )
            return br.targets[label]
        raise GraphError(f"node {name!r} has no outgoing edge")

    def validate(self) -> None:
        entry = self.entry  # raises if missing
        targets: set[str] = set()
        for src, dst in self.edges.items():
            if src != START and src not in self.nodes:
                raise GraphError(f"edge from unknown node {src!r}")
            if dst != END and dst not in self.nodes:
                raise GraphError(f"edge to unknown node {dst!r} from {src!r}")
            targets.add(dst)
        for src, br in self.branches.items():
            if src not in self.nodes:
                raise GraphError(f"branch from unknown node {src!r}")
            if not br.targets:
                raise GraphError(f"branch at {src!r} has no targets")
            for dst in br.targets.values():
                if dst != END and dst not in self.nodes:
                    raise GraphError(f"branch to unknown node {dst!r} from {src!r}")
                targets.add(dst)
        # every node has an out-edge
        for name in self.nodes:
            if name not in self.edges and name not in self.branches:
                raise GraphError(f"node {name!r} is a dead end (no outgoing edge)")
        # reachability from entry
        seen: set[str] = set()
        frontier = [entry]
        while frontier:
            cur = frontier.pop()
            if cur in seen or cur == END:
                continue
            seen.add(cur)
            if cur in self.edges:
                frontier.append(self.edges[cur])
            if cur in self.branches:
                frontier.extend(self.branches[cur].targets.values())
        unreachable = set(self.nodes) - seen
        if unreachable:
            raise GraphError(f"unreachable nodes in {self.name!r}: {sorted(unreachable)}")
        if END not in targets:
            raise GraphError(f"graph {self.name!r} never reaches END")

    # -- rendering ---------------------------------------------------------

    def to_mermaid(self) -> str:
        def nid(n: str) -> str:
            return n.replace("__start__", "S").replace("__end__", "E")

        def shape(node: Node) -> str:
            label = node.name.replace("_", " ")
            if node.kind == "agent":
                return f'{nid(node.name)}(["{label}"]):::agent'
            if node.kind == "tool":
                return f'{nid(node.name)}[["{label}"]]:::tool'
            if node.kind == "gate":
                return f'{nid(node.name)}{{"{label}"}}:::gate'
            if node.kind == "subgraph":
                return f'{nid(node.name)}[/"{label}"/]:::sub'
            return f'{nid(node.name)}["{label}"]:::task'

        lines = ["flowchart TD"]
        lines.append('    S((" ")):::term')
        lines.append('    E(("done")):::term')
        for node in self.nodes.values():
            lines.append(f"    {shape(node)}")
        for src, dst in self.edges.items():
            lines.append(f"    {nid(src)} --> {nid(dst)}")
        for src, br in self.branches.items():
            for label, dst in br.targets.items():
                lines.append(f"    {nid(src)} -->|{label}| {nid(dst)}")
        lines += [
            "    classDef agent fill:#dbeafe,stroke:#2563eb,color:#1e3a8a",
            "    classDef tool fill:#dcfce7,stroke:#16a34a,color:#14532d",
            "    classDef task fill:#f1f5f9,stroke:#64748b,color:#0f172a",
            "    classDef gate fill:#fef3c7,stroke:#d97706,color:#78350f",
            "    classDef sub fill:#ede9fe,stroke:#7c3aed,color:#4c1d95",
            "    classDef term fill:#0f172a,stroke:#0f172a,color:#f8fafc",
        ]
        return "\n".join(lines)
