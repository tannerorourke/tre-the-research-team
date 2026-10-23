"""Research Scientist workflow: hypothesis formation + experiment design.

pull context -> generate candidate hypotheses (scored for falsifiability,
novelty, tractability, impact) -> draft pre-registration -> Tech Lead
feasibility review loop -> human approval gate -> register HYP/EXP entries
and knowledge-graph lineage.
"""

from __future__ import annotations

from typing import Optional

from ..agents.schemas import DesignReview, ExperimentDesign, Hypothesis, HypothesisSet
from ..graph import Context, END, Graph, GraphState, START
from .common import agent_step, bullet, clip


class DesignState(GraphState):
    question: str = ""
    context_digest: str = ""
    lit_context: str = ""
    hypotheses: Optional[HypothesisSet] = None
    design: Optional[ExperimentDesign] = None
    design_review: Optional[DesignReview] = None
    design_round: int = 0
    max_design_rounds: int = 2
    human_decision: str = ""
    hyp_id: str = ""
    exp_id: str = ""

    def selected(self) -> Optional[Hypothesis]:
        if not self.hypotheses or not self.hypotheses.candidates:
            return None
        i = min(self.hypotheses.selected_index, len(self.hypotheses.candidates) - 1)
        return self.hypotheses.candidates[i]


# -- tool nodes --------------------------------------------------------------

async def pull_design_context(state: DesignState, ctx: Context) -> dict:
    digest = clip(ctx.notebook.digest(), 4000)
    lit = state.lit_context
    if not lit:
        reports = ctx.notebook.entries("report")
        if reports:
            try:
                lit = clip(ctx.notebook.read(reports[-1]["id"]), 6000)
            except FileNotFoundError:
                lit = ""
    return {"context_digest": digest, "lit_context": lit}


async def register(state: DesignState, ctx: Context) -> dict:
    hyp = state.selected()
    design = state.design
    assert hyp is not None and design is not None
    approved = state.human_decision.strip().lower().startswith(("y", "approve"))
    others = [c for i, c in enumerate(state.hypotheses.candidates)
              if i != state.hypotheses.selected_index]
    hyp_id, _ = ctx.notebook.create("hypothesis", hyp.title, {
        "statement": hyp.statement,
        "rationale": hyp.rationale,
        "falsification": hyp.falsification,
        "predicted_effect": hyp.predicted_effect,
        "alternatives": "\n\n".join(f"**{c.title}** — {c.statement}" for c in others) or "_",
    })
    ctx.notebook.set_frontmatter(hyp_id, status="testing" if approved else "proposed")
    exp_id, _ = ctx.notebook.create("experiment", design.title, {
        "hypothesis": hyp_id,
        "links": f"[[{hyp_id}]]",
        "hypothesis_statement": design.hypothesis_statement,
        "independent_vars": design.independent_vars,
        "dependent_vars": design.dependent_vars,
        "baselines": design.baselines,
        "ablations": design.ablations,
        "controls": design.controls,
        "power": design.power,
        "stop_criteria": design.stop_criteria,
        "compute_budget": design.compute_budget,
        "threats": design.threats,
        "log": f"- registered ({'approved' if approved else 'pending approval'}); "
               f"human note: {state.human_decision or '(none)'}",
    })
    ctx.notebook.set_frontmatter(exp_id, status="approved" if approved else "designed")
    ctx.kg.add_node(hyp_id, "hypothesis", hyp.title)
    ctx.kg.add_node(exp_id, "experiment", design.title)
    ctx.kg.add_edge(exp_id, hyp_id, "tests")
    return {
        "hyp_id": hyp_id, "exp_id": exp_id,
        "run_notes": [f"registered {hyp_id} + {exp_id} "
                      f"({'approved' if approved else 'not yet approved'})"],
    }


# -- agent nodes -------------------------------------------------------------

def _hypothesize_prompt(state: DesignState, ctx: Context) -> str:
    return (
        f"Research question: {state.question}\n\n"
        f"Lab context:\n{state.context_digest}\n\n"
        f"Literature context:\n{clip(state.lit_context, 5000)}\n\n"
        "Generate 3 independent candidate hypotheses attacking this question "
        "from different mechanisms — not three phrasings of one idea. Each "
        "must be falsifiable (state the refuting observation), with a "
        "predicted effect naming metric, direction, and rough magnitude. "
        "Score each 1-5 on novelty, tractability (our compute/data), impact. "
        "Select the best overall; justify against the runners-up."
    )


