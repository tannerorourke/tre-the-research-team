"""Shared workflow machinery: the agent_step factory that turns a persona +
prompt builder + output schema into a graph node, with costs recorded."""

from __future__ import annotations

from typing import Any, Callable, Optional

from pydantic import BaseModel

from ..agents.personas import PERSONAS
from ..graph import Context, GraphState

PromptBuilder = Callable[[Any, Context], str]
OutMapper = Callable[[Any, Any, Context], dict]


def agent_step(
    name: str,
    persona_key: str,
    build_prompt: PromptBuilder,
    schema: Optional[type[BaseModel]] = None,
    out: Optional[OutMapper] = None,
    *,
    tools: Optional[list[str]] = None,
    use_lab_tools: bool = True,
    max_turns: Optional[int] = None,
    cwd_from: Optional[Callable[[Any, Context], Optional[str]]] = None,
):
    """Build a node fn that runs one persona task and maps its output into a
    state update. `out(data_or_text, state, ctx) -> dict`."""

    async def fn(state: GraphState, ctx: Context) -> dict:
        persona = PERSONAS[persona_key]
        prompt = build_prompt(state, ctx)
        cwd = cwd_from(state, ctx) if cwd_from else None
        result = await ctx.runtime.run(
            persona=persona, prompt=prompt, schema=schema, node=name,
            tools=tools, use_lab_tools=use_lab_tools, cwd=cwd, max_turns=max_turns,
        )
        payload = result.data if schema is not None else result.text
        update = dict(out(payload, state, ctx) or {}) if out else {}
        costs = dict(update.get("costs") or {})
        costs[name] = costs.get(name, 0.0) + result.cost
        update["costs"] = costs
        return update

    fn.__name__ = name
    return fn


def clip(text: str, chars: int) -> str:
    text = text or ""
    return text if len(text) <= chars else text[: chars - 20] + "\n… (truncated) …"


def bullet(items: list[str]) -> str:
    return "\n".join(f"- {i}" for i in items) if items else "- (none)"
