"""The `tre` CLI: init a lab on a repo, run workflow graphs with live progress, inspect runs/costs, export graphs."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import time
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
from .memory import KnowledgeGraph
from .workflows import WORKFLOWS

app = typer.Typer(
    help="TRE — a three-agent ML research lab (Research Scientist, Tech Lead, "
         "Developer) that lives on your repository.",
    no_args_is_help=True, add_completion=False,
)
console = Console()

EXIT_DONE, EXIT_FAILED, EXIT_GATE = 0, 1, 3
WAIT_STATUSES = {"running", "queued"}
DONE_STATUSES = ("completed", "done")


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


def _headless_gate(ctx):
    async def ask(gi: GraphInterrupt) -> str:
        ctx.store.set_status(gi.run_id, "waiting_gate", gate=gi.gate, gate_message=gi.message)
        raise _GateAbort(gi.run_id, gi.gate)
    return ask


def _exit_code(status: str) -> int:
    if status in DONE_STATUSES:
        return EXIT_DONE
    return EXIT_GATE if status == "waiting_gate" else EXIT_FAILED


def _finish(ctx, run_id: Optional[str], code: int, status: Optional[str] = None) -> None:
    """Print the machine-readable final line and exit; supervisors parse it."""
    meta = (ctx.store.meta(run_id) if ctx and run_id else None) or {}
    status = status or meta.get("status") or "-"
    gate = meta.get("gate") if status == "waiting_gate" else None
    print(f"[tre] {run_id or '-'} exit {code} status={status} gate={gate or '-'}", flush=True)
    raise typer.Exit(code)


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


async def _run_workflow(ctx, name: str, input_arg: str, resume: Optional[str],
                        answers: dict, *, headless: bool = False,
                        run_id: Optional[str] = None) -> Optional[RunResult]:
    spec = WORKFLOWS[name]
    state = None if resume else spec.make_state(input_arg, ctx)
    runner = Runner(ctx)
    ask = _headless_gate(ctx) if headless else _ask_gate
    try:
        return await run_to_completion(
            runner, spec.build(), state, ask,
            run_id=resume or run_id, answers=answers, max_steps=400)
    except _GateAbort as ga:
        console.print(Panel(
            f"Run paused at gate [bold]{ga.gate}[/].\nResume with:\n"
            f"  tre run {name} --resume {ga.run_id} --answer {ga.gate}=yes",
            border_style="yellow"))
        return None


# ---------------------------------------------------------------------------

@app.command()
def init(path: str = typer.Argument(".", help="repository root")) -> None:
    """Attach a TRE lab to a repository: docs/ notebook, knowledge graph,
    config."""
    root = Path(path).resolve()
    ctx = build_context(root, on_event=None)
    config_path = root / ".tre" / "config.toml"
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
        '  tre run literature "…"       # survey the literature\n'
        '  tre run study "…"            # the full loop',
        title=f"tre {__version__}", border_style="blue"))


@app.command()
def run(
    workflow: str = typer.Argument(..., help=f"one of: {', '.join(WORKFLOWS)}"),
    input_arg: str = typer.Argument("", metavar="INPUT", help="question / EXP-id / TCK-id"),
    path: str = typer.Option(".", "--path", "-p"),
    resume: Optional[str] = typer.Option(None, "--resume", help="run id to resume"),
    answer: list[str] = typer.Option([], "--answer", "-a", help="gate=value (repeatable)"),
    yes: bool = typer.Option(False, "--yes", "-y", help="auto-approve all gates"),
    headless: bool = typer.Option(False, "--headless",
                                  help="never prompt; exit 3 at an unanswered gate"),
    run_id: Optional[str] = typer.Option(None, "--run-id",
                                         help="id for a fresh run, preallocated by a supervisor"),
) -> None:
    """Run a workflow graph with live progress. The last stdout line is
    `[tre] <run_id> exit <code> status=<status> gate=<gate or ->`."""
    if workflow not in WORKFLOWS:
        console.print(f"[red]unknown workflow {workflow!r}[/] — try: {', '.join(WORKFLOWS)}")
        _finish(None, None, EXIT_FAILED)
    ctx = _ctx(path)
    if yes:
        ctx.config.autonomy.gates = False
    answers: dict[str, list[str]] = {}
    for item in answer:
        gate, _, value = item.partition("=")
        answers.setdefault(gate.strip(), []).append(value.strip() or "yes")
    try:
        result = asyncio.run(_run_workflow(ctx, workflow, input_arg, resume, answers,
                                           headless=headless, run_id=run_id))
    except (GraphError, ValueError) as exc:
        console.print(Panel(str(exc), title="run failed", border_style="red"))
        _finish(ctx, ctx.extras.get("current_run_id") or resume or run_id, EXIT_FAILED)
    if result:
        _summarize(result)
        _finish(ctx, result.run_id, EXIT_DONE)
    _finish(ctx, ctx.extras.get("current_run_id") or resume or run_id, EXIT_GATE)


@app.command()
def wait(
    run_id: str = typer.Argument(...),
    path: str = typer.Option(".", "--path", "-p"),
    poll: float = typer.Option(5.0, "--poll", help="seconds between meta.json reads"),
) -> None:
    """Block until a run leaves `running`, then print its exit line."""
    ctx = _ctx(path, live=False)
    meta = ctx.store.meta(run_id)
    if meta is None:
        console.print(f"[red]no run {run_id!r}[/]")
        _finish(ctx, run_id, EXIT_FAILED)
    gone = 0
    while meta.get("status") in WAIT_STATUSES:
        time.sleep(poll)
        meta = ctx.store.meta(run_id) or {}
        # -- a worker that died without its final write leaves 'running' forever;
        #    two polls with its pid gone end the wait. Writing 'died' stays the supervisor's.
        gone = gone + 1 if meta.get("status") == "running" and _worker_gone(ctx, run_id) else 0
        if gone >= 2:
            _finish(ctx, run_id, EXIT_FAILED, status="died")
    _finish(ctx, run_id, _exit_code(meta.get("status", "")))


def _worker_gone(ctx, run_id: str) -> bool:
    record = ctx.store.dir_of(run_id) / "worker.json"
    if not record.exists():
        return False
    pid = json.loads(record.read_text()).get("pid")
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    return False


@app.command()
def serve(
    host: str = typer.Option("127.0.0.1", "--host", help="loopback address to bind"),
    port: int = typer.Option(8765, "--port"),
) -> None:
    """Run the MCP daemon on streamable HTTP; supervises runs across registered repositories."""
    from .serve.daemon import is_loopback, run as run_daemon
    if not is_loopback(host):
        console.print(f"[red]refusing to bind {host!r}: the daemon has no auth and serves loopback only[/]")
        raise typer.Exit(2)
    console.print(f"[bold blue]tre serve[/] http://{host}:{port}/mcp")
    run_daemon(host, port)


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
    export: bool = typer.Option(False, "--export", help="write every graph as Mermaid"),
    out: Optional[Path] = typer.Option(None, "--out", help="export directory (default: the attached repo's docs/graphs)"),
    path: str = typer.Option(".", "--path", "-p"),
) -> None:
    """Show a workflow's Mermaid graph, list workflows, or export all."""
    if export:
        _export_graphs(out, path)
        return
    if workflow:
        if workflow not in WORKFLOWS:
            console.print(f"[red]unknown workflow {workflow!r}[/]")
            raise typer.Exit(1)
        print(WORKFLOWS[workflow].build().to_mermaid())
        return
    _workflow_table()


