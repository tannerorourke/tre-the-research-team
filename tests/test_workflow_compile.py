from __future__ import annotations

from triad.workflows import WORKFLOWS
from triad.workflows.study import StudyState, build_study


def test_all_workflows_validate_and_render():
    for name, spec in WORKFLOWS.items():
        graph = spec.build()
        graph.validate()          # raises on structural defects
        mermaid = graph.to_mermaid()
        assert "flowchart TD" in mermaid
        assert len(graph.nodes) >= 3, name


def test_study_splices_all_phases_with_gates():
    g = build_study()
    g.validate()
    names = set(g.nodes)
    for expected in ("lit_plan_queries", "des_approve_design", "plan_decompose",
                     "exec_dispatch_wave", "ana_red_team", "retro",
                     "iterate_reset", "conclude"):
        assert expected in names, expected
    # aborting execution ends the study rather than analyzing nothing
    assert g.edges["exec_abort"] == "__end__"
    # gate nodes present and marked
    assert g.nodes["des_approve_design"].kind == "gate"
    assert g.nodes["exec_approve_execution"].kind == "gate"


def test_study_state_merges_phase_fields_and_reducers():
    state = StudyState(question="q")
    fields = type(state).model_fields
    for f in ("papers", "hypotheses", "tickets", "dev_results",
              "interpretation", "retro", "iteration"):
        assert f in fields, f
    reducers = StudyState.reducers()
    assert reducers["papers"] == "extend"
    assert reducers["dev_results"] == "merge"
    assert reducers["costs"] == "sum_merge"
