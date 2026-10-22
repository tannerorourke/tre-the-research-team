from __future__ import annotations

import pytest

from triad.agents.runtime import MockRuntime
from triad.chat.session import LabChat, standup_text
from triad.graph import Runner
from triad.tools import papers as papers_mod
from triad.workflows.study import StudyState, build_study

from conftest import make_ctx, schema_factory
from test_workflows_integration import CANNED_PAPERS


@pytest.fixture(autouse=True)
def offline_papers(monkeypatch):
    for source in ("arxiv", "semantic_scholar", "hf_papers"):
        monkeypatch.setitem(papers_mod.SEARCHERS, source,
                            lambda q, n=12: list(CANNED_PAPERS))
    monkeypatch.setattr(papers_mod, "fetch_paper_text",
                        lambda arxiv_id, max_chars=30_000: "full text")


async def test_full_study_end_to_end(tmp_path):
    """The flagship path: literature -> design -> plan -> execute -> analyze
    -> retro(conclude), gates auto-approved, everything offline."""
    ctx = make_ctx(tmp_path)   # gates disabled
    state = StudyState(question="does X improve Y?", max_iterations=1,
                       test_command="true")
    result = await Runner(ctx).run(build_study(), state, max_steps=300)
    assert result.status == "completed"
    final: StudyState = result.state

    # every phase left its artifact
    assert final.review_id.startswith("REP-")           # literature review
    assert final.hyp_id.startswith("HYP-")
    assert final.exp_id.startswith("EXP-")
    assert final.adr_id.startswith("ADR-")
    assert final.ticket_ids and final.ticket_ids[0].startswith("TCK-")
    assert final.report_id.startswith("REP-")           # analysis report
    assert final.retro is not None and final.retro.decision == "conclude"

    # notebook state transitions happened
    assert ctx.notebook.frontmatter(final.exp_id)["status"] == "analyzed"
    assert ctx.notebook.frontmatter(final.ticket_ids[0])["status"] == "done"
    assert ctx.notebook.frontmatter(final.hyp_id)["status"] == "inconclusive"

    # knowledge graph lineage: EXP tests HYP, analysis report linked to HYP
    assert ctx.kg.edges(src=final.exp_id, dst=final.hyp_id, type_="tests")
    assert ctx.kg.edges(src=final.report_id, dst=final.hyp_id, type_="inconclusive")
    assert ctx.kg.edges(src=final.ticket_ids[0], dst=final.exp_id, type_="implements")

    # charter journal recorded the arc
    charter = (tmp_path / "docs" / "charter.md").read_text()
    assert "study started" in charter and "study concluded" in charter

    # costs accumulated across phases (mock charges 0.001/call)
    assert result.cost_usd > 0
    # nested developer runs were recorded under the study run
    children = [m for m in ctx.store.list_runs() if m.get("parent") == result.run_id]
    assert children, "developer runs should be parented to the study run"


async def test_study_halts_when_design_rejected(tmp_path):
    ctx = make_ctx(tmp_path, gates=True)
    state = StudyState(question="q", max_iterations=1, test_command="true")
    result = await Runner(ctx).run(
        build_study(), state, max_steps=300,
        answers={"des_approve_design": ["no — too expensive"]})
    assert result.status == "completed"
    final: StudyState = result.state
    assert any("halted" in n for n in final.run_notes)
    assert ctx.notebook.frontmatter(final.exp_id)["status"] == "designed"
    assert not final.ticket_ids                          # never planned


async def test_chat_routing_rules_and_llm_fallback(tmp_path):
    runtime = MockRuntime(default_factory=schema_factory)
    ctx = make_ctx(tmp_path, runtime=runtime)
    chat = LabChat(ctx)
    assert await chat.route("@tl how are the tests") == "tl"
    assert await chat.route("@rs what does the paper say") == "rs"
    assert await chat.route(
        "the repo tests fail after the merge of the ticket branch infra") == "tl"
    # ambiguous -> LLM router (mock returns rs)
    assert await chat.route(
        "walk me through what happened yesterday and what is next") == "rs"
    assert any(c["node"] == "chat_route" for c in runtime.calls)


def test_standup_reflects_lab_state(tmp_path):
    ctx = make_ctx(tmp_path)
    ctx.notebook.create("experiment", "Exp one", {})
    ctx.kg.add_node("EXP-001", "experiment", "Exp one")
    text = standup_text(ctx)
    assert "EXP-001" in text
    assert "Knowledge graph" in text