def _export_graphs(out: Optional[Path], path: str) -> None:
    # -- '--out' exports without a notebook: the legend comes from an empty
    #    knowledge graph in a temp dir, so a bare checkout regenerates clean
    if out is None:
        ctx = _ctx(path, live=False)
        out, kg = ctx.docs_dir / "graphs", ctx.kg
    else:
        tmp = tempfile.TemporaryDirectory()
        kg = KnowledgeGraph(Path(tmp.name) / "graph.db")
    out.mkdir(parents=True, exist_ok=True)
    for spec in WORKFLOWS.values():
        target = out / f"{spec.name}.mmd"
        target.write_text(spec.build().to_mermaid() + "\n")
        console.print(f"[green]✓[/] {target}")
    kg_target = out / "knowledge.mmd"
    kg_target.write_text(kg.to_mermaid() + "\n")
    console.print(f"[green]✓[/] {kg_target}")


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
    as_json: bool = typer.Option(False, "--json", help="print meta.json"),
) -> None:
    """List runs, or show one run's steps."""
    ctx = _ctx(path, live=False)
    if not run_id:
        if as_json:
            print(json.dumps(ctx.store.list_runs(), indent=2))
            return
        _status_table(ctx)
        return
    meta = ctx.store.meta(run_id)
    if not meta:
        console.print(f"[red]no run {run_id!r}[/]")
        raise typer.Exit(1)
    if as_json:
        print(json.dumps(meta, indent=2))
        return
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
    from .memory.standup import save_standup, standup_text
    ctx = _ctx(path, live=False)
    console.print(Markdown(standup_text(ctx)))
    console.print(f"[dim]saved: {save_standup(ctx)}[/]")


@app.command("plugin-sync")
def plugin_sync(root: Path = typer.Option(Path("."), "--root", help="plugin root holding skills/")) -> None:
    """Regenerate the persona skills from their charters."""
    from .plugin import sync
    for target in sync(root):
        console.print(f"[green]✓[/] {target}")


@app.command()
def version() -> None:
    print(f"tre {__version__}")


if __name__ == "__main__":
    app()
