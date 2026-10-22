"""Research Scientist workflow: literature review.

plan queries -> parallel multi-source search -> triage -> deep-read top
papers -> parallel structured annotation -> synthesis -> self-critique loop
-> persist notes + review + knowledge-graph edges.
"""

from __future__ import annotations

import asyncio
from typing import Optional

from pydantic import Field

from ..agents.schemas import Critique, PaperNote, QueryPlan, Synthesis, Triage
from ..graph import Context, END, Graph, GraphState, START, parallel_map, split_errors
from ..tools import papers as papers_mod
from ..tools.papers import Paper
from .common import agent_step, bullet, clip


class LitState(GraphState):
    __reducers__ = {
        "papers": "extend", "notes": "extend", "warnings": "extend",
        "fulltexts": "merge", "lit_ids": "extend",
    }

    question: str = ""
    context_digest: str = ""
    plan: Optional[QueryPlan] = None
    papers: list[Paper] = Field(default_factory=list)
    triage: Optional[Triage] = None
    fulltexts: dict[str, str] = Field(default_factory=dict)
    notes: list[PaperNote] = Field(default_factory=list)
    synthesis: Optional[Synthesis] = None
    critique: Optional[Critique] = None
    revision_round: int = 0
    warnings: list[str] = Field(default_factory=list)
    lit_ids: list[str] = Field(default_factory=list)
    review_id: str = ""

    def kept_papers(self) -> list[Paper]:
        if not self.triage:
            return []
        by_key = {p.key: p for p in self.papers}
        return [by_key[k] for k in self.triage.keep if k in by_key]


# -- tool nodes --------------------------------------------------------------

async def pull_context(state: LitState, ctx: Context) -> dict:
    return {"context_digest": clip(ctx.notebook.digest(), 4000)}


async def search(state: LitState, ctx: Context) -> dict:
    cfg = ctx.config.search
    assert state.plan is not None
    tasks = [(source, q) for source in cfg.sources for q in state.plan.queries]

    async def worker(task: tuple[str, str]):
        source, q = task
        fn = papers_mod.SEARCHERS[source]
        return await asyncio.to_thread(fn, q, cfg.max_results_per_source)

    results = await parallel_map(tasks, worker, limit=6)
    batches, errors = split_errors(results)
    merged = papers_mod.merge_dedupe([b for b in batches if b])[:60]
    warnings = [f"search failed: {e.item} -> {e.error}" for e in errors]
    return {
        "papers": merged,
        "warnings": warnings,
        "run_notes": [f"search: {len(merged)} unique papers from {len(tasks)} queries "
                      f"({len(errors)} source failures)"],
    }


async def deep_read(state: LitState, ctx: Context) -> dict:
    kept = state.kept_papers()[: ctx.config.search.top_k_deep_read]
    arxiv_like = [p for p in kept if p.source == "arxiv" or
                  (p.ext_id and p.ext_id.replace(".", "").replace("v", "")[:4].isdigit())]

    async def worker(paper: Paper):
        text = await asyncio.to_thread(papers_mod.fetch_paper_text, paper.ext_id)
        return (paper.key, text)

    results = await parallel_map(arxiv_like, worker, limit=3)
    ok, errors = split_errors(results)
    fulltexts = {key: text for key, text in ok if text}
    return {
        "fulltexts": fulltexts,
        "warnings": [f"deep_read failed: {e.error}" for e in errors],
        "run_notes": [f"deep_read: full text for {len(fulltexts)}/{len(arxiv_like)} papers"],
    }


