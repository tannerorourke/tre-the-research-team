"""The standup: a dated Markdown summary of a lab's recent runs, notebook
entries, and knowledge-graph size, saved under the notebook's `standups/`."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..graph import Context


def standup_text(ctx: Context) -> str:
    lines = [f"# Standup — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}", ""]
    runs = ctx.store.list_runs()
    if runs:
        lines.append("## Recent runs")
        for meta in runs[-8:]:
            cost = meta.get("cost_usd")
            lines.append(f"- `{meta['run_id']}` {meta.get('graph', '?')}: "
                         f"**{meta.get('status', '?')}**"
                         + (f", ${cost:.2f}" if isinstance(cost, (int, float)) else "")
                         + (f" (gate: {meta.get('gate')})" if meta.get("gate") else ""))
        lines.append("")
    for kind, title in (("experiment", "Experiments"), ("ticket", "Tickets"),
                        ("hypothesis", "Hypotheses")):
        entries = ctx.notebook.entries(kind)
        if entries:
            lines.append(f"## {title}")
            for e in entries[-10:]:
                lines.append(f"- {e['id']} [{e['status'] or '—'}] {e['title']}")
            lines.append("")
    reports = ctx.notebook.entries("report")
    if reports:
        lines.append("## Latest reports")
        for e in reports[-3:]:
            lines.append(f"- {e['id']} {e['title']}")
        lines.append("")
    stats = ctx.kg.stats()
    lines.append(f"_Knowledge graph: {stats['nodes']} nodes, {stats['edges']} edges._")
    return "\n".join(lines)


def save_standup(ctx: Context) -> str:
    text = standup_text(ctx)
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    directory = ctx.docs_dir / "standups"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{day}.md"
    n = 2
    while path.exists():
        path = directory / f"{day}-{n}.md"
        n += 1
    path.write_text(text + "\n")
    return str(path)
