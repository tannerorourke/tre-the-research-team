"""Typed contracts between agents.

Every agent-to-agent and agent-to-engine handoff is a pydantic schema,
enforced at the SDK layer via structured outputs. Schemas are the interface
spec of the lab: the Research Scientist's design must be complete enough for
the Tech Lead to plan from; a ticket must be complete enough for a Developer
to build without guessing.
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field


# -- research scientist: literature -----------------------------------------

class QueryPlan(BaseModel):
    queries: list[str] = Field(description="3-6 distinct search strings covering the question from different angles")
    inclusion_criteria: list[str]
    exclusion_criteria: list[str]
    notes: str = ""


class Triage(BaseModel):
    keep: list[str] = Field(description="dedupe keys of papers to keep, ranked most relevant first")
    rationale: str


class PaperNote(BaseModel):
    key: str = Field(description="dedupe key of the paper this note covers")
    title: str
    claim: str = Field(description="the paper's central claim, one or two sentences")
    method: str
    evidence: str = Field(description="the actual numbers/experiments backing the claim")
    relevance: str = Field(description="specific bearing on OUR research question")
    limitations: str
    quotes: str = Field(default="", description="key figures worth citing, verbatim")
    tags: list[str] = Field(default_factory=list)


class Theme(BaseModel):
    name: str
    summary: str
    papers: list[str] = Field(description="dedupe keys of supporting papers")


class Synthesis(BaseModel):
    themes: list[Theme]
    gaps: list[str] = Field(description="open questions the literature does not answer")
    implications: str = Field(description="what this means for our research question")
    summary_md: str = Field(description="the full literature review body, markdown, citing papers by [key]")


class Critique(BaseModel):
    verdict: Literal["accept", "revise"]
    issues: list[str]
    instructions: str = Field(default="", description="concrete revision instructions when verdict=revise")


# -- research scientist: hypotheses & design --------------------------------

class Hypothesis(BaseModel):
    title: str
    statement: str = Field(description="precise, falsifiable statement")
    rationale: str
    falsification: str = Field(description="what observation would refute this")
    predicted_effect: str = Field(description="direction AND rough magnitude, with the metric named")
    novelty: int = Field(ge=1, le=5)
    tractability: int = Field(ge=1, le=5, description="feasibility with our compute/data")
    impact: int = Field(ge=1, le=5)


class HypothesisSet(BaseModel):
    candidates: list[Hypothesis] = Field(description="independent candidate hypotheses (aim for 3)")
    selected_index: int = Field(ge=0)
    selection_rationale: str


class ExperimentDesign(BaseModel):
    title: str
    hypothesis_statement: str
    independent_vars: str
    dependent_vars: str = Field(description="metrics, with exact definitions")
    baselines: str = Field(description="strongest fair baselines and how they are tuned")
    ablations: str
    controls: str = Field(description="seeds, data splits, fixed confounders")
    power: str = Field(description="how many runs/seeds and why that is enough to see the predicted effect")
    stop_criteria: str
    compute_budget: str
    threats: str = Field(description="threats to validity and mitigations")


class DesignReview(BaseModel):
    verdict: Literal["approve", "revise"]
    feasibility: str = Field(description="engineering feasibility assessment")
    infra_risks: list[str]
    required_changes: list[str] = Field(default_factory=list)


# -- tech lead: planning & review -------------------------------------------

class ADRDoc(BaseModel):
    title: str
    context: str
    decision: str
    alternatives: str
    consequences: str


class Ticket(BaseModel):
    title: str
    goal: str
    acceptance: list[str] = Field(description="verifiable acceptance criteria")
    test_plan: str
    files: list[str] = Field(default_factory=list, description="files likely touched")
    out_of_scope: str = ""
    depends_on: list[int] = Field(default_factory=list, description="indexes of prerequisite tickets in this set")


class TicketSet(BaseModel):
    tickets: list[Ticket]
    sequencing_notes: str


class ReviewVerdict(BaseModel):
    verdict: Literal["approve", "request_changes"]
    comments: list[str]
    blocking: list[str] = Field(default_factory=list, description="issues that must be fixed before approval")


class IntegrationNote(BaseModel):
    summary_md: str
    risks: list[str] = Field(default_factory=list)
    followups: list[str] = Field(default_factory=list)


# -- developer ---------------------------------------------------------------

class ContextDigest(BaseModel):
    relevant_files: list[str]
    observations: str = Field(description="conventions, integration points, pitfalls observed in the codebase")


class DevPlan(BaseModel):
    approach: str
    steps: list[str]
    test_names: list[str] = Field(description="tests to write FIRST")
    risks: str = ""


class DevResult(BaseModel):
    status: Literal["done", "blocked"]
    summary_md: str = Field(description="PR-style summary: what changed, why, how verified")
    files_changed: list[str] = Field(default_factory=list)
    question: str = Field(default="", description="the precise blocker question when status=blocked")


# -- analysis ----------------------------------------------------------------

class AnalysisPrep(BaseModel):
    data_ok: bool
    issues: list[str] = Field(default_factory=list)
    metrics_found: list[str] = Field(default_factory=list)


class Interpretation(BaseModel):
    verdict: Literal["supported", "refuted", "inconclusive"]
    effect_summary: str = Field(description="observed effect vs predicted, with numbers from the stats tables")
    interpretation_md: str = Field(description="full interpretation, markdown")
    threats: list[str]
    followups: list[str]


class RedTeam(BaseModel):
    alternative_explanations: list[str]
    survives: bool = Field(description="does the interpretation survive the strongest alternative?")
    notes: str


# -- orchestration -----------------------------------------------------------

class Retro(BaseModel):
    decision: Literal["iterate", "conclude"]
    reasoning: str
    next_focus: str = Field(default="", description="when iterating: the sharpened question for the next cycle")


class RouteDecision(BaseModel):
    target: Literal["rs", "tl", "both"]
    reason: str


class Reply(BaseModel):
    """Free-form chat reply wrapper (keeps chat and workflows on one runtime path)."""
    text: str


SCHEMAS: dict[str, type[BaseModel]] = {
    cls.__name__: cls
    for cls in [
        QueryPlan, Triage, PaperNote, Theme, Synthesis, Critique, Hypothesis,
        HypothesisSet, ExperimentDesign, DesignReview, ADRDoc, Ticket, TicketSet,
        ReviewVerdict, IntegrationNote, ContextDigest, DevPlan, DevResult,
        AnalysisPrep, Interpretation, RedTeam, Retro, RouteDecision, Reply,
    ]
}
