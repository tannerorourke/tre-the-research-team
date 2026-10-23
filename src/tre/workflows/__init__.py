"""Workflow registry: name -> (graph builder, initial-state factory)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable

from ..graph import Context, Graph, GraphState
from .analysis import AnalysisState, build_analysis
from .design import DesignState, build_design
from .developer import DevState, build_developer
from .execution import ExecState, build_execution
from .literature import LitState, build_literature
from .planning import PlanState, build_planning
from .study import StudyState, build_study

EXP_RE = re.compile(r"^EXP-\d+$")
TCK_RE = re.compile(r"^TCK-\d+$")


@dataclass(frozen=True)
class WorkflowSpec:
    name: str
    description: str
    input_help: str
    build: Callable[[], Graph]
    make_state: Callable[[str, Context], GraphState]


def _lit_state(arg: str, ctx: Context) -> LitState:
    if not arg:
        raise ValueError('literature needs a question: tre run literature "…"')
    return LitState(question=arg)


def _design_state(arg: str, ctx: Context) -> DesignState:
    if not arg:
        raise ValueError('design needs a question/topic: tre run design "…"')
    return DesignState(question=arg,
                       max_design_rounds=ctx.config.autonomy.max_review_rounds)


def _plan_state(arg: str, ctx: Context) -> PlanState:
    if EXP_RE.match(arg or ""):
        title = ""
        try:
            title = ctx.notebook.frontmatter(arg).get("title", "")
        except FileNotFoundError:
            pass
        return PlanState(exp_id=arg, objective=f"Implement {arg}: {title}")
    if not arg:
        raise ValueError('planning needs an EXP id or an objective: tre run plan "…"')
    return PlanState(objective=arg)


def _exec_state(arg: str, ctx: Context) -> ExecState:
    if arg and not EXP_RE.match(arg):
        raise ValueError("execution takes an optional EXP id (scope), e.g. EXP-001")
    return ExecState(exp_id=arg or "")


def _dev_state(arg: str, ctx: Context) -> DevState:
    if not TCK_RE.match(arg or ""):
        raise ValueError("developer needs a ticket id: tre run ticket TCK-001")
    return DevState(ticket_id=arg,
                    max_debug=ctx.config.autonomy.max_debug_rounds)


def _analysis_state(arg: str, ctx: Context) -> AnalysisState:
    if not EXP_RE.match(arg or ""):
        raise ValueError("analysis needs an EXP id: tre run analysis EXP-001")
    return AnalysisState(exp_id=arg)


def _study_state(arg: str, ctx: Context) -> StudyState:
    if not arg:
        raise ValueError('study needs a research question: tre run study "…"')
    return StudyState(
        question=arg,
        max_iterations=ctx.config.autonomy.max_study_iterations,
        max_design_rounds=ctx.config.autonomy.max_review_rounds,
    )


WORKFLOWS: dict[str, WorkflowSpec] = {
    "literature": WorkflowSpec(
        "literature",
        "Research Scientist: multi-source literature review with structured notes and self-critique",
        '"research question"', build_literature, _lit_state),
    "design": WorkflowSpec(
        "design",
        "Research Scientist: candidate hypotheses -> pre-registered design -> TL feasibility -> human gate",
        '"question or topic"', build_design, _design_state),
    "plan": WorkflowSpec(
        "plan",
        "Tech Lead: repo recon -> ADR -> ticket DAG with acceptance criteria",
        "EXP-001 | \"objective\"", build_planning, _plan_state),
    "execute": WorkflowSpec(
        "execute",
        "Tech Lead: waves of parallel Developers (worktrees), review + fix loops, merge + tests",
        "[EXP-001]", build_execution, _exec_state),
    "ticket": WorkflowSpec(
        "ticket",
        "Developer: one ticket, test-driven, bounded debug loop, self-review",
        "TCK-001", build_developer, _dev_state),
    "analysis": WorkflowSpec(
        "analysis",
        "Research Scientist: deterministic stats -> interpretation vs pre-registration -> red team",
        "EXP-001", build_analysis, _analysis_state),
    "study": WorkflowSpec(
        "study",
        "Full loop: literature -> design -> plan -> execute -> analyze -> retro (iterate/conclude)",
        '"research question"', build_study, _study_state),
}

__all__ = ["WORKFLOWS", "WorkflowSpec"]