async def annotate(state: LitState, ctx: Context) -> dict:
    """Map node: one structured PaperNote per kept paper, bounded concurrency."""
    from ..agents.personas import PERSONAS

    kept = state.kept_papers()[: ctx.config.search.top_k_notes]

    async def worker(paper: Paper):
        fulltext = state.fulltexts.get(paper.key, "")
        prompt = (
            f"Research question: {state.question}\n\n"
            f"Write a structured note for this paper.\n\n"
            f"Title: {paper.title}\nAuthors: {', '.join(paper.authors)}\n"
            f"Year: {paper.year}  Citations: {paper.citations}\nURL: {paper.url}\n"
            f"key (use verbatim in the `key` field): {paper.key}\n\n"
            f"Abstract:\n{paper.abstract}\n\n"
            + (f"Full text (truncated):\n{clip(fulltext, 12000)}\n\n" if fulltext else "")
            + "Evidence must quote actual numbers when present. Relevance must be "
              "specific to OUR question, not generic praise."
        )
        result = await ctx.runtime.run(
            persona=PERSONAS["rs"], prompt=prompt, schema=PaperNote,
            node=f"annotate:{paper.key[:24]}", use_lab_tools=False,
        )
        note: PaperNote = result.data
        note.key = paper.key  # contract: key binds note to paper
        return (note, result.cost)

    results = await parallel_map(kept, worker, limit=3)
    ok, errors = split_errors(results)
    notes = [n for n, _ in ok]
    costs = {f"annotate:{n.key[:24]}": c for n, c in ok}
    return {
        "notes": notes,
        "costs": costs,
        "warnings": [f"annotate failed: {e.error}" for e in errors],
        "run_notes": [f"annotate: {len(notes)} structured notes"],
    }


async def persist(state: LitState, ctx: Context) -> dict:
    assert state.synthesis is not None
    by_key = {p.key: p for p in state.papers}
    lit_ids: list[str] = []
    key_to_lit: dict[str, str] = {}
    for note in state.notes:
        paper = by_key.get(note.key)
        lit_id, _ = ctx.notebook.create("literature", note.title, {
            "source": paper.source if paper else "",
            "url": paper.url if paper else "",
            "year": str(paper.year or "") if paper else "",
            "authors": ", ".join(paper.authors) if paper else "",
            "tags": ", ".join(note.tags),
            "claim": note.claim,
            "method": note.method,
            "evidence": note.evidence,
            "relevance": note.relevance,
            "limitations": note.limitations,
            "quotes": note.quotes or "_",
        })
        lit_ids.append(lit_id)
        key_to_lit[note.key] = lit_id
        ctx.kg.add_node(lit_id, "paper", note.title,
                        url=paper.url if paper else "", key=note.key)

    themes_md = "\n".join(
        f"### {t.name}\n\n{t.summary}\n\nPapers: "
        + ", ".join(f"[[{key_to_lit[k]}]]" for k in t.papers if k in key_to_lit)
        for t in state.synthesis.themes
    )
    index_md = "\n".join(
        f"- [[{key_to_lit[n.key]}]] {n.title}" for n in state.notes if n.key in key_to_lit
    )
    body = (
        f"## Question\n\n{state.question}\n\n"
        f"## Synthesis\n\n{state.synthesis.summary_md}\n\n"
        f"## Themes\n\n{themes_md}\n\n"
        f"## Gaps\n\n{bullet(state.synthesis.gaps)}\n\n"
        f"## Implications for our work\n\n{state.synthesis.implications}\n\n"
        f"## Papers reviewed\n\n{index_md}\n"
    )
    rep_id, _ = ctx.notebook.create(
        "report", f"Literature review: {state.question[:70]}",
        {"body": body, "experiment": "_", "links": " ".join(f"[[{i}]]" for i in lit_ids)},
    )
    ctx.kg.add_node(rep_id, "report", f"Literature review: {state.question[:70]}")
    for lit_id in lit_ids:
        ctx.kg.add_edge(rep_id, lit_id, "cites")
    return {
        "lit_ids": lit_ids,
        "review_id": rep_id,
        "run_notes": [f"persist: {len(lit_ids)} literature notes + review {rep_id}"],
    }


# -- agent nodes -------------------------------------------------------------

def _plan_prompt(state: LitState, ctx: Context) -> str:
    return (
        f"Research question: {state.question}\n\n"
        f"Lab context:\n{state.context_digest}\n\n"
        "Plan a literature search. Produce 3-6 query strings that attack the "
        "question from genuinely different angles (method names, problem "
        "phrasings, adjacent fields, known competing approaches), plus "
        "inclusion/exclusion criteria strict enough to triage by."
    )


