"""The lab's in-process MCP server: the tool surface agents see.

Registers the tool bodies in 'tre.tools.lab_tools' with the Claude Agent
SDK. Imported lazily by the SDK runtime so tests (and any environment
without the SDK) never touch it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .. import __version__
from .lab_tools import LAB_TOOLS, LabTool

if TYPE_CHECKING:
    from ..graph import Context


def _bind(spec: LabTool, ctx: Any):
    async def handler(args: dict) -> dict:
        return await spec.fn(ctx, args)
    return handler


def build_lab_server(ctx: Context):
    from claude_agent_sdk import create_sdk_mcp_server, tool

    tools = [tool(spec.name, spec.description, spec.params)(_bind(spec, ctx)) for spec in LAB_TOOLS]
    return create_sdk_mcp_server(name="lab", version=__version__, tools=tools)


LAB_TOOL_NAMES = [f"mcp__lab__{spec.name}" for spec in LAB_TOOLS]
