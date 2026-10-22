"""Tech Lead workflow: execution.

load the ticket plan -> human go/no-go -> per dependency wave: dispatch
parallel Developers (git-worktree isolation when available) -> TL review
each hand-off -> bounded fix rounds -> integrate (merge + full test suite)
-> next wave -> integration report.

Developers run as nested developer-graph runs; the Tech Lead reviews real
diffs, and the engine — not any agent — runs the test suite.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Optional

from pydantic import Field

from ..agents.personas import PERSONAS
from ..agents.schemas import IntegrationNote, ReviewVerdict
from ..graph import Context, END, Graph, GraphState, START, parallel_map, split_errors
from ..tools import repo as repo_mod
from .common import agent_step, bullet, clip
from .developer import DevState, build_developer


class ExecState(GraphState):
    __reducers__ = {
        "dev_results": "merge", "review_verdicts": "merge",
        "fix_rounds": "merge", "integration_log": "extend",
    }

    exp_id: str = ""
    scope: list[str] = Field(default_factory=list)
    scope_titles: dict[str, str] = Field(default_factory=dict)
    waves: list[list[str]] = Field(default_factory=list)
    wave_index: int = 0
    use_git: bool = False
    base_branch: str = ""
    test_command: str = ""
    dev_concurrency: int = 2
    max_fix_rounds: int = 2
    dev_results: dict[str, Any] = Field(default_factory=dict)
    review_verdicts: dict[str, Any] = Field(default_factory=dict)
    fix_rounds: dict[str, int] = Field(default_factory=dict)
    human_decision: str = ""
    integration_log: list[str] = Field(default_factory=list)
    report: Optional[IntegrationNote] = None

    def wave(self) -> list[str]:
        if 0 <= self.wave_index < len(self.waves):
            return self.waves[self.wave_index]
        return []


# -- helpers -----------------------------------------------------------------

def _deps_from_frontmatter(fm_value: str) -> list[str]:
    return [d.strip() for d in (fm_value or "").split(",")
            if d.strip().startswith("TCK")]


def _ensure_worktree(root: Path, branch: str) -> Path:
    slug = branch.replace("/", "-")
    path = root / ".triad" / "worktrees" / slug
    if path.is_dir():
        return path
    return repo_mod.add_worktree(root, branch)


async def _run_dev(ctx: Context, state: ExecState, tck: str, mode: str,
                   comments: list[str]) -> tuple[str, dict]:
    from ..graph import Runner  # local import avoids cycles

    branch, workdir = "", str(ctx.root)
    if state.use_git:
        branch = f"tck/{tck.lower()}"
        workdir = str(await asyncio.to_thread(_ensure_worktree, ctx.root, branch))
    dev_state = DevState(
        ticket_id=tck, mode=mode, review_comments=comments, workdir=workdir,
        branch=branch, test_command=state.test_command,
        max_debug=ctx.config.autonomy.max_debug_rounds,
    )
    parent = ctx.extras.get("current_run_id")
    run_id = ctx.store.new_run_id(f"dev-{tck.lower()}-{mode}")
    rr = await Runner(ctx).run(build_developer(), dev_state,
                               run_id=run_id, parent=parent, max_steps=60)
    final: DevState = rr.state  # type: ignore[assignment]
    res = final.result
    return tck, {
        "status": res.status if res else "blocked",
        "summary_md": res.summary_md if res else "(developer produced no result)",
        "files_changed": res.files_changed if res else [],
        "question": (res.question if res else "developer run ended without a result"),
        "tests_passed": final.tests_passed,
        "branch": branch,
        "run_id": rr.run_id,
        "cost": rr.cost_usd,
        "mode": mode,
    }


# -- tool nodes --------------------------------------------------------------

async def load_plan(state: ExecState, ctx: Context) -> dict:
    entries = ctx.notebook.entries("ticket")
    scope: list[str] = []
    titles: dict[str, str] = {}
    deps: dict[str, list[str]] = {}
    for entry in entries:
        fm = ctx.notebook.frontmatter(entry["id"])
        if state.exp_id and fm.get("experiment", "") != state.exp_id:
            continue
        if state.scope and entry["id"] not in state.scope:
            continue
        if fm.get("status", "todo") not in ("todo", "changes_requested", "blocked"):
            continue
        scope.append(entry["id"])
        titles[entry["id"]] = entry["title"]
        deps[entry["id"]] = [d for d in _deps_from_frontmatter(fm.get("depends_on", ""))]
    from .planning import waves_of
    waves = waves_of({k: [d for d in v if d in deps] for k, v in deps.items()})
    use_git = repo_mod.is_git_repo(ctx.root)
    base = repo_mod.current_branch(ctx.root) if use_git else ""
    test_command = state.test_command or repo_mod.detect_test_command(ctx.root)
    return {
        "scope": scope, "scope_titles": titles, "waves": waves,
        "use_git": use_git, "base_branch": base, "test_command": test_command,
        "dev_concurrency": ctx.config.autonomy.max_dev_concurrency,
        "max_fix_rounds": ctx.config.autonomy.max_review_rounds,
        "run_notes": [f"load_plan: {len(scope)} tickets in {len(waves)} wave(s); "
                      f"git={'yes' if use_git else 'no'}"],
    }


async def dispatch_wave(state: ExecState, ctx: Context) -> dict:
    wave = [t for t in state.wave()
            if state.dev_results.get(t, {}).get("status") != "done"]
    if not wave:
        return {"run_notes": [f"dispatch_wave: wave {state.wave_index} empty"]}
    limit = state.dev_concurrency if state.use_git else 1

    async def worker(tck: str):
        return await _run_dev(ctx, state, tck, "build", [])

    results = await parallel_map(wave, worker, limit=limit)
    ok, errors = split_errors(results)
    dev_results = {tck: payload for tck, payload in ok}
    for err in errors:
        tck = str(err.item).strip("'\"")
        dev_results[tck] = {"status": "blocked", "summary_md": "(developer run crashed)",
                            "files_changed": [], "question": err.error,
                            "tests_passed": False, "branch": "", "run_id": "", "cost": 0.0,
                            "mode": "build"}
    costs = {f"dev:{tck}": p.get("cost", 0.0) for tck, p in dev_results.items()}
    return {
        "dev_results": dev_results, "costs": costs,
        "run_notes": [f"dispatch_wave {state.wave_index}: {len(dev_results)} developer run(s), "
                      f"concurrency {limit}"],
    }


async def review_wave(state: ExecState, ctx: Context) -> dict:
    wave = [t for t in state.wave() if t in state.dev_results]
    todo = [t for t in wave
            if state.review_verdicts.get(t, {}).get("verdict") != "approve"]
    if not todo:
        return {"run_notes": ["review_wave: nothing awaiting review"]}

    async def worker(tck: str):
        dev = state.dev_results[tck]
        try:
            ticket_text = ctx.notebook.read(tck)
        except FileNotFoundError:
            ticket_text = f"(ticket {tck} not found)"
        if state.use_git and dev.get("branch"):
            diff = await asyncio.to_thread(
                repo_mod.diff_full, ctx.root,
                f"{state.base_branch}...{dev['branch']}")
            diff = diff or "(empty diff — nothing committed?)"
        else:
            diff = "(no git diff available) files changed:\n" + bullet(dev.get("files_changed", []))
        blocked = ""
        if dev.get("status") == "blocked":
            blocked = (f"\nThe Developer is BLOCKED with this question:\n"
                       f"  {dev.get('question')}\n"
                       "Answer it concretely in your comments; verdict must be "
                       "request_changes so a fix round can act on your answer.\n")
        prompt = (
            f"Code review of {tck} ({state.scope_titles.get(tck, '')}).\n\n"
            f"Ticket:\n{clip(ticket_text, 4000)}\n\n"
            f"Developer hand-off:\n{clip(dev.get('summary_md', ''), 2500)}\n"
            f"Tests passed: {dev.get('tests_passed')}\n{blocked}\n"
            f"Diff:\n{clip(diff, 14000)}\n\n"
            "Rubric in order: correctness; tests actually test the acceptance "
            "criteria; scope discipline (nothing unrelated changed); clarity; "
            "no silent behavior changes. blocking lists only must-fix issues."
        )
        result = await ctx.runtime.run(
            persona=PERSONAS["tl"], prompt=prompt, schema=ReviewVerdict,
            node=f"review:{tck}", use_lab_tools=False)
        return tck, result.data, result.cost

    results = await parallel_map(todo, worker, limit=2)
    ok, errors = split_errors(results)
    verdicts = {tck: v.model_dump() for tck, v, _ in ok}
    for err in errors:
        tck = str(err.item).strip("'\"")
        verdicts[tck] = {"verdict": "request_changes",
                         "comments": [f"review crashed: {err.error}"],
                         "blocking": ["review did not complete"]}
    costs = {f"review:{tck}": c for tck, _, c in ok}
    return {"review_verdicts": verdicts, "costs": costs,
            "run_notes": [f"review_wave: {len(verdicts)} verdict(s)"]}


async def fix_wave(state: ExecState, ctx: Context) -> dict:
    flagged = [t for t in state.wave()
               if state.review_verdicts.get(t, {}).get("verdict") == "request_changes"
               and state.fix_rounds.get(t, 0) < state.max_fix_rounds]
    if not flagged:
        return {"run_notes": ["fix_wave: nothing to fix"]}
    limit = state.dev_concurrency if state.use_git else 1

    async def worker(tck: str):
        verdict = state.review_verdicts.get(tck, {})
        comments = list(verdict.get("blocking", [])) + list(verdict.get("comments", []))
        return await _run_dev(ctx, state, tck, "fix", comments)

    results = await parallel_map(flagged, worker, limit=limit)
    ok, errors = split_errors(results)
    dev_results = {tck: payload for tck, payload in ok}
    for err in errors:
        tck = str(err.item).strip("'\"")
        dev_results[tck] = {**state.dev_results.get(tck, {}),
                            "status": "blocked", "question": err.error}
    fix_rounds = {tck: state.fix_rounds.get(tck, 0) + 1 for tck in flagged}
    costs = {f"fix:{tck}": p.get("cost", 0.0) for tck, p in dev_results.items()}
    return {"dev_results": dev_results, "fix_rounds": fix_rounds, "costs": costs,
            "run_notes": [f"fix_wave: {len(flagged)} fix round(s)"]}


async def integrate(state: ExecState, ctx: Context) -> dict:
    log: list[str] = []
    merged_any = False
    for tck in state.wave():
        verdict = state.review_verdicts.get(tck, {})
        dev = state.dev_results.get(tck, {})
        approved = verdict.get("verdict") == "approve"
        branch = dev.get("branch", "")
        if approved:
            if state.use_git and branch:
                code, out = await asyncio.to_thread(
                    repo_mod.merge_branch, ctx.root, branch, f"merge {tck} [triad]")
                if code == 0:
                    await asyncio.to_thread(repo_mod.remove_worktree, ctx.root, branch)
                    log.append(f"{tck}: merged {branch}")
                    merged_any = True
                else:
                    log.append(f"{tck}: MERGE FAILED — {clip(out, 400)}; "
                               f"branch {branch} left for manual resolution")
                    continue
            else:
                log.append(f"{tck}: approved (no git merge needed)")
                merged_any = True
            try:
                ctx.notebook.set_frontmatter(tck, status="done")
                ctx.notebook.append(tck, "Integrated",
                                    "Approved by Tech Lead. " +
                                    "; ".join(verdict.get("comments", [])[:3]))
            except FileNotFoundError:
                pass
        else:
            if state.use_git and branch:
                await asyncio.to_thread(repo_mod.remove_worktree, ctx.root, branch)
                log.append(f"{tck}: NOT approved after {state.fix_rounds.get(tck, 0)} "
                           f"fix round(s); branch {branch} kept, worktree removed")
            else:
                log.append(f"{tck}: NOT approved after fix rounds")
            try:
                ctx.notebook.set_frontmatter(tck, status="changes_requested")
                ctx.notebook.append(
                    tck, "Escalation",
                    "Unresolved after fix rounds. Blocking:\n"
                    + bullet(verdict.get("blocking", ["unknown"])))
            except FileNotFoundError:
                pass
    if merged_any:
        passed, output = await asyncio.to_thread(
            repo_mod.run_tests, ctx.root, state.test_command)
        log.append(f"post-integration test suite: {'PASS' if passed else 'FAIL'}")
        if not passed:
            log.append("test tail: " + clip(output[-1200:], 1200))
    return {"wave_index": state.wave_index + 1, "integration_log": log,
            "run_notes": [f"integrate: wave {state.wave_index} done"]}


async def persist_exec(state: ExecState, ctx: Context) -> dict:
    note = state.report
    body = (note.summary_md if note else "(no report)") + "\n\nIntegration log:\n" + bullet(
        state.integration_log)
    if state.exp_id:
        try:
            ctx.notebook.append(state.exp_id, "Implementation", body)
            done = [t for t in state.scope
                    if state.review_verdicts.get(t, {}).get("verdict") == "approve"]
            if done:
                ctx.notebook.set_frontmatter(state.exp_id, status="running")
        except FileNotFoundError:
            pass
    ctx.notebook.rebuild_index("ticket")
    return {"run_notes": ["persist_exec: report recorded"]}


# -- agent nodes -------------------------------------------------------------

def _report_prompt(state: ExecState, ctx: Context) -> str:
    lines = []
    for tck in state.scope:
        verdict = state.review_verdicts.get(tck, {}).get("verdict", "not_reviewed")
        dev = state.dev_results.get(tck, {})
        lines.append(f"- {tck} {state.scope_titles.get(tck, '')}: dev={dev.get('status', '—')}, "
                     f"tests={dev.get('tests_passed', '—')}, review={verdict}, "
                     f"fix_rounds={state.fix_rounds.get(tck, 0)}")
    return (
        f"Write the integration report for this execution round"
        f"{' of ' + state.exp_id if state.exp_id else ''}.\n\n"
        f"Tickets:\n" + "\n".join(lines or ["- (none)"]) + "\n\n"
        f"Integration log:\n{bullet(state.integration_log)}\n\n"
        "summary_md: what shipped, what did not and why, in what state the "
        "repo was left. risks: anything integrated that needs watching. "
        "followups: concrete next actions for unresolved tickets."
    )


def _gate_message(state: ExecState) -> str:
    lines = [f"Execute {len(state.scope)} ticket(s) in {len(state.waves)} wave(s)?"]
    for i, wave in enumerate(state.waves):
        for t in wave:
            lines.append(f"  wave {i}: {t} — {state.scope_titles.get(t, '')}")
    lines.append(f"git isolation: {'worktrees' if state.use_git else 'NO GIT — sequential in-tree'}")
    lines.append("Answer yes to proceed; anything else aborts with your note recorded.")
    return "\n".join(lines)


def route_gate(state: ExecState) -> str:
    return "go" if state.human_decision.strip().lower().startswith("y") else "abort"


def route_review(state: ExecState) -> str:
    for tck in state.wave():
        verdict = state.review_verdicts.get(tck, {}).get("verdict")
        if verdict == "request_changes" and state.fix_rounds.get(tck, 0) < state.max_fix_rounds:
            return "fix"
    return "integrate"


def route_waves(state: ExecState) -> str:
    return "next" if state.wave_index < len(state.waves) else "report"


async def abort_note(state: ExecState, ctx: Context) -> dict:
    return {"run_notes": [f"execution aborted at gate: {state.human_decision!r}"]}


def build_execution() -> Graph:
    g = Graph("execution", ExecState,
              doc="TL execution: waves of parallel Developers in worktrees, review + fix loops, merge + test, report")
    g.tool("load_plan", load_plan)
    g.gate("approve_execution", _gate_message, field_name="human_decision")
    g.task("abort", abort_note)
    g.tool("dispatch_wave", dispatch_wave, max_visits=8)
    g.tool("review_wave", review_wave, max_visits=24)
    g.tool("fix_wave", fix_wave, max_visits=16)
    g.tool("integrate", integrate, max_visits=8)
    g.add("report", agent_step(
        "report", "tl", _report_prompt, IntegrationNote,
        lambda data, s, c: {"report": data}, use_lab_tools=False),
        kind="agent", retries=1)
    g.tool("persist_exec", persist_exec)

    g.edge(START, "load_plan")
    g.edge("load_plan", "approve_execution")
    g.branch("approve_execution", route_gate, {"go": "dispatch_wave", "abort": "abort"})
    g.edge("abort", END)
    g.edge("dispatch_wave", "review_wave")
    g.branch("review_wave", route_review, {"fix": "fix_wave", "integrate": "integrate"})
    g.edge("fix_wave", "review_wave")
    g.branch("integrate", route_waves, {"next": "dispatch_wave", "report": "report"})
    g.edge("report", "persist_exec")
    g.edge("persist_exec", END)
    return g
