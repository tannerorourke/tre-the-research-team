"""The flagship orchestration: a full study, end to end.

    kickoff -> literature -> design (TL feasibility + human gate)
            -> planning -> execution (parallel devs, review, integrate)
            -> analysis (stats, red team) -> retro
            -> iterate (back to design, focus sharpened) | conclude

Built by splicing the five phase graphs into one flat, checkpointed,
resumable graph — human gates included — with a bounded iterate loop.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from ..agents.schemas import Retro
from ..graph import Context, END, Graph, GraphState, START, Replace
from .analysis import AnalysisState, build_analysis
from .common import agent_step, clip
from .design import DesignState, build_design
from .execution import ExecState, build_execution
from .literature import LitState, build_literature
from .planning import PlanState, build_planning


class StudyState(LitState, DesignState, PlanState, ExecState, AnalysisState):
    iteration: int = 0
    max_iterations: int = 2
    retro: Optional[Retro] = None


def _charter_append(ctx: Context, line: str) -> None:
    charter = ctx.docs_dir / "charter.md"
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    try:
        with charter.open("a") as f:
            f.write(f"- {stamp} — {line}\n")
    except OSError:
        pass


# -- glue nodes --------------------------------------------------------------

async def kickoff(state: StudyState, ctx: Context) -> dict:
    _charter_append(ctx, f"study started: {state.question}")
    return {"run_notes": [f"kickoff: {state.question!r}, "
                          f"max {state.max_iterations} iteration(s)"]}


async def bridge_lit_design(state: StudyState, ctx: Context) -> dict:
    lit = state.synthesis.summary_md if state.synthesis else ""
    return {"lit_context": clip(lit, 6000),
            "run_notes": ["bridge: literature -> design"]}


async def bridge_design_plan(state: StudyState, ctx: Context) -> dict:
    title = state.design.title if state.design else state.question
    return {"objective": f"Implement pre-registered experiment {state.exp_id}: {title}",
            "run_notes": [f"bridge: design -> planning ({state.exp_id})"]}


def route_design_approved(state: StudyState) -> str:
    return "go" if state.human_decision.strip().lower().startswith(("y", "approve")) else "halt"


async def halted(state: StudyState, ctx: Context) -> dict:
    _charter_append(ctx, f"study paused: design {state.exp_id} not approved "
                         f"({state.human_decision!r})")
    return {"run_notes": [f"study halted at design approval: {state.human_decision!r}. "
                          f"{state.exp_id} registered as draft; approve it and run "
                          f"planning/execution/analysis individually to continue."]}


async def bridge_exec_analysis(state: StudyState, ctx: Context) -> dict:
    return {"run_notes": [f"bridge: execution -> analysis ({state.exp_id})"]}


async def iterate_reset(state: StudyState, ctx: Context) -> dict:
    """Clear phase state for the next design cycle; carry forward the
    literature, the sharpened focus, and pointers to prior evidence."""
    assert state.retro is not None
    focus = state.retro.next_focus or state.question
    prior = (f"(iteration {state.iteration + 1}) Focus: {focus}\n\n"
             f"Previous cycle: {state.exp_id or '?'} -> "
             f"{state.interpretation.verdict if state.interpretation else '?'} "
             f"(analysis [[{state.report_id}]]).\n\n"
             f"Prior literature synthesis:\n{clip(state.lit_context, 3000)}")
    _charter_append(ctx, f"iteration {state.iteration + 1} started: {focus}")
    return {
        "iteration": state.iteration + 1,
        "lit_context": prior,
        # design phase
        "hypotheses": None, "design": None, "design_review": None,
        "design_round": 0, "human_decision": "", "hyp_id": "", "exp_id": "",
        # planning phase
        "recon": "", "adr": None, "tickets": None, "adr_id": "",
        "ticket_ids": Replace([]), "waves": Replace([]),
        # execution phase
        "wave_index": 0, "scope": Replace([]), "scope_titles": Replace({}),
        "dev_results": Replace({}), "review_verdicts": Replace({}),
        "fix_rounds": Replace({}), "integration_log": Replace([]), "report": None,
        # analysis phase
        "prereg": "", "results_files": Replace({}), "prep": None, "stats_md": "",
        "interpretation": None, "red_team": None, "analysis_round": 0, "report_id": "",
        "run_notes": [f"iterate_reset: entering iteration {state.iteration + 1}"],
    }


# -- retro -------------------------------------------------------------------

def _retro_prompt(state: StudyState, ctx: Context) -> str:
    interp = state.interpretation
    report = state.report
    return (
        f"End-of-cycle retro for the study: {state.question}\n"
        f"Iteration {state.iteration} of at most {state.max_iterations}.\n\n"
        f"Experiment: {state.exp_id} — verdict: {interp.verdict if interp else 'n/a'}\n"
        f"Effect: {interp.effect_summary if interp else 'n/a'}\n"
        f"Follow-ups proposed by analysis:\n"
        f"{chr(10).join('- ' + f for f in (interp.followups if interp else [])) or '- (none)'}\n\n"
        f"Engineering state (Tech Lead report):\n"
        f"{clip(report.summary_md if report else '(none)', 2500)}\n"
        f"Engineering risks: {', '.join(report.risks) if report and report.risks else '(none)'}\n\n"
        "Decide: iterate (another design cycle would materially advance the "
        "question — give next_focus as one sharpened, falsifiable direction) "
        "or conclude (the question is answered, or further cycles hit "
        "diminishing returns — say which, honestly). Weigh both the science "
        "and the engineering cost."
    )


def _retro_out(data: Retro, state: StudyState, ctx: Context) -> dict:
    _charter_append(ctx, f"iteration {state.iteration} retro: "
                         f"{state.interpretation.verdict if state.interpretation else '?'}; "
                         f"decision: {data.decision}")
    return {"retro": data}


def route_retro(state: StudyState) -> str:
    if (state.retro and state.retro.decision == "iterate"
            and state.iteration + 1 < state.max_iterations):
        return "iterate"
    return "conclude"


async def conclude(state: StudyState, ctx: Context) -> dict:
    verdict = state.interpretation.verdict if state.interpretation else "n/a"
    _charter_append(ctx, f"study concluded after iteration {state.iteration}: "
                         f"{verdict}. {state.retro.reasoning if state.retro else ''}")
    return {"run_notes": [f"study concluded: {verdict}"]}


# -- assembly ----------------------------------------------------------------

def build_study() -> Graph:
    g = Graph("study", StudyState,
              doc="Full study: literature -> design -> plan -> execute -> analyze -> retro loop")

    lit_entry, lit_exits = g.splice(build_literature(), prefix="lit_")
    des_entry, des_exits = g.splice(build_design(), prefix="des_")
    plan_entry, plan_exits = g.splice(build_planning(), prefix="plan_")
    exec_entry, exec_exits = g.splice(build_execution(), prefix="exec_")
    ana_entry, ana_exits = g.splice(build_analysis(), prefix="ana_")

    g.task("kickoff", kickoff)
    g.task("bridge_lit_design", bridge_lit_design)
    g.task("check_design_approval", bridge_design_plan)
    g.task("study_halted", halted)
    g.task("bridge_exec_analysis", bridge_exec_analysis)
    g.add("retro", agent_step("retro", "rs", _retro_prompt, Retro, _retro_out),
          kind="agent", retries=1, max_visits=4)
    g.task("iterate_reset", iterate_reset, max_visits=4)
    g.task("conclude", conclude)

    g.edge(START, "kickoff")
    g.edge("kickoff", lit_entry)
    for exit_node in lit_exits:
        g.rewire_end(exit_node, "bridge_lit_design")
    g.edge("bridge_lit_design", des_entry)
    for exit_node in des_exits:
        g.rewire_end(exit_node, "check_design_approval")
    g.branch("check_design_approval", route_design_approved,
             {"go": plan_entry, "halt": "study_halted"})
    g.edge("study_halted", END)
    for exit_node in plan_exits:
        g.rewire_end(exit_node, exec_entry)
    for exit_node in exec_exits:
        g.rewire_end(exit_node, "bridge_exec_analysis")
    # an aborted execution gate ends the study, not the analysis
    g.edges["exec_abort"] = END
    g.edge("bridge_exec_analysis", ana_entry)
    for exit_node in ana_exits:
        g.rewire_end(exit_node, "retro")
    g.branch("retro", route_retro, {"iterate": "iterate_reset", "conclude": "conclude"})
    g.edge("iterate_reset", des_entry)
    g.edge("conclude", END)
    return g
