"""The lab's in-process MCP server: the tool surface agents see.

Wraps the plain functions in triad.tools and the notebook/knowledge-graph
layers. Imported lazily by the SDK runtime so tests (and any environment
without the SDK) never touch it.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from .. import __version__
from ..memory import EDGE_TYPES, NODE_TYPES
from . import papers as papers_mod
from . import repo as repo_mod


def _text(payload: Any, *, is_error: bool = False) -> dict:
    if not isinstance(payload, str):
        payload = json.dumps(payload, indent=2, default=str)
    out = {"content": [{"type": "text", "text": payload[:50_000]}]}
    if is_error:
        out["is_error"] = True
    return out


READONLY_GIT = {"status", "log", "diff", "show", "branch", "shortlog", "rev-parse", "worktree"}


def build_lab_server(ctx: Any):
    """ctx: triad.graph.Context (needs .root, .notebook, .kg, .config)."""
    from claude_agent_sdk import create_sdk_mcp_server, tool

    notebook, kg, root = ctx.notebook, ctx.kg, ctx.root

    @tool("paper_search", "Search arXiv, Semantic Scholar, and HF papers. Returns deduplicated JSON.",
          {"query": str, "max_results": int})
    async def paper_search(args: dict) -> dict:
        query = str(args.get("query", "")).strip()
        limit = int(args.get("max_results", 10) or 10)
        if not query:
            return _text("paper_search requires a query", is_error=True)

        def go() -> list[dict]:
            batches = []
            for source in ctx.config.search.sources:
                fn = papers_mod.SEARCHERS.get(source)
                if not fn:
                    continue
                try:
                    batches.append(fn(query, limit))
                except Exception:  # noqa: BLE001 — tolerate a down index
                    continue
            return [p.model_dump() for p in papers_mod.merge_dedupe(batches)[:limit]]

        return _text(await asyncio.to_thread(go))

    @tool("paper_fetch", "Fetch best-effort full text of an arXiv paper (ar5iv HTML).",
          {"arxiv_id": str})
    async def paper_fetch(args: dict) -> dict:
        text = await asyncio.to_thread(papers_mod.fetch_paper_text, str(args.get("arxiv_id", "")))
        return _text(text or "(no full text retrievable)")

    @tool("notebook_create",
          "Create a lab notebook entry. kind: literature|hypothesis|experiment|adr|ticket|report. "
          "fields is a JSON object of template fields.",
          {"kind": str, "title": str, "fields": str})
    async def notebook_create(args: dict) -> dict:
        try:
            fields = json.loads(args.get("fields") or "{}")
            entry_id, path = notebook.create(str(args["kind"]), str(args["title"]), fields)
            return _text({"id": entry_id, "path": str(path)})
        except Exception as exc:  # noqa: BLE001
            return _text(f"notebook_create failed: {exc}", is_error=True)

    @tool("notebook_read", "Read a notebook entry by ID (e.g. EXP-003).", {"id": str})
    async def notebook_read(args: dict) -> dict:
        try:
            return _text(notebook.read(str(args["id"])))
        except Exception as exc:  # noqa: BLE001
            return _text(f"notebook_read failed: {exc}", is_error=True)

    @tool("notebook_append", "Append a timestamped section to a notebook entry.",
          {"id": str, "heading": str, "content": str})
    async def notebook_append(args: dict) -> dict:
        try:
            notebook.append(str(args["id"]), str(args["heading"]), str(args["content"]))
            return _text("ok")
        except Exception as exc:  # noqa: BLE001
            return _text(f"notebook_append failed: {exc}", is_error=True)

    @tool("notebook_set_status", "Set the status frontmatter of a notebook entry.",
          {"id": str, "status": str})
    async def notebook_set_status(args: dict) -> dict:
        try:
            notebook.set_frontmatter(str(args["id"]), status=str(args["status"]))
            return _text("ok")
        except Exception as exc:  # noqa: BLE001
            return _text(f"notebook_set_status failed: {exc}", is_error=True)

    @tool("notebook_list", "List notebook entries of a kind with status and links.", {"kind": str})
    async def notebook_list(args: dict) -> dict:
        try:
            return _text(notebook.entries(str(args["kind"])))
        except Exception as exc:  # noqa: BLE001
            return _text(f"notebook_list failed: {exc}", is_error=True)

    @tool("kg_add_node",
          f"Add/update a knowledge-graph node. type one of: {sorted(NODE_TYPES)}",
          {"id": str, "type": str, "title": str})
    async def kg_add_node(args: dict) -> dict:
        try:
            return _text(kg.add_node(str(args["id"]), str(args["type"]), str(args["title"])))
        except Exception as exc:  # noqa: BLE001
            return _text(f"kg_add_node failed: {exc}", is_error=True)

    @tool("kg_link",
          f"Add a typed edge between knowledge-graph nodes. type one of: {sorted(EDGE_TYPES)}",
          {"src": str, "dst": str, "type": str})
    async def kg_link(args: dict) -> dict:
        try:
            return _text(kg.add_edge(str(args["src"]), str(args["dst"]), str(args["type"])))
        except Exception as exc:  # noqa: BLE001
            return _text(f"kg_link failed: {exc}", is_error=True)

    @tool("kg_query", "Search knowledge-graph nodes by substring and/or type.",
          {"q": str, "type": str})
    async def kg_query(args: dict) -> dict:
        return _text(kg.nodes(type_=str(args.get("type") or "") or None,
                              q=str(args.get("q") or "") or None))

    @tool("kg_lineage", "Evidence neighborhood of a node (papers/hypotheses/experiments/results).",
          {"id": str})
    async def kg_lineage(args: dict) -> dict:
        return _text(kg.lineage(str(args["id"])))

    @tool("repo_map", "Compact tree of the repository plus key-file excerpts.", {})
    async def repo_map_tool(args: dict) -> dict:
        tree = await asyncio.to_thread(repo_mod.repo_map, root)
        keys = await asyncio.to_thread(repo_mod.key_files_excerpt, root)
        return _text(tree + "\n\n" + keys)

    @tool("git_read", "Run a READ-ONLY git subcommand (status|log|diff|show|branch|shortlog).",
          {"args": str})
    async def git_read(args: dict) -> dict:
        parts = str(args.get("args", "")).split()
        if not parts or parts[0] not in READONLY_GIT:
            return _text(f"git_read allows only {sorted(READONLY_GIT)}", is_error=True)
        code, out = await asyncio.to_thread(repo_mod.git, root, *parts)
        return _text(out or f"(exit {code})", is_error=code != 0)

    tools = [paper_search, paper_fetch, notebook_create, notebook_read,
             notebook_append, notebook_set_status, notebook_list,
             kg_add_node, kg_link, kg_query, kg_lineage, repo_map_tool, git_read]
    return create_sdk_mcp_server(name="lab", version=__version__, tools=tools)


LAB_TOOL_NAMES = [
    "mcp__lab__paper_search", "mcp__lab__paper_fetch",
    "mcp__lab__notebook_create", "mcp__lab__notebook_read",
    "mcp__lab__notebook_append", "mcp__lab__notebook_set_status",
    "mcp__lab__notebook_list",
    "mcp__lab__kg_add_node", "mcp__lab__kg_link", "mcp__lab__kg_query",
    "mcp__lab__kg_lineage", "mcp__lab__repo_map", "mcp__lab__git_read",
]
