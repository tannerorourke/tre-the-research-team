from __future__ import annotations

import pytest

from tre.agents.runtime import MockRuntime
from tre.agents import schemas as S
from tre.config import TreConfig
from tre.graph import Context, Emitter, RunStore
from tre.memory import KnowledgeGraph, LabNotebook

CANNED_KEYS = ["paperalpha", "paperbeta", "papergamma"]


def schema_factory(persona, prompt, schema, node):
    """Return a valid canned instance for whatever schema a node requests."""
    if schema is None:
        return f"(free-text work by {persona.key} at {node})"
    if schema is S.QueryPlan:
        return S.QueryPlan(queries=["q one", "q two"],
                           inclusion_criteria=["about the question"],
                           exclusion_criteria=["off topic"])
    if schema is S.Triage:
        return S.Triage(keep=list(CANNED_KEYS), rationale="all relevant")
    if schema is S.PaperNote:
        key = node.split(":", 1)[1] if ":" in node else CANNED_KEYS[0]
        return S.PaperNote(key=key, title=f"Paper {key}", claim="c", method="m",
                           evidence="acc 90.1 vs 88.4", relevance="rel",
                           limitations="lim", tags=["ml"])
    if schema is S.Synthesis:
        return S.Synthesis(
            themes=[S.Theme(name="theme", summary="s", papers=[CANNED_KEYS[0]])],
            gaps=["gap one"], implications="imp",
            summary_md="Body citing [paperalpha].")
    if schema is S.Critique:
        return S.Critique(verdict="accept", issues=[])
    if schema is S.HypothesisSet:
        hyp = S.Hypothesis(title="H", statement="X improves Y by ~2 pts",
                           rationale="because", falsification="no diff at n=5 seeds",
                           predicted_effect="+2 accuracy", novelty=3,
                           tractability=4, impact=3)
        return S.HypothesisSet(candidates=[hyp], selected_index=0,
                               selection_rationale="only candidate")
    if schema is S.ExperimentDesign:
        return S.ExperimentDesign(
            title="Test X vs baseline", hypothesis_statement="X improves Y",
            independent_vars="X on/off", dependent_vars="accuracy",
            baselines="tuned baseline", ablations="X variants",
            controls="5 seeds fixed split", power="5 seeds vs +2pt effect",
            stop_criteria="all seeds done", compute_budget="1 GPU-day",
            threats="seed variance")
    if schema is S.DesignReview:
        return S.DesignReview(verdict="approve", feasibility="fits infra",
                              infra_risks=[])
    if schema is S.ADRDoc:
        return S.ADRDoc(title="Implementation ADR", context="ctx",
                        decision="do it in src/", alternatives="none",
                        consequences="fine")
    if schema is S.TicketSet:
        return S.TicketSet(
            tickets=[S.Ticket(title="Implement runner", goal="g",
                              acceptance=["runs"], test_plan="test_runner",
                              files=["src/x.py"], depends_on=[])],
            sequencing_notes="single wave")
    if schema is S.ContextDigest:
        return S.ContextDigest(relevant_files=["src/x.py"], observations="obs")
    if schema is S.DevPlan:
        return S.DevPlan(approach="tdd", steps=["write test", "code"],
                         test_names=["test_x"])
    if schema is S.DevResult:
        return S.DevResult(status="done", summary_md="did the ticket",
                           files_changed=["src/x.py"])
    if schema is S.ReviewVerdict:
        return S.ReviewVerdict(verdict="approve", comments=["clean"])
    if schema is S.IntegrationNote:
        return S.IntegrationNote(summary_md="shipped wave", risks=[], followups=[])
    if schema is S.AnalysisPrep:
        return S.AnalysisPrep(data_ok=True, issues=[], metrics_found=["accuracy"])
    if schema is S.Interpretation:
        return S.Interpretation(verdict="inconclusive",
                                effect_summary="no seeds recorded",
                                interpretation_md="cannot conclude",
                                threats=["missing runs"], followups=["run it"])
    if schema is S.RedTeam:
        return S.RedTeam(alternative_explanations=["noise"], survives=True,
                         notes="holds")
    if schema is S.Retro:
        return S.Retro(decision="conclude", reasoning="question answered enough")
    raise AssertionError(f"schema_factory: unhandled schema {schema}")


def make_ctx(tmp_path, runtime=None, gates=False):
    config = TreConfig()
    config.autonomy.gates = gates
    store = RunStore(tmp_path)
    emitter = Emitter(sink=lambda rid, ev: store.append_event(rid, ev) if rid else None)
    notebook = LabNotebook(tmp_path / "docs")
    notebook.ensure_scaffold()
    kg = KnowledgeGraph(tmp_path / "docs" / "knowledge" / "graph.db")
    runtime = runtime or MockRuntime(default_factory=schema_factory)
    return Context(root=tmp_path, config=config, store=store, emitter=emitter,
                   notebook=notebook, kg=kg, runtime=runtime)


@pytest.fixture
def ctx(tmp_path):
    return make_ctx(tmp_path)


@pytest.fixture
def gated_ctx(tmp_path):
    return make_ctx(tmp_path, gates=True)
