"""The graph executor.

One node at a time; conditional routing; per-node retries with backoff;
bounded loops (per-node max_visits + global max_steps); a checkpoint after
every step; human gates that pause the run by raising GraphInterrupt and
resume from disk with an answer.
"""

from __future__ import annotations

import asyncio
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from ..config import TreConfig
from .checkpoint import RunStore
from .events import Emitter
from .graph import END, Graph, GraphError, Node
from .state import GraphState


@dataclass
class Context:
    """Everything nodes may need. Injected, never global — tests swap any part."""

    root: Path                       # repo root the lab sits on
    config: TreConfig
    store: RunStore
    emitter: Emitter
    runtime: Any = None              # AgentRuntime (SDK or mock)
    notebook: Any = None             # LabNotebook
    kg: Any = None                   # KnowledgeGraph
    extras: dict = field(default_factory=dict)

    @property
    def docs_dir(self) -> Path:
        return self.root / self.config.paths.docs


class GraphInterrupt(Exception):
    """Raised when a run reaches an unanswered human gate. Not an error:
    the run is checkpointed and resumes with `answers={gate: text}`."""

    def __init__(self, run_id: str, graph: str, gate: str, message: str):
        super().__init__(f"[{run_id}] gate {gate!r}: {message}")
        self.run_id = run_id
        self.graph = graph
        self.gate = gate
        self.message = message


@dataclass
class RunResult:
    run_id: str
    graph: str
    status: str
    state: GraphState
    steps: int
    duration_s: float
    cost_usd: float


