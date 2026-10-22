from __future__ import annotations

import pytest
from pydantic import Field

from triad.graph import (END, Graph, GraphInterrupt, GraphState, Runner, START,
                         run_to_completion)


class S(GraphState):
    __reducers__ = {"trail": "extend"}
    trail: list[str] = Field(default_factory=list)
    human_decision: str = ""
    loops: int = 0


def gated_graph(max_loops=2):
    g = Graph("gated", S)

    async def before(state, ctx):
        return {"trail": ["before"]}

    async def after(state, ctx):
        return {"trail": [f"after:{state.human_decision}"], "loops": state.loops + 1}

    g.task("before", before)
    g.gate("approve", lambda s: f"loop {s.loops}: proceed?")
    g.task("after", after)
    g.edge(START, "before").edge("before", "approve").edge("approve", "after")
    g.branch("after", lambda s: "again" if s.loops < max_loops else "done",
             {"again": "approve", "done": END})
    return g


async def test_gate_interrupt_and_resume(gated_ctx):
    runner = Runner(gated_ctx)
    with pytest.raises(GraphInterrupt) as exc_info:
        await runner.run(gated_graph(max_loops=1), S())
    gi = exc_info.value
    assert gi.gate == "approve"
    meta = gated_ctx.store.meta(gi.run_id)
    assert meta["status"] == "waiting_gate"
    # resume with the answer; loop bound 1 -> completes after one pass
    result = await runner.run(gated_graph(max_loops=1), run_id=gi.run_id,
                              answers={"approve": "yes go"})
    assert result.status == "completed"
    assert result.state.trail == ["before", "after:yes go"]


async def test_revisited_gate_asks_again_not_reuses(gated_ctx):
    """A loop that revisits a gate must re-interrupt, not silently reuse the
    old approval."""
    runner = Runner(gated_ctx)
    asked: list[str] = []

    async def ask(gi: GraphInterrupt) -> str:
        asked.append(gi.message)
        return f"yes-{len(asked)}"

    result = await run_to_completion(runner, gated_graph(max_loops=2), S(), ask)
    assert result.status == "completed"
    assert len(asked) == 2                      # gate visited twice, asked twice
    assert result.state.trail == ["before", "after:yes-1", "after:yes-2"]


async def test_gates_disabled_auto_approve(ctx):
    result = await Runner(ctx).run(gated_graph(max_loops=1), S())
    assert result.status == "completed"
    assert result.state.trail == ["before", "after:yes"]


async def test_state_snapshot_survives_between_processes(gated_ctx):
    """Simulate a crash/restart: a fresh Runner resumes purely from disk."""
    with pytest.raises(GraphInterrupt) as exc_info:
        await Runner(gated_ctx).run(gated_graph(max_loops=1), S())
    run_id = exc_info.value.run_id
    fresh = Runner(gated_ctx)   # new instance, state only on disk
    result = await fresh.run(gated_graph(max_loops=1), run_id=run_id,
                             answers={"approve": ["ok"]})
    assert result.state.trail == ["before", "after:ok"]
    steps = gated_ctx.store.steps(run_id)
    assert [s["node"] for s in steps] == ["before", "approve", "after"]
