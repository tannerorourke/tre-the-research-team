"""The `triad` CLI: init a lab on a repo, chat with the principals, run
workflow graphs with live progress, inspect runs/costs, export graphs."""

from __future__ import annotations

import asyncio
import shlex
import sys
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table

from . import __version__
from .config import STARTER_CONFIG
from .graph import GraphError, GraphInterrupt, Runner, RunResult, run_to_completion
from .lab import build_context
from .workflows import WORKFLOWS

app = typer.Typer(
    help="Triad — a three-agent ML research lab (Research Scientist, Tech Lead, "
         "Developer) that lives on your repository.",
    no_args_is_help=True, add_completion=False,
)
console = Console()

PERSONA_STYLE = {"rs": ("Research Scientist", "cyan"), "tl": ("Tech Lead", "magenta"),
                 "dev": ("Developer", "green")}


class _GateAbort(Exception):
    def __init__(self, run_id: str, gate: str):
        super().__init__(gate)
        self.run_id, self.gate = run_id, gate


def _print_event(event: dict) -> None:
    kind = event.get("type")
    run = str(event.get("run", ""))[-20:]
    if kind == "run_start":
        console.print(f"[bold blue]● run[/] [dim]{run}[/] graph={event.get('graph')}")
    elif kind == "run_resume":
        console.print(f"[bold blue]● resume[/] [dim]{run}[/] at {event.get('at')}")
    elif kind == "node_start":
        console.print(f"[dim]{run}[/] ▶ [bold]{event.get('node')}[/] "
                      f"[dim]({event.get('kind')}, visit {event.get('visit')})[/]")
    elif kind == "node_end":
        console.print(f"[dim]{run}[/]   [green]✓[/] {event.get('node')} "
                      f"[dim]→ {event.get('next')} ({event.get('duration_s')}s)[/]")
    elif kind == "node_error":
        console.print(f"[dim]{run}[/]   [red]✗ {event.get('node')} "
                      f"attempt {event.get('attempt')}: {event.get('error')}[/]")
    elif kind == "agent_call":
        cost = event.get("cost_usd") or 0
        console.print(f"    [dim]🧠 {event.get('persona')}:{event.get('node')} "
                      f"${cost:.3f}[/]")
    elif kind == "gate_wait":
        console.print(f"[yellow]⏸ gate {event.get('node')} — waiting for you[/]")
    elif kind == "run_end":
        status = event.get("status")
        style = "green" if status == "completed" else "red"
        console.print(f"[{style}]■ run {run}: {status}[/] "
                      f"[dim]cost=${event.get('cost_usd', 0) or 0:.2f}[/]")


def _ctx(path: str, *, live: bool = True):
    return build_context(Path(path), on_event=_print_event if live else None)


async def _ask_gate(gi: GraphInterrupt) -> str:
    console.print(Panel(gi.message, title=f"gate: {gi.gate}", border_style="yellow"))
    try:
        answer = await asyncio.to_thread(console.input, "[bold yellow]answer ▸ [/]")
    except (EOFError, KeyboardInterrupt):
        raise _GateAbort(gi.run_id, gi.gate) from None
    return answer.strip()


def _summarize(result: RunResult) -> None:
    lines = [f"status: [bold]{result.status}[/]   steps: {result.steps}   "
             f"cost: ${result.cost_usd:.2f}   duration: {result.duration_s}s",
             f"run id: {result.run_id}"]
    state = result.state
    artifacts = []
    for name in type(state).model_fields:
        if name.endswith("_id"):
            value = getattr(state, name, "")
            if value:
                artifacts.append(f"{name}={value}")
        if name.endswith("_ids"):
            value = getattr(state, name, [])
            if value:
                artifacts.append(f"{name}={', '.join(value)}")
    if artifacts:
        lines.append("artifacts: " + "  ".join(artifacts))
    notes = getattr(state, "run_notes", [])
    if notes:
        lines.append("")
        lines.extend(f"[dim]· {n}[/]" for n in notes[-8:])
    console.print(Panel("\n".join(lines), title="run complete", border_style="green"))


async def _run_workflow(ctx, name: str, input_arg: str,
                        resume: Optional[str], answers: dict) -> Optional[RunResult]:
    spec = WORKFLOWS[name]
    state = None if resume else spec.make_state(input_arg, ctx)
    runner = Runner(ctx)
    try:
        return await run_to_completion(
            runner, spec.build(), state, _ask_gate,
            run_id=resume, answers=answers, max_steps=400)
    except _GateAbort as ga:
        console.print(Panel(
            f"Run paused at gate [bold]{ga.gate}[/].\nResume with:\n"
            f"  triad run {name} --resume {ga.run_id} --answer {ga.gate}=yes",
            border_style="yellow"))
        return None


# ---------------------------------------------------------------------------

