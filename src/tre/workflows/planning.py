"""Tech Lead workflow: planning.

repo recon -> architecture decision record -> decomposition into a ticket
DAG with acceptance criteria and test plans -> persist tickets, compute
dependency waves, record knowledge-graph lineage.
"""

from __future__ import annotations

import asyncio
from typing import Optional

from pydantic import Field

from ..agents.schemas import ADRDoc, TicketSet
from ..graph import Context, END, Graph, GraphState, START
from ..tools import repo as repo_mod
from .common import agent_step, bullet, clip


class PlanState(GraphState):
    __reducers__ = {"ticket_ids": "extend"}

    objective: str = ""
    exp_id: str = ""
    recon: str = ""
    adr: Optional[ADRDoc] = None
    tickets: Optional[TicketSet] = None
    adr_id: str = ""
    ticket_ids: list[str] = Field(default_factory=list)
    waves: list[list[str]] = Field(default_factory=list)


def waves_of(deps: dict[str, list[str]]) -> list[list[str]]:
    """Kahn layering: tickets grouped into waves runnable in parallel."""
    remaining = {k: set(v) for k, v in deps.items()}
    waves: list[list[str]] = []
    while remaining:
        ready = sorted(k for k, d in remaining.items() if not (d & set(remaining)))
        if not ready:  # cycle: dump the rest as a final sequential wave
            waves.extend([[k] for k in sorted(remaining)])
            break
        waves.append(ready)
        for k in ready:
            remaining.pop(k)
    return waves


# -- tool nodes --------------------------------------------------------------

async def recon(state: PlanState, ctx: Context) -> dict:
    tree = await asyncio.to_thread(repo_mod.repo_map, ctx.root)
    keys = await asyncio.to_thread(repo_mod.key_files_excerpt, ctx.root)
    prereg = ""
    if state.exp_id:
        try:
            prereg = ctx.notebook.read(state.exp_id)
        except FileNotFoundError:
            prereg = ""
    recon_text = (
        f"## Repository tree\n{clip(tree, 6000)}\n\n"
        f"## Key files\n{clip(keys, 5000)}\n\n"
        + (f"## Pre-registration under implementation\n{clip(prereg, 6000)}\n" if prereg else "")
    )
    return {"recon": recon_text, "run_notes": ["recon: repo mapped"]}


async def persist_plan(state: PlanState, ctx: Context) -> dict:
    adr, tickets = state.adr, state.tickets
    assert adr is not None and tickets is not None
    adr_id, _ = ctx.notebook.create("adr", adr.title, {
        "context": adr.context, "decision": adr.decision,
        "alternatives": adr.alternatives, "consequences": adr.consequences,
        "links": f"[[{state.exp_id}]]" if state.exp_id else "_",
    })
    ctx.notebook.set_frontmatter(adr_id, status="accepted")

    index_to_id: dict[int, str] = {}
    ticket_ids: list[str] = []
    for i, ticket in enumerate(tickets.tickets):
        tck_id, _ = ctx.notebook.create("ticket", ticket.title, {
            "experiment": state.exp_id or "_",
            "goal": ticket.goal,
            "acceptance": bullet(ticket.acceptance),
            "test_plan": ticket.test_plan,
            "files": bullet(ticket.files),
            "out_of_scope": ticket.out_of_scope or "_",
            "links": f"[[{adr_id}]]" + (f" [[{state.exp_id}]]" if state.exp_id else ""),
            "activity": "- created by Tech Lead planning workflow",
        })
        index_to_id[i] = tck_id
        ticket_ids.append(tck_id)
    # second pass: dependencies (needs the full index->id map)
    deps: dict[str, list[str]] = {}
    for i, ticket in enumerate(tickets.tickets):
        dep_ids = [index_to_id[j] for j in ticket.depends_on if j in index_to_id and j != i]
        deps[index_to_id[i]] = dep_ids
        ctx.notebook.set_frontmatter(
            index_to_id[i],
            depends_on=", ".join(dep_ids) if dep_ids else "_",
            branch=f"tck/{index_to_id[i].lower()}",
        )
    waves = waves_of(deps)

    ctx.kg.add_node(adr_id, "adr", adr.title)
    if state.exp_id:
        ctx.kg.add_edge(adr_id, state.exp_id, "informs")
    for tck_id in ticket_ids:
        title = next(t.title for i, t in enumerate(tickets.tickets) if index_to_id[i] == tck_id)
        ctx.kg.add_node(tck_id, "ticket", title)
        if state.exp_id:
            ctx.kg.add_edge(tck_id, state.exp_id, "implements")
    for tck_id, dep_ids in deps.items():
        for dep in dep_ids:
            ctx.kg.add_edge(tck_id, dep, "depends_on")

    return {
        "adr_id": adr_id, "ticket_ids": ticket_ids, "waves": waves,
        "run_notes": [f"persist_plan: {adr_id}, {len(ticket_ids)} tickets in "
                      f"{len(waves)} wave(s)"],
    }


# -- agent nodes -------------------------------------------------------------

def _adr_prompt(state: PlanState, ctx: Context) -> str:
    return (
        f"Objective: {state.objective}\n\n"
        f"{state.recon}\n\n"
        "Write the architecture decision record for how this repository will "
        "implement the objective: where code lives, what gets reused vs "
        "added, config/seed/logging strategy for reproducibility, how results "
        "land in docs/experiments/runs/<EXP-ID>/ as JSON/CSV. Decisions are "
        "concrete (paths, module names); alternatives are real options you "
        "rejected with reasons."
    )


def _decompose_prompt(state: PlanState, ctx: Context) -> str:
    adr = state.adr
    assert adr is not None
    return (
        f"Objective: {state.objective}\n\n"
        f"Accepted ADR:\nDecision: {adr.decision}\nConsequences: {adr.consequences}\n\n"
        f"{clip(state.recon, 6000)}\n\n"
        "Decompose into 2-6 tickets a Developer can each finish without asking "
        "questions. Every ticket: crisp goal, VERIFIABLE acceptance criteria, "
        "a test plan naming the tests to write first, likely files, explicit "
        "out-of-scope. depends_on uses indexes into this ticket list, only "
        "where a true build-order dependency exists — independent tickets run "
        "in parallel. Results-producing code must write metrics to "
        "docs/experiments/runs/<EXP-ID>/ per the ADR."
    )


def build_planning() -> Graph:
    g = Graph("planning", PlanState,
              doc="TL planning: recon -> ADR -> ticket decomposition -> persist + dependency waves")
    g.tool("recon", recon)
    g.add("draft_adr", agent_step(
        "draft_adr", "tl", _adr_prompt, ADRDoc,
        lambda data, s, c: {"adr": data}), kind="agent", retries=1)
    g.add("decompose", agent_step(
        "decompose", "tl", _decompose_prompt, TicketSet,
        lambda data, s, c: {"tickets": data}, use_lab_tools=False),
        kind="agent", retries=1)
    g.tool("persist_plan", persist_plan)

    g.edge(START, "recon")
    g.chain("recon", "draft_adr", "decompose", "persist_plan")
    g.edge("persist_plan", END)
    return g
