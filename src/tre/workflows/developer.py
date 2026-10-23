"""Developer workflow: implement one ticket, test-driven.

read ticket -> gather codebase context -> plan -> implement (tests first)
-> run tests (ground truth, engine-executed) -> bounded debug loop ->
structured self-review -> commit + hand off for review.

The engine, not the agent, runs the test suite: a Developer can claim
anything; `run_tests` measures.
"""

from __future__ import annotations

import asyncio
from typing import Optional

from pydantic import Field

from ..agents.schemas import ContextDigest, DevPlan, DevResult
from ..graph import Context, END, Graph, GraphState, START
from ..tools import repo as repo_mod
from .common import agent_step, bullet, clip


class DevState(GraphState):
    ticket_id: str = ""
    mode: str = "build"                     # build | fix
    review_comments: list[str] = Field(default_factory=list)
    workdir: str = ""
    branch: str = ""
    test_command: str = ""
    ticket_text: str = ""
    context: Optional[ContextDigest] = None
    plan: Optional[DevPlan] = None
    implement_log: str = ""
    test_output: str = ""
    tests_passed: bool = False
    debug_round: int = 0
    max_debug: int = 3
    result: Optional[DevResult] = None


# -- tool nodes --------------------------------------------------------------

async def read_ticket(state: DevState, ctx: Context) -> dict:
    text = ctx.notebook.read(state.ticket_id)
    try:
        ctx.notebook.set_frontmatter(state.ticket_id, status="in_progress")
    except FileNotFoundError:
        pass
    workdir = state.workdir or str(ctx.root)
    test_command = state.test_command or repo_mod.detect_test_command(ctx.root)
    return {"ticket_text": text, "workdir": workdir, "test_command": test_command,
            "run_notes": [f"read_ticket: {state.ticket_id} (mode={state.mode})"]}


async def run_tests(state: DevState, ctx: Context) -> dict:
    passed, output = await asyncio.to_thread(
        repo_mod.run_tests, state.workdir, state.test_command)
    return {"tests_passed": passed, "test_output": output,
            "run_notes": [f"run_tests: {'PASS' if passed else 'FAIL'} ({state.test_command})"]}


async def finalize(state: DevState, ctx: Context) -> dict:
    result = state.result
    assert result is not None
    from pathlib import Path
    workdir = Path(state.workdir)
    committed = ""
    if state.branch and repo_mod.is_git_repo(workdir):
        title = state.ticket_id
        code, out = await asyncio.to_thread(
            repo_mod.commit_all, workdir, f"{state.ticket_id}: {result.status} [tre-dev]")
        committed = f"commit exit {code}"
    status = "in_review" if result.status == "done" else "blocked"
    try:
        ctx.notebook.set_frontmatter(state.ticket_id, status=status)
        ctx.notebook.append(
            state.ticket_id, f"Developer hand-off ({state.mode})",
            result.summary_md
            + f"\n\nTests: {'PASS' if state.tests_passed else 'FAIL'} (`{state.test_command}`)"
            + (f"\nBlocked on: {result.question}" if result.question else "")
            + (f"\n{committed}" if committed else ""))
    except FileNotFoundError:
        pass
    return {"run_notes": [f"finalize: {state.ticket_id} -> {status}"]}


# -- agent nodes -------------------------------------------------------------

def _context_prompt(state: DevState, ctx: Context) -> str:
    return (
        f"Ticket:\n{clip(state.ticket_text, 5000)}\n\n"
        "Explore this repository (Read/Grep/Glob) to gather exactly the "
        "context needed to implement the ticket: the files you will touch, "
        "the conventions they follow, the integration points, the pitfalls. "
        "Do not modify anything. Report as ContextDigest."
    )


def _plan_prompt(state: DevState, ctx: Context) -> str:
    digest = state.context
    return (
        f"Ticket:\n{clip(state.ticket_text, 5000)}\n\n"
        f"Codebase context:\n"
        f"Files: {', '.join(digest.relevant_files) if digest else '(none)'}\n"
        f"Observations: {digest.observations if digest else '(none)'}\n\n"
        "Plan the implementation. test_names are the tests you will write "
        "FIRST, derived from the ticket's test plan and acceptance criteria. "
        "Steps are concrete edits, not aspirations."
    )