class Runner:
    def __init__(self, ctx: Context, *, backoff: float = 1.0):
        self.ctx = ctx
        self.backoff = backoff

    async def run(
        self,
        graph: Graph,
        state: Optional[GraphState] = None,
        *,
        run_id: Optional[str] = None,
        answers: Optional[dict[str, str]] = None,
        max_steps: int = 200,
        parent: Optional[str] = None,
    ) -> RunResult:
        graph.validate()
        # normalize answers: each gate maps to a QUEUE of answers, consumed one
        # per gate visit — a loop that revisits a gate re-interrupts instead of
        # silently reusing a stale approval
        answer_queues: dict[str, list[str]] = {
            k: (list(v) if isinstance(v, list) else [str(v)])
            for k, v in (answers or {}).items()
        }
        store, emitter = self.ctx.store, self.ctx.emitter
        t0 = time.monotonic()

        if run_id and store.has(run_id):
            snap = store.load_state(run_id)
            state = graph.state_model.model_validate(snap["state"])
            current: str = snap["current"]
            visits: Counter = Counter(snap.get("visits", {}))
            step: int = snap.get("step", 0)
            used_answers: dict[str, int] = dict(snap.get("used_answers", {}))
            store.set_status(run_id, "running")
            emitter.emit(run_id, "run_resume", graph=graph.name, at=current)
        else:
            if state is None:
                raise GraphError("fresh run requires an initial state")
            run_id = run_id or store.new_run_id(graph.name)
            current = graph.entry
            visits = Counter()
            step = 0
            used_answers = {}
            store.init_run(run_id, graph.name, parent)
            store.save_state(run_id, self._snapshot(state, current, visits, step, used_answers))
            emitter.emit(run_id, "run_start", graph=graph.name)

        self.ctx.extras["current_run_id"] = run_id  # lets nodes parent nested runs
        try:
            while current != END:
                if step >= max_steps:
                    raise GraphError(f"run exceeded max_steps={max_steps}")
                node = graph.nodes[current]

                gate_answer: Optional[str] = None
                if node.kind == "gate":
                    consumed = used_answers.get(current, 0)
                    queue = answer_queues.get(current, [])
                    if consumed < len(queue):
                        gate_answer = queue[consumed]
                        used_answers[current] = consumed + 1
                    elif not self.ctx.config.autonomy.gates:
                        gate_answer = "yes"  # gates disabled -> auto-approve
                    else:
                        message = node.ask(state) if node.ask else f"Approve {current}?"
                        store.save_state(run_id, self._snapshot(state, current, visits, step, used_answers))
                        store.set_status(run_id, "waiting_gate", gate=current)
                        emitter.emit(run_id, "gate_wait", node=current, message=message)
                        raise GraphInterrupt(run_id, graph.name, current, message)

                visits[current] += 1
                if visits[current] > node.max_visits:
                    raise GraphError(f"loop bound exceeded at node {current!r} "
                                     f"({visits[current]} visits > {node.max_visits})")

                emitter.emit(run_id, "node_start", node=current, kind=node.kind, visit=visits[current])
                t_node = time.monotonic()
                if node.kind == "gate":
                    update: Optional[dict] = {node.field_name: gate_answer}
                    emitter.emit(run_id, "gate_answer", node=current, answer=gate_answer)
                else:
                    update = await self._exec(node, state, run_id)
                if update:
                    state = state.apply(update)

                nxt = graph.next_of(current, state)
                step += 1
                store.save_state(run_id, self._snapshot(state, nxt, visits, step, used_answers))
                store.append_step(run_id, {
                    "step": step, "node": current, "kind": node.kind,
                    "next": nxt, "duration_s": round(time.monotonic() - t_node, 3),
                    "updated": sorted(update.keys()) if update else [],
                })
                emitter.emit(run_id, "node_end", node=current, next=nxt,
                             duration_s=round(time.monotonic() - t_node, 2))
                current = nxt

        except GraphInterrupt:
            raise
        except GraphError as exc:
            store.finalize(run_id, "failed", error=str(exc))
            emitter.emit(run_id, "run_end", status="failed", error=str(exc))
            raise

        cost = round(sum(state.costs.values()), 4)
        duration = round(time.monotonic() - t0, 2)
        store.finalize(run_id, "completed", cost_usd=cost, steps=step, duration_s=duration)
        emitter.emit(run_id, "run_end", status="completed", cost_usd=cost, steps=step)
        return RunResult(run_id=run_id, graph=graph.name, status="completed",
                         state=state, steps=step, duration_s=duration, cost_usd=cost)

    async def _exec(self, node: Node, state: GraphState, run_id: str) -> Optional[dict]:
        last: Optional[Exception] = None
        for attempt in range(node.retries + 1):
            try:
                coro = node.fn(state, self.ctx)
                if node.timeout:
                    return await asyncio.wait_for(coro, timeout=node.timeout)
                return await coro
            except (GraphError, GraphInterrupt):
                raise
            except Exception as exc:  # noqa: BLE001 — retry boundary
                last = exc
                self.ctx.emitter.emit(run_id, "node_error", node=node.name,
                                      attempt=attempt + 1, error=f"{type(exc).__name__}: {exc}")
                if attempt < node.retries:
                    await asyncio.sleep(self.backoff * (2 ** attempt))
        raise GraphError(f"node {node.name!r} failed after {node.retries + 1} attempt(s): {last}")

    @staticmethod
    def _snapshot(state: GraphState, current: str, visits: Counter, step: int,
                  used_answers: Optional[dict[str, int]] = None) -> dict:
        return {"state": state.model_dump(), "current": current,
                "visits": dict(visits), "step": step,
                "used_answers": dict(used_answers or {})}


AskGate = Callable[[GraphInterrupt], Awaitable[str]]


async def run_to_completion(
    runner: Runner,
    graph: Graph,
    state: Optional[GraphState],
    ask: AskGate,
    *,
    run_id: Optional[str] = None,
    answers: Optional[dict[str, str]] = None,
    max_steps: int = 200,
) -> RunResult:
    """Drive a run across any number of human gates: on each GraphInterrupt,
    call `ask` for an answer and resume. Answers accumulate as per-gate queues
    so revisited gates (study iterations) ask again instead of reusing."""
    queues: dict[str, list[str]] = {
        k: (list(v) if isinstance(v, list) else [str(v)])
        for k, v in (answers or {}).items()
    }
    while True:
        try:
            return await runner.run(graph, state, run_id=run_id,
                                    answers=queues, max_steps=max_steps)
        except GraphInterrupt as gi:
            run_id, state = gi.run_id, None
            queues.setdefault(gi.gate, []).append(await ask(gi))