def _triage_prompt(state: LitState, ctx: Context) -> str:
    assert state.plan is not None
    briefs = "\n".join(p.brief() for p in state.papers[:60])
    return (
        f"Research question: {state.question}\n\n"
        f"Inclusion criteria:\n{bullet(state.plan.inclusion_criteria)}\n"
        f"Exclusion criteria:\n{bullet(state.plan.exclusion_criteria)}\n\n"
        f"Candidates (format: [key] title (year, citations) — abstract):\n\n{briefs}\n\n"
        f"Keep at most {ctx.config.search.top_k_notes}, ranked most relevant "
        "first, returning their [key] values verbatim. Prefer strongest "
        "evidence and OPPOSING findings over redundant agreement."
    )


def _synthesis_prompt(state: LitState, ctx: Context) -> str:
    notes_md = "\n\n".join(
        f"[{n.key}] {n.title}\nClaim: {n.claim}\nEvidence: {n.evidence}\n"
        f"Relevance: {n.relevance}\nLimitations: {n.limitations}"
        for n in state.notes
    )
    revision = ""
    if state.critique and state.critique.verdict == "revise":
        revision = (
            f"\n\nThis is revision round {state.revision_round}. Your previous "
            f"draft was rejected for:\n{bullet(state.critique.issues)}\n"
            f"Revision instructions: {state.critique.instructions}\n"
        )
    return (
        f"Research question: {state.question}\n\n"
        f"Structured notes on {len(state.notes)} papers:\n\n{notes_md}\n"
        f"{revision}\n"
        "Synthesize: themes with their supporting papers (cite by [key]); gaps "
        "the literature leaves open; implications for our question. summary_md "
        "is the full review body — every claim cites a [key], opposing "
        "evidence is engaged, no filler."
    )


def _critique_prompt(state: LitState, ctx: Context) -> str:
    assert state.synthesis is not None
    return (
        "Adversarially review this literature synthesis you drafted. Rubric:\n"
        "1. Coverage: are opposing/contradicting findings engaged, not buried?\n"
        "2. Claim-evidence linkage: does every claim cite a [key] that supports it?\n"
        "3. Gaps: are the gaps real open questions, or padding?\n"
        "4. Terseness: any filler to cut?\n\n"
        f"Synthesis:\n\n{clip(state.synthesis.summary_md, 8000)}\n\n"
        f"Themes: {', '.join(t.name for t in state.synthesis.themes)}\n"
        f"Gaps:\n{bullet(state.synthesis.gaps)}\n\n"
        "verdict=revise only for substantive failures; give concrete instructions."
    )


def route_critique(state: LitState) -> str:
    if state.critique and state.critique.verdict == "revise" and state.revision_round <= 2:
        return "revise"
    return "accept"


def build_literature() -> Graph:
    g = Graph("literature", LitState,
              doc="RS literature review: plan -> search -> triage -> deep-read -> annotate -> synthesize -> critique loop -> persist")
    g.tool("pull_context", pull_context)
    g.add("plan_queries", agent_step(
        "plan_queries", "rs", _plan_prompt, QueryPlan,
        lambda data, s, c: {"plan": data}, use_lab_tools=False), kind="agent", retries=1)
    g.tool("search", search, retries=1)
    g.add("triage", agent_step(
        "triage", "rs", _triage_prompt, Triage,
        lambda data, s, c: {"triage": data}, use_lab_tools=False), kind="agent", retries=1)
    g.tool("deep_read", deep_read)
    g.tool("annotate", annotate)
    g.add("synthesize", agent_step(
        "synthesize", "rs", _synthesis_prompt, Synthesis,
        lambda data, s, c: {"synthesis": data}, use_lab_tools=False),
        kind="agent", retries=1, max_visits=4)
    g.add("self_critique", agent_step(
        "self_critique", "rs", _critique_prompt, Critique,
        lambda data, s, c: {"critique": data, "revision_round": s.revision_round + 1},
        use_lab_tools=False), kind="agent", retries=1, max_visits=4)
    g.tool("persist", persist)

    g.edge(START, "pull_context")
    g.chain("pull_context", "plan_queries", "search", "triage", "deep_read",
            "annotate", "synthesize", "self_critique")
    g.branch("self_critique", route_critique, {"revise": "synthesize", "accept": "persist"})
    g.edge("persist", END)
    return g