def _implement_prompt(state: DevState, ctx: Context) -> str:
    plan = state.plan
    fix_block = ""
    if state.mode == "fix" and state.review_comments:
        fix_block = (
            "\n\nThis is a FIX round. Address these review comments and "
            f"nothing else:\n{bullet(state.review_comments)}\n"
        )
    return (
        f"Implement this ticket in the current working directory.\n\n"
        f"Ticket:\n{clip(state.ticket_text, 5000)}\n\n"
        f"Your plan:\nApproach: {plan.approach if plan else '(none)'}\n"
        f"Steps:\n{bullet(plan.steps) if plan else '- (none)'}\n"
        f"Tests to write first:\n{bullet(plan.test_names) if plan else '- (none)'}\n"
        f"{fix_block}\n"
        "Strict order of work:\n"
        "1. Write the failing tests from the plan.\n"
        "2. Run them; confirm they fail for the right reason.\n"
        "3. Implement the minimal change that makes them pass.\n"
        "4. Run the full relevant test scope.\n"
        "Minimal diff; repository conventions; no new dependencies unless the "
        "ticket authorizes them; seeds explicit in any stochastic code."
    )


def _debug_prompt(state: DevState, ctx: Context) -> str:
    return (
        f"The test suite failed (round {state.debug_round + 1}/{state.max_debug}).\n\n"
        f"Command: {state.test_command}\n\nOutput:\n{clip(state.test_output, 9000)}\n\n"
        f"Ticket (for scope):\n{clip(state.ticket_text, 2500)}\n\n"
        "Diagnose the actual root cause from the output, then fix it. Fix the "
        "code, not the test — unless the test itself contradicts the ticket's "
        "acceptance criteria, and say so if it does."
    )


def _review_prompt(state: DevState, ctx: Context) -> str:
    diff = repo_mod.diff_full(state.workdir) or "(no unstaged diff; may be committed or empty)"
    return (
        f"Self-review your work on {state.ticket_id} before hand-off.\n\n"
        f"Ticket:\n{clip(state.ticket_text, 3500)}\n\n"
        f"Diff:\n{clip(diff, 12000)}\n\n"
        f"Tests: {'PASS' if state.tests_passed else 'FAIL'}\n"
        f"Last test output (tail):\n{clip(state.test_output[-2500:] if state.test_output else '', 2500)}\n\n"
        "Check acceptance criteria one by one. status=done only if every "
        "criterion is met AND tests pass. Otherwise status=blocked with the "
        "single precise question or blocker. summary_md is your PR "
        "description: what changed, why, how verified."
    )


def route_tests(state: DevState) -> str:
    if state.tests_passed:
        return "review"
    if state.debug_round < state.max_debug:
        return "debug"
    return "review"  # give up gracefully; self_review will report blocked


def build_developer() -> Graph:
    g = Graph("developer", DevState,
              doc="Dev TDD loop: context -> plan -> implement -> test -> debug loop -> self-review -> hand-off")
    g.tool("read_ticket", read_ticket)
    g.add("gather_context", agent_step(
        "gather_context", "dev", _context_prompt, ContextDigest,
        lambda data, s, c: {"context": data},
        tools=["Read", "Grep", "Glob"], use_lab_tools=False, max_turns=15,
        cwd_from=lambda s, c: s.workdir or None), kind="agent", retries=1)
    g.add("plan", agent_step(
        "plan", "dev", _plan_prompt, DevPlan,
        lambda data, s, c: {"plan": data}, use_lab_tools=False), kind="agent", retries=1)
    g.add("implement", agent_step(
        "implement", "dev", _implement_prompt, None,
        lambda text, s, c: {"implement_log": clip(text, 4000)},
        tools=["Read", "Grep", "Glob", "Write", "Edit", "Bash"],
        use_lab_tools=False, max_turns=60,
        cwd_from=lambda s, c: s.workdir or None), kind="agent")
    g.tool("run_tests", run_tests, max_visits=8)
    g.add("debug", agent_step(
        "debug", "dev", _debug_prompt, None,
        lambda text, s, c: {"debug_round": s.debug_round + 1,
                            "implement_log": clip(text, 3000)},
        tools=["Read", "Grep", "Glob", "Write", "Edit", "Bash"],
        use_lab_tools=False, max_turns=40,
        cwd_from=lambda s, c: s.workdir or None), kind="agent", max_visits=5)
    g.add("self_review", agent_step(
        "self_review", "dev", _review_prompt, DevResult,
        lambda data, s, c: {"result": data},
        tools=["Read", "Grep"], use_lab_tools=False, max_turns=10,
        cwd_from=lambda s, c: s.workdir or None), kind="agent", retries=1)
    g.tool("finalize", finalize)

    g.edge(START, "read_ticket")
    g.chain("read_ticket", "gather_context", "plan", "implement", "run_tests")
    g.branch("run_tests", route_tests, {"review": "self_review", "debug": "debug"})
    g.edge("debug", "run_tests")
    g.chain("self_review", "finalize")
    g.edge("finalize", END)
    return g
