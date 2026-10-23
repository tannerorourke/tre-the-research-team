from __future__ import annotations

import pytest

from tre.agents import schemas as S
from tre.agents.runtime import MockRuntime
from tre.graph import Runner
from tre.tools import papers as papers_mod
from tre.tools.papers import Paper
from tre.workflows.execution import ExecState, build_execution
from tre.workflows.literature import LitState, build_literature

from conftest import CANNED_KEYS, make_ctx, schema_factory

CANNED_PAPERS = [
    Paper(key=CANNED_KEYS[0], source="arxiv", ext_id="2101.00001",
          title="Paper Alpha", authors=["A"], year=2021,
          abstract="alpha abstract", url="https://arxiv.org/abs/2101.00001"),
    Paper(key=CANNED_KEYS[1], source="semantic_scholar", ext_id="s2-1",
          title="Paper Beta", authors=["B"], year=2022,
          abstract="beta abstract", url="https://example.org/b", citations=10),
    Paper(key=CANNED_KEYS[2], source="hf_papers", ext_id="2201.00002",
          title="Paper Gamma", authors=["C"], year=2023,
          abstract="gamma abstract", url="https://hf.co/papers/2201.00002"),
]


@pytest.fixture(autouse=True)
def offline_papers(monkeypatch):
    def fake_search(query, max_results=12):
        return list(CANNED_PAPERS)
    for source in ("arxiv", "semantic_scholar", "hf_papers"):
        monkeypatch.setitem(papers_mod.SEARCHERS, source, fake_search)
    monkeypatch.setattr(papers_mod, "fetch_paper_text",
                        lambda arxiv_id, max_chars=30_000: f"full text of {arxiv_id}")


async def test_literature_end_to_end(tmp_path):
    ctx = make_ctx(tmp_path)
    result = await Runner(ctx).run(
        build_literature(), LitState(question="does X improve Y?"))
    assert result.status == "completed"
    state: LitState = result.state
    assert len(state.papers) == 3                      # deduped canned set
    assert len(state.notes) == 3
    assert state.review_id.startswith("REP-")
    assert len(state.lit_ids) == 3

    # notebook artifacts exist and are indexed
    lits = ctx.notebook.entries("literature")
    assert {e["id"] for e in lits} == set(state.lit_ids)
    review = ctx.notebook.read(state.review_id)
    assert "does X improve Y?" in review and "Gaps" in review

    # knowledge graph lineage: review cites every note
    edges = ctx.kg.edges(src=state.review_id, type_="cites")
    assert len(edges) == 3
    # costs were recorded for agent nodes
    assert any(k.startswith("annotate:") for k in state.costs)
    assert state.costs.get("synthesize", 0) > 0


async def test_literature_revision_loop(tmp_path):
    runtime = MockRuntime(default_factory=schema_factory)
    runtime.enqueue("self_critique",
                    S.Critique(verdict="revise", issues=["weak gaps"],
                               instructions="sharpen the gaps"),
                    S.Critique(verdict="accept", issues=[]))
    ctx = make_ctx(tmp_path, runtime=runtime)
    result = await Runner(ctx).run(build_literature(), LitState(question="q"))
    assert result.status == "completed"
    # synthesize ran twice (draft + revision)
    synth_calls = [c for c in runtime.calls if c["node"] == "synthesize"]
    assert len(synth_calls) == 2
    assert "sharpen the gaps" in synth_calls[1]["prompt"]


async def test_execution_review_fix_loop_no_git(tmp_path):
    runtime = MockRuntime(default_factory=schema_factory)
    ctx = make_ctx(tmp_path, runtime=runtime)
    exp_id, _ = ctx.notebook.create("experiment", "The experiment", {})
    tck_id, _ = ctx.notebook.create("ticket", "Build the runner", {
        "experiment": exp_id, "goal": "g", "acceptance": "- runs",
        "test_plan": "tp", "depends_on": "_",
    })
    # first review requests changes; after the fix round it approves
    runtime.enqueue(f"review:{tck_id}",
                    S.ReviewVerdict(verdict="request_changes",
                                    comments=["rename the flag"],
                                    blocking=["flag name wrong"]),
                    S.ReviewVerdict(verdict="approve", comments=["good now"]))

    result = await Runner(ctx).run(
        build_execution(), ExecState(exp_id=exp_id, test_command="true"),
        max_steps=100)
    assert result.status == "completed"
    state: ExecState = result.state
    assert state.fix_rounds.get(tck_id) == 1
    assert state.review_verdicts[tck_id]["verdict"] == "approve"
    assert state.dev_results[tck_id]["mode"] == "fix"      # fix round ran
    assert ctx.notebook.frontmatter(tck_id)["status"] == "done"
    assert ctx.notebook.frontmatter(exp_id)["status"] == "running"
    assert state.report is not None
    # the fix-round developer saw the review comments
    fix_calls = [c for c in runtime.calls
                 if c["node"] == "implement" and "FIX round" in c["prompt"]]
    assert fix_calls, "fix round should re-run implement with review comments"


async def test_execution_gate_abort(tmp_path):
    runtime = MockRuntime(default_factory=schema_factory)
    ctx = make_ctx(tmp_path, runtime=runtime, gates=True)
    ctx.notebook.create("ticket", "T", {"depends_on": "_"})
    result = await Runner(ctx).run(
        build_execution(), ExecState(test_command="true"),
        answers={"approve_execution": ["no — hold off"]})
    assert result.status == "completed"
    assert any("aborted" in n for n in result.state.run_notes)
    assert not result.state.dev_results                  # nothing dispatched