def _design_prompt(state: DesignState, ctx: Context) -> str:
    hyp = state.selected()
    assert hyp is not None
    revision = ""
    if state.design_review and state.design_review.verdict == "revise":
        revision = (
            f"\n\nRevision round {state.design_round}. The Tech Lead rejected "
            f"the previous design:\nFeasibility: {state.design_review.feasibility}\n"
            f"Infra risks:\n{bullet(state.design_review.infra_risks)}\n"
            f"Required changes:\n{bullet(state.design_review.required_changes)}\n"
            "Address every required change or explicitly rebut it on scientific grounds."
        )
    return (
        f"Hypothesis to test:\n{hyp.statement}\n\n"
        f"Falsification criteria: {hyp.falsification}\n"
        f"Predicted effect: {hyp.predicted_effect}\n"
        f"Rationale: {hyp.rationale}\n\n"
        f"Lab context:\n{state.context_digest}\n"
        f"{revision}\n\n"
        "Write the full pre-registration. Baselines must be the strongest fair "
        "ones. Power reasoning must justify seed/run counts against the "
        "predicted effect size. Stop criteria must prevent both p-hacking and "
        "runaway compute. Every field substantive."
    )


def _feasibility_prompt(state: DesignState, ctx: Context) -> str:
    d = state.design
    assert d is not None
    return (
        "Feasibility review of a pre-registered experiment design. Judge "
        "ENGINEERING only (data availability, compute realism, implementation "
        "complexity, timeline); science is the Research Scientist's call.\n\n"
        f"Title: {d.title}\nHypothesis: {d.hypothesis_statement}\n"
        f"Independent vars: {d.independent_vars}\nMetrics: {d.dependent_vars}\n"
        f"Baselines: {d.baselines}\nAblations: {d.ablations}\n"
        f"Controls: {d.controls}\nPower: {d.power}\n"
        f"Compute budget: {d.compute_budget}\nStop criteria: {d.stop_criteria}\n\n"
        "Inspect the repository (Read/Grep/Glob and lab tools) if you need to "
        "check what infrastructure already exists. verdict=revise requires "
        "concrete required_changes; vague concern is not a verdict."
    )


def route_design(state: DesignState) -> str:
    if (state.design_review and state.design_review.verdict == "revise"
            and state.design_round <= state.max_design_rounds):
        return "revise"
    return "approve"


def _gate_message(state: DesignState) -> str:
    hyp = state.selected()
    d = state.design
    lines = [
        "Register this experiment?",
        f"  Hypothesis: {hyp.statement if hyp else '?'}",
        f"  Design: {d.title if d else '?'}",
        f"  Predicted effect: {hyp.predicted_effect if hyp else '?'}",
        f"  Compute budget: {d.compute_budget if d else '?'}",
        f"  TL feasibility: {state.design_review.verdict if state.design_review else 'n/a'}",
        "Answer yes to approve, or anything else to register as draft with your note.",
    ]
    return "\n".join(lines)


def build_design() -> Graph:
    g = Graph("design", DesignState,
              doc="RS hypothesis + experiment design with TL feasibility loop and human approval gate")
    g.tool("pull_design_context", pull_design_context)
    g.add("hypothesize", agent_step(
        "hypothesize", "rs", _hypothesize_prompt, HypothesisSet,
        lambda data, s, c: {"hypotheses": data}, use_lab_tools=False),
        kind="agent", retries=1)
    g.add("draft_design", agent_step(
        "draft_design", "rs", _design_prompt, ExperimentDesign,
        lambda data, s, c: {"design": data}, use_lab_tools=False),
        kind="agent", retries=1, max_visits=4)
    g.add("feasibility", agent_step(
        "feasibility", "tl", _feasibility_prompt, DesignReview,
        lambda data, s, c: {"design_review": data, "design_round": s.design_round + 1}),
        kind="agent", retries=1, max_visits=4)
    g.gate("approve_design", _gate_message, field_name="human_decision")
    g.tool("register", register)

    g.edge(START, "pull_design_context")
    g.chain("pull_design_context", "hypothesize", "draft_design", "feasibility")
    g.branch("feasibility", route_design,
             {"revise": "draft_design", "approve": "approve_design"})
    g.chain("approve_design", "register")
    g.edge("register", END)
    return g
