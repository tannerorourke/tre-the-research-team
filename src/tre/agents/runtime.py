"""Agent runtime: one narrow interface between workflows and the Claude Agent
SDK, with a mock twin for offline tests.

Design points:
- Structured outputs ride the SDK's `output_format` (JSON-schema enforced,
  retried by the SDK) and are re-validated with pydantic here.
- Guard hooks deny `git push`, `sudo`, destructive `rm`, and file writes
  outside the repository, regardless of what a prompt talks an agent into.
- Every call reports its cost; workflows fold costs into graph state.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any, Optional, Protocol

from pydantic import BaseModel, ValidationError

from .personas import Persona


class AgentError(RuntimeError):
    pass


@dataclass
class AgentResult:
    text: str
    data: Any = None                 # validated schema instance when requested
    cost: float = 0.0
    session_id: Optional[str] = None


class AgentRuntime(Protocol):
    async def run(
        self,
        *,
        persona: Persona,
        prompt: str,
        schema: Optional[type[BaseModel]] = None,
        node: str = "",
        tools: Optional[list[str]] = None,
        use_lab_tools: bool = True,
        cwd: Optional[str] = None,
        max_turns: Optional[int] = None,
        model: Optional[str] = None,
        permission_mode: Optional[str] = None,
    ) -> AgentResult: ...


# SDK-backed runtime

BASH_DENY: list[tuple[str, str]] = [
    (r"\bgit\s+push\b", "pushing is a human decision — leave commits local"),
    (r"\bsudo\b", "no privilege escalation from agent turns"),
    (r"\brm\s+-[a-zA-Z]*[rf][a-zA-Z]*\s+(/|~)", "destructive rm on absolute/home paths is blocked"),
    (r"curl[^|\n]*\|\s*(ba|z)?sh", "piping downloads into a shell is blocked"),
    (r"\bgit\s+reset\s+--hard\b", "hard resets are a human decision"),
]


def _field_of(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


class SDKRuntime:
    """Runs one persona turn (or task) through the Claude Agent SDK."""

    def __init__(self, ctx: Any):
        self.ctx = ctx               # tre.graph.Context (runtime set after init)
        self._server = None

    def lab_server(self):
        if self._server is None:
            from ..tools.server import build_lab_server
            self._server = build_lab_server(self.ctx)
        return self._server

    def build_options(
        self,
        persona: Persona,
        *,
        schema: Optional[type[BaseModel]] = None,
        tools: Optional[list[str]] = None,
        use_lab_tools: bool = True,
        cwd: Optional[str] = None,
        max_turns: Optional[int] = None,
        model: Optional[str] = None,
        permission_mode: Optional[str] = None,
        system_suffix: str = "",
        include_partial: bool = False,
    ):
        from claude_agent_sdk import ClaudeAgentOptions

        from ..tools.server import LAB_TOOL_NAMES

        allowed = list(tools if tools is not None else persona.default_tools)
        mcp_servers: dict[str, Any] = {}
        if use_lab_tools:
            mcp_servers["lab"] = self.lab_server()
            allowed += LAB_TOOL_NAMES

        kwargs: dict[str, Any] = dict(
            system_prompt=persona.system + (("\n\n" + system_suffix) if system_suffix else ""),
            model=model or getattr(self.ctx.config.models, persona.model_field),
            allowed_tools=allowed,
            permission_mode=permission_mode or self.ctx.config.autonomy.permission_mode,
            cwd=str(cwd or self.ctx.root),
            mcp_servers=mcp_servers,
            hooks=self._hooks(),
            # -- no filesystem settings: an agent turn must not inherit the operator's
            #    CLAUDE.md, skills, or memory; the charter and the lab server are the whole context
            setting_sources=[],
        )
        if max_turns:
            kwargs["max_turns"] = max_turns
        if include_partial:
            kwargs["include_partial_messages"] = True
        if schema is not None:
            kwargs["output_format"] = {"type": "json_schema", "schema": schema.model_json_schema()}
        budget = self.ctx.config.autonomy.max_budget_usd_per_run
        if budget:
            kwargs["max_budget_usd"] = budget
        return ClaudeAgentOptions(**kwargs)

    def _hooks(self):
        from claude_agent_sdk import HookMatcher

        root = os.path.abspath(str(self.ctx.root))

        def _deny(input_data: Any, reason: str) -> dict:
            return {
                "hookSpecificOutput": {
                    "hookEventName": _field_of(input_data, "hook_event_name", "PreToolUse"),
                    "permissionDecision": "deny",
                    "permissionDecisionReason": reason,
                }
            }

        async def guard_bash(input_data: Any, tool_use_id: Any, context: Any) -> dict:
            command = str(_field_of(_field_of(input_data, "tool_input", {}) or {}, "command", ""))
            for pattern, reason in BASH_DENY:
                if re.search(pattern, command):
                    return _deny(input_data, reason)
            return {}

        async def guard_paths(input_data: Any, tool_use_id: Any, context: Any) -> dict:
            tool_input = _field_of(input_data, "tool_input", {}) or {}
            path = str(_field_of(tool_input, "file_path", "") or _field_of(tool_input, "path", ""))
            if path and os.path.isabs(path):
                resolved = os.path.abspath(path)
                if not (resolved.startswith(root + os.sep) or resolved == root or resolved.startswith("/tmp")):
                    return _deny(input_data, f"writes outside the repository ({root}) are blocked")
            return {}

        return {
            "PreToolUse": [
                HookMatcher(matcher="Bash", hooks=[guard_bash]),
                HookMatcher(matcher="Write|Edit|NotebookEdit", hooks=[guard_paths]),
            ]
        }

    # -- one-shot task ------------------------------------------------------

    async def run(
        self,
        *,
        persona: Persona,
        prompt: str,
        schema: Optional[type[BaseModel]] = None,
        node: str = "",
        tools: Optional[list[str]] = None,
        use_lab_tools: bool = True,
        cwd: Optional[str] = None,
        max_turns: Optional[int] = None,
        model: Optional[str] = None,
        permission_mode: Optional[str] = None,
    ) -> AgentResult:
        from claude_agent_sdk import AssistantMessage, ResultMessage, query

        options = self.build_options(
            persona, schema=schema, tools=tools, use_lab_tools=use_lab_tools,
            cwd=cwd, max_turns=max_turns, model=model, permission_mode=permission_mode,
        )

        final: Optional[Any] = None
        fallback_text: list[str] = []
        async for message in query(prompt=prompt, options=options):
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    text = getattr(block, "text", None)
                    if text:
                        fallback_text.append(text)
            if isinstance(message, ResultMessage):
                final = message

        if final is None:
            raise AgentError(f"[{persona.key}:{node}] no ResultMessage from SDK")
        subtype = getattr(final, "subtype", "") or ""
        is_error = bool(getattr(final, "is_error", False)) or subtype.startswith("error")
        text = getattr(final, "result", None) or ("\n".join(fallback_text))
        cost = float(getattr(final, "total_cost_usd", 0.0) or 0.0)
        session_id = getattr(final, "session_id", None)

        self.ctx.emitter.emit(
            "", "agent_call", persona=persona.key, node=node,
            cost_usd=round(cost, 4), model=options.model if hasattr(options, "model") else "",
            error=is_error,
        )

        if is_error:
            raise AgentError(f"[{persona.key}:{node}] SDK error ({subtype}): {str(text)[:500]}")

        data = None
        if schema is not None:
            payload = getattr(final, "structured_output", None)
            if payload is None:
                raise AgentError(f"[{persona.key}:{node}] expected structured output "
                                 f"({schema.__name__}), got none")
            try:
                data = schema.model_validate(payload)
            except ValidationError as exc:
                raise AgentError(
                    f"[{persona.key}:{node}] structured output failed "
                    f"{schema.__name__} validation: {exc}") from exc
        return AgentResult(text=str(text), data=data, cost=cost, session_id=session_id)


# Mock runtime (tests / offline dry runs)

@dataclass
class MockRuntime:
    queues: dict[str, list[Any]] = field(default_factory=dict)
    calls: list[dict] = field(default_factory=list)
    default_factory: Any = None      # (persona, prompt, schema, node) -> payload

    def enqueue(self, node: str, *payloads: Any) -> "MockRuntime":
        self.queues.setdefault(node, []).extend(payloads)
        return self

    async def run(
        self,
        *,
        persona: Persona,
        prompt: str,
        schema: Optional[type[BaseModel]] = None,
        node: str = "",
        **kwargs: Any,
    ) -> AgentResult:
        self.calls.append({"persona": persona.key, "node": node,
                           "prompt": prompt, "schema": schema.__name__ if schema else None,
                           **{k: v for k, v in kwargs.items() if k in ("cwd", "model")}})
        queue = self.queues.get(node) or self.queues.get(node.split(":", 1)[0])
        if queue:
            payload = queue.pop(0)
        elif self.default_factory is not None:
            payload = self.default_factory(persona, prompt, schema, node)
        else:
            raise AssertionError(f"MockRuntime: nothing queued for node {node!r} "
                                 f"(persona {persona.key})")
        if isinstance(payload, Exception):
            raise payload
        data = None
        if schema is not None:
            data = payload if isinstance(payload, BaseModel) else schema.model_validate(payload)
            text = data.model_dump_json()
        else:
            text = str(payload)
        return AgentResult(text=text, data=data, cost=0.001, session_id="mock")
