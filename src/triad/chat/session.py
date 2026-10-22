"""Interactive lab chat: the user talks to the Research Scientist and Tech
Lead in one thread. A cheap router (rules first, small model on ambiguity)
picks who answers; "both" runs a confer round — primary drafts, the other
adds their take. Sessions are persistent SDK clients, so each principal keeps
conversational memory across turns.
"""

from __future__ import annotations

from contextlib import AsyncExitStack
from datetime import datetime, timezone
from typing import Optional

from ..agents.personas import PERSONAS
from ..agents.schemas import RouteDecision
from ..graph import Context

TL_HINTS = ("repo", "code", "refactor", "ticket", "merge", "test", "ci", "infra",
            "branch", "implement", "bug", "deploy", "architecture", "adr",
            "pipeline", "worktree", "dependency")
RS_HINTS = ("paper", "literature", "hypothes", "design", "metric", "baseline",
            "ablation", "result", "significan", "analysis", "related work",
            "cite", "effect size", "seed", "variance", "interpret")

CHAT_SUFFIX = (
    "You are in a live chat session with the human principal of the lab. "
    "Reply conversationally but tersely. Use your lab tools to check the "
    "notebook/knowledge graph/repo before asserting lab state. If real work "
    "is warranted (a literature review, a design, tickets, an analysis), say "
    "which `triad run …` workflow you would kick off rather than doing deep "
    "work inline."
)


class LabChat:
    def __init__(self, ctx: Context):
        self.ctx = ctx
        self._stack: Optional[AsyncExitStack] = None
        self._clients: dict[str, object] = {}
        self.costs: float = 0.0
        self.transcript: list[tuple[str, str]] = []

    # -- lifecycle ----------------------------------------------------------

    async def __aenter__(self) -> "LabChat":
        self._stack = AsyncExitStack()
        await self._stack.__aenter__()
        return self

    async def __aexit__(self, *exc) -> None:
        if self._stack:
            await self._stack.__aexit__(*exc)
            self._stack = None
        self._clients.clear()

    async def _client(self, key: str):
        if key in self._clients:
            return self._clients[key]
        from claude_agent_sdk import ClaudeSDKClient

        persona = PERSONAS[key]
        digest = self.ctx.notebook.digest()
        options = self.ctx.runtime.build_options(
            persona,
            system_suffix=CHAT_SUFFIX + "\n\n## Current lab state\n\n" + digest,
        )
        client = ClaudeSDKClient(options=options)
        assert self._stack is not None, "use `async with LabChat(ctx)`"
        await self._stack.enter_async_context(client)
        self._clients[key] = client
        return client

    # -- routing ------------------------------------------------------------

    async def route(self, text: str) -> str:
        lowered = text.lower()
        if lowered.startswith("@rs"):
            return "rs"
        if lowered.startswith("@tl"):
            return "tl"
        if lowered.startswith("@both") or lowered.startswith("@all"):
            return "both"
        tl_score = sum(h in lowered for h in TL_HINTS)
        rs_score = sum(h in lowered for h in RS_HINTS)
        if tl_score and tl_score >= rs_score + 2:
            return "tl"
        if rs_score and rs_score >= tl_score + 2:
            return "rs"
        if tl_score == rs_score == 0 and len(text) < 40:
            return "rs"  # small talk defaults to the scientist
        try:
            result = await self.ctx.runtime.run(
                persona=PERSONAS["router"],
                prompt=(
                    "Route this user message to the right lab principal.\n"
                    "rs = Research Scientist (science: papers, hypotheses, designs, "
                    "stats, interpretation). tl = Tech Lead (engineering: repo, "
                    "tickets, reviews, infra). both = genuinely needs the pair.\n\n"
                    f"Message: {text}"
                ),
                schema=RouteDecision, node="chat_route", use_lab_tools=False,
            )
            return result.data.target
        except Exception:  # noqa: BLE001 — routing must never sink the chat
            return "rs"

    # -- turns --------------------------------------------------------------

    async def _say(self, key: str, text: str) -> str:
        from claude_agent_sdk import AssistantMessage, ResultMessage

        client = await self._client(key)
        await client.query(text)
        chunks: list[str] = []
        final = ""
        async for message in client.receive_response():
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    piece = getattr(block, "text", None)
                    if piece:
                        chunks.append(piece)
            if isinstance(message, ResultMessage):
                final = getattr(message, "result", "") or ""
                self.costs += float(getattr(message, "total_cost_usd", 0.0) or 0.0)
        reply = final or "\n".join(chunks) or "(no reply)"
        self.transcript.append((key, reply))
        return reply

    async def ask(self, text: str) -> list[tuple[str, str]]:
        """One user turn. Returns [(persona_key, reply), ...]."""
        target = await self.route(text)
        stripped = text
        for prefix in ("@rs", "@tl", "@both", "@all"):
            if stripped.lower().startswith(prefix):
                stripped = stripped[len(prefix):].lstrip(" :,")
                break
        if target in ("rs", "tl"):
            reply = await self._say(target, stripped)
            return [(target, reply)]
        # confer: RS drafts, TL adds the engineering take
        rs_reply = await self._say("rs", stripped)
        tl_reply = await self._say(
            "tl",
            f"The user asked the lab:\n{stripped}\n\n"
            f"The Research Scientist replied:\n{rs_reply}\n\n"
            "Add your engineering take: agree/disagree with reasons, plus what "
            "you would concretely do in the repo. Terse.",
        )
        return [("rs", rs_reply), ("tl", tl_reply)]


# -- standup (pure code: cheap, deterministic) -------------------------------

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
