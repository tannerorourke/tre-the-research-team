from __future__ import annotations

import pytest
from pydantic import Field

from triad.graph import (END, Graph, GraphError, GraphState, Replace, Runner,
                         START, parallel_map, split_errors)


class S(GraphState):
    __reducers__ = {"items": "extend", "tally": "sum_merge"}
    value: int = 0
    items: list[str] = Field(default_factory=list)
    tally: dict[str, int] = Field(default_factory=dict)
    label: str = ""


def linear_graph():
    g = Graph("lin", S)

    async def a(state, ctx):
        return {"value": state.value + 1, "items": ["a"], "tally": {"n": 1}}

    async def b(state, ctx):
        return {"value": state.value * 10, "items": ["b"], "tally": {"n": 2}}

    g.task("a", a).task("b", b)
    g.edge(START, "a").edge("a", "b").edge("b", END)
    return g


async def test_linear_run_and_reducers(ctx):
    result = await Runner(ctx).run(linear_graph(), S(value=1))
    assert result.status == "completed"
    assert result.state.value == 20
    assert result.state.items == ["a", "b"]        # extend
    assert result.state.tally == {"n": 3}          # sum_merge
    assert result.steps == 2
    meta = ctx.store.meta(result.run_id)
    assert meta["status"] == "completed"


async def test_branch_loop_and_bound(ctx):
    g = Graph("loop", S)

    async def bump(state, ctx_):
        return {"value": state.value + 1}

    g.task("bump", bump, max_visits=3)
    g.edge(START, "bump")
    g.branch("bump", lambda s: "again" if s.value < 10 else "done",
             {"again": "bump", "done": END})
    with pytest.raises(GraphError, match="loop bound"):
        await Runner(ctx).run(g, S(value=0))
    # under the bound it completes
    result = await Runner(ctx).run(g, S(value=8))
    assert result.state.value == 10


async def test_unknown_field_rejected(ctx):
    g = Graph("bad", S)

    async def oops(state, ctx_):
        return {"nope": 1}

    g.task("oops", oops)
    g.edge(START, "oops").edge("oops", END)
    with pytest.raises(GraphError, match="nope"):
        await Runner(ctx).run(g, S())


async def test_retries_then_success(ctx):
    attempts = {"n": 0}

    async def flaky(state, ctx_):
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise RuntimeError("transient")
        return {"label": "ok"}

    g = Graph("retry", S)
    g.task("flaky", flaky, retries=2)
    g.edge(START, "flaky").edge("flaky", END)
    result = await Runner(ctx, backoff=0).run(g, S())
    assert result.state.label == "ok"
    assert attempts["n"] == 3


async def test_replace_marker_resets_reducer_field(ctx):
    g = Graph("reset", S)

    async def fill(state, ctx_):
        return {"items": ["x", "y"], "tally": {"n": 5}}

    async def reset(state, ctx_):
        return {"items": Replace([]), "tally": Replace({})}

    g.task("fill", fill).task("reset", reset)
    g.edge(START, "fill").edge("fill", "reset").edge("reset", END)
    result = await Runner(ctx).run(g, S())
    assert result.state.items == []
    assert result.state.tally == {}


def test_validation_catches_dead_ends_and_unreachable():
    g = Graph("v", S)

    async def noop(state, ctx_):
        return None

    g.task("a", noop).task("orphan", noop)
    g.edge(START, "a").edge("a", END).edge("orphan", END)
    with pytest.raises(GraphError, match="unreachable"):
        g.validate()

    g2 = Graph("v2", S)
    g2.task("a", noop)
    g2.edge(START, "a")
    with pytest.raises(GraphError, match="dead end"):
        g2.validate()


def test_mermaid_contains_nodes_and_labels():
    text = linear_graph().to_mermaid()
    assert "flowchart TD" in text
    assert '"a"' in text and '"b"' in text


async def test_parallel_map_isolates_errors():
    async def worker(n):
        if n == 2:
            raise ValueError("boom")
        return n * 2

    results = await parallel_map([1, 2, 3], worker, limit=2)
    ok, errors = split_errors(results)
    assert ok == [2, 6]
    assert len(errors) == 1 and "boom" in errors[0].error