@app.command()
def init(path: str = typer.Argument(".", help="repository root")) -> None:
    """Attach a Triad lab to a repository: docs/ notebook, knowledge graph,
    config."""
    root = Path(path).resolve()
    ctx = build_context(root, on_event=None)
    config_path = root / ".triad" / "config.toml"
    if not config_path.exists():
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(STARTER_CONFIG)
    from .tools import repo as repo_mod
    git = "yes" if repo_mod.is_git_repo(root) else "NO — parallel dev isolation disabled"
    console.print(Panel(
        f"lab attached at [bold]{root}[/]\n"
        f"notebook: {ctx.docs_dir}\n"
        f"knowledge graph: {ctx.docs_dir / 'knowledge' / 'graph.db'}\n"
        f"config: {config_path}\n"
        f"git repo: {git}\n\n"
        "next:\n"
        "  export ANTHROPIC_API_KEY=…\n"
        '  triad chat                     # talk to the Scientist + Tech Lead\n'
        '  triad run literature "…"       # or go straight to work\n'
        '  triad run study "…"            # the full loop',
        title=f"triad {__version__}", border_style="blue"))


@app.command()
def run(
    workflow: str = typer.Argument(..., help=f"one of: {', '.join(WORKFLOWS)}"),
    input_arg: str = typer.Argument("", metavar="INPUT", help="question / EXP-id / TCK-id"),
    path: str = typer.Option(".", "--path", "-p"),
    resume: Optional[str] = typer.Option(None, "--resume", help="run id to resume"),
    answer: list[str] = typer.Option([], "--answer", "-a", help="gate=value (repeatable)"),
    yes: bool = typer.Option(False, "--yes", "-y", help="auto-approve all gates"),
) -> None:
    """Run a workflow graph with live progress."""
    if workflow not in WORKFLOWS:
        console.print(f"[red]unknown workflow {workflow!r}[/] — try: {', '.join(WORKFLOWS)}")
        raise typer.Exit(1)
    ctx = _ctx(path)
    if yes:
        ctx.config.autonomy.gates = False
    answers: dict[str, list[str]] = {}
    for item in answer:
        gate, _, value = item.partition("=")
        answers.setdefault(gate.strip(), []).append(value.strip() or "yes")
    try:
        result = asyncio.run(_run_workflow(ctx, workflow, input_arg, resume, answers))
    except (GraphError, ValueError) as exc:
        console.print(Panel(str(exc), title="run failed", border_style="red"))
        raise typer.Exit(1)
    if result:
        _summarize(result)


@app.command()
def chat(path: str = typer.Option(".", "--path", "-p")) -> None:
    """Talk to the Research Scientist and Tech Lead (persistent sessions)."""
    asyncio.run(_chat_main(path))


async def _chat_main(path: str) -> None:
    from .chat import LabChat, save_standup, standup_text

    ctx = _ctx(path)
    console.print(Panel(
        "You're in the lab. [cyan]@rs[/] Research Scientist · [magenta]@tl[/] Tech Lead "
        "· plain messages auto-route.\n"
        "/run <workflow> <input…>   /standup   /status   /costs   /graph <wf>   /quit",
        title="triad chat", border_style="blue"))
    async with LabChat(ctx) as lab:
        while True:
            try:
                text = (await asyncio.to_thread(console.input, "[bold]you ▸ [/]")).strip()
            except (EOFError, KeyboardInterrupt):
                break
            if not text:
                continue
            if text in ("/quit", "/exit", "/q"):
                break
            if text == "/standup":
                console.print(Markdown(standup_text(ctx)))
                console.print(f"[dim]saved: {save_standup(ctx)}[/]")
                continue
            if text == "/status":
                _status_table(ctx)
                continue
            if text == "/costs":
                _costs_table(ctx)
                console.print(f"[dim]this chat session: ${lab.costs:.2f}[/]")
                continue
            if text.startswith("/graph"):
                parts = text.split()
                if len(parts) > 1 and parts[1] in WORKFLOWS:
                    console.print(WORKFLOWS[parts[1]].build().to_mermaid())
                else:
                    _workflow_table()
                continue
            if text.startswith("/run"):
                try:
                    parts = shlex.split(text)[1:]
                except ValueError as exc:
                    console.print(f"[red]{exc}[/]")
                    continue
                if not parts or parts[0] not in WORKFLOWS:
                    _workflow_table()
                    continue
                name, arg = parts[0], " ".join(parts[1:])
                try:
                    result = await _run_workflow(ctx, name, arg, None, {})
                    if result:
                        _summarize(result)
                except (GraphError, ValueError) as exc:
                    console.print(Panel(str(exc), border_style="red"))
                continue
            try:
                replies = await lab.ask(text)
            except Exception as exc:  # noqa: BLE001 — chat survives bad turns
                console.print(Panel(f"{type(exc).__name__}: {exc}", border_style="red"))
                continue
            for key, reply in replies:
                title, style = PERSONA_STYLE.get(key, (key, "white"))
                console.print(Panel(Markdown(reply), title=title, border_style=style))
    console.print(f"[dim]chat closed. session cost ${lab.costs:.2f}[/]")


