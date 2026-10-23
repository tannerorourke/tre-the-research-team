"""Plain tool functions (papers, repo, stats) plus the MCP server that exposes
them to agent turns. `server` is imported lazily by the SDK runtime — nothing
here forces the SDK at import time."""

from . import papers, repo, stats

__all__ = ["papers", "repo", "stats"]