def _workflow_table() -> None:
    table = Table(title="workflows")
    table.add_column("name", style="bold")
    table.add_column("input")
    table.add_column("what it does")
    for spec in WORKFLOWS.values():
        table.add_row(spec.name, spec.input_help, spec.description)
    console.print(table)


@app.command()
def graph(
    workflow: Optional[str] = typer.Argument(None),
    export: bool = typer.Option(False, "--export", help="write all graphs to docs/graphs/*.mmd"),
    path: str = typer.Option(".", "--path", "-p"),
) -> None:
    """Show a workflow's Mermaid graph, list workflows, or export all."""
    if export:
        ctx = _ctx(path, live=False)
        out_dir = ctx.docs_dir / "graphs"
        out_dir.mkdir(parents=True, exist_ok=True)
        for spec in WORKFLOWS.values():
            target = out_dir / f"{spec.name}.mmd"
            target.write_text(spec.build().to_mermaid() + "\n")
            console.print(f"[green]✓[/] {target}")
        kg_target = out_dir / "knowledge.mmd"
        kg_target.write_text(ctx.kg.to_mermaid() + "\n")
        console.print(f"[green]✓[/] {kg_target}")
        return
    if workflow:
        if workflow not in WORKFLOWS:
            console.print(f"[red]unknown workflow {workflow!r}[/]")
            raise typer.Exit(1)
        print(WORKFLOWS[workflow].build().to_mermaid())
        return
    _workflow_table()


def _status_table(ctx) -> None:
    runs = ctx.store.list_runs()
    if not runs:
        console.print("[dim]no runs yet[/]")
        return
    table = Table(title="runs")
    for col in ("run id", "graph", "status", "cost", "started"):
        table.add_column(col)
    for meta in runs[-20:]:
        cost = meta.get("cost_usd")
        status = meta.get("status", "?")
        style = {"completed": "green", "failed": "red",
                 "waiting_gate": "yellow"}.get(status, "white")
        table.add_row(
            meta["run_id"], meta.get("graph", "?"), f"[{style}]{status}[/]"
            + (f" ({meta.get('gate')})" if meta.get("gate") else ""),
            f"${cost:.2f}" if isinstance(cost, (int, float)) else "—",
            str(meta.get("started", ""))[:16],
        )
    console.print(table)


@app.command()
def status(
    run_id: Optional[str] = typer.Argument(None),
    path: str = typer.Option(".", "--path", "-p"),
) -> None:
    """List runs, or show one run's steps."""
    ctx = _ctx(path, live=False)
    if not run_id:
        _status_table(ctx)
        return
    meta = ctx.store.meta(run_id)
    if not meta:
        console.print(f"[red]no run {run_id!r}[/]")
        raise typer.Exit(1)
    console.print(Panel("\n".join(f"{k}: {v}" for k, v in meta.items()), title=run_id))
    steps = ctx.store.steps(run_id)
    table = Table(title="steps")
    for col in ("#", "node", "kind", "next", "s", "updated"):
        table.add_column(col)
    for record in steps[-40:]:
        table.add_row(str(record.get("step")), record.get("node", ""), record.get("kind", ""),
                      record.get("next", ""), str(record.get("duration_s", "")),
                      ", ".join(record.get("updated", [])))
    console.print(table)


def _costs_table(ctx) -> None:
    runs = ctx.store.list_runs()
    by_graph: dict[str, float] = {}
    total = 0.0
    for meta in runs:
        cost = meta.get("cost_usd")
        if isinstance(cost, (int, float)):
            by_graph[meta.get("graph", "?")] = by_graph.get(meta.get("graph", "?"), 0.0) + cost
            total += cost
    table = Table(title="costs by workflow")
    table.add_column("graph")
    table.add_column("USD", justify="right")
    for name, cost in sorted(by_graph.items(), key=lambda kv: -kv[1]):
        table.add_row(name, f"${cost:.2f}")
    table.add_row("[bold]total[/]", f"[bold]${total:.2f}[/]")
    console.print(table)


@app.command()
def costs(path: str = typer.Option(".", "--path", "-p")) -> None:
    """Aggregate spend across recorded runs."""
    _costs_table(_ctx(path, live=False))


@app.command()
def standup(path: str = typer.Option(".", "--path", "-p")) -> None:
    """Generate and save today's standup from runs + notebook state."""
    from .chat import save_standup, standup_text
    ctx = _ctx(path, live=False)
    console.print(Markdown(standup_text(ctx)))
    console.print(f"[dim]saved: {save_standup(ctx)}[/]")


@app.command()
def version() -> None:
    print(f"triad {__version__}")


if __name__ == "__main__":
    app()
