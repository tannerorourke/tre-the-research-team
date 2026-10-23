"""The lab's thirteen tools as plain async functions '(ctx, args) -> dict'.

Each returns an MCP-shaped result '{"content": [...], "is_error"?: True}'.
'tools.server' registers them for the SDK's in-process server, and
'serve.daemon' registers them per repository on the HTTP daemon.

    python -m pytest -q tests/test_daemon.py
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from ..memory import EDGE_TYPES, NODE_TYPES
from . import papers as papers_mod
from . import repo as repo_mod

READONLY_GIT = {"status", "log", "diff", "show", "branch", "shortlog", "rev-parse", "worktree"}


def _text(payload: Any, *, is_error: bool = False) -> dict:
    if not isinstance(payload, str):
        payload = json.dumps(payload, indent=2, default=str)
    out = {"content": [{"type": "text", "text": payload[:50_000]}]}
    if is_error:
        out["is_error"] = True
    return out


async def paper_search(ctx: Any, args: dict) -> dict:
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


async def paper_fetch(ctx: Any, args: dict) -> dict:
    text = await asyncio.to_thread(papers_mod.fetch_paper_text, str(args.get("arxiv_id", "")))
    return _text(text or "(no full text retrievable)")


async def notebook_create(ctx: Any, args: dict) -> dict:
    try:
        fields = json.loads(args.get("fields") or "{}")
        entry_id, path = ctx.notebook.create(str(args["kind"]), str(args["title"]), fields)
        return _text({"id": entry_id, "path": str(path)})
    except Exception as exc:  # noqa: BLE001
        return _text(f"notebook_create failed: {exc}", is_error=True)


async def notebook_read(ctx: Any, args: dict) -> dict:
    try:
        return _text(ctx.notebook.read(str(args["id"])))
    except Exception as exc:  # noqa: BLE001
        return _text(f"notebook_read failed: {exc}", is_error=True)


async def notebook_append(ctx: Any, args: dict) -> dict:
    try:
        ctx.notebook.append(str(args["id"]), str(args["heading"]), str(args["content"]))
        return _text("ok")
    except Exception as exc:  # noqa: BLE001
        return _text(f"notebook_append failed: {exc}", is_error=True)


async def notebook_set_status(ctx: Any, args: dict) -> dict:
    try:
        ctx.notebook.set_frontmatter(str(args["id"]), status=str(args["status"]))
        return _text("ok")
    except Exception as exc:  # noqa: BLE001
        return _text(f"notebook_set_status failed: {exc}", is_error=True)


async def notebook_list(ctx: Any, args: dict) -> dict:
    try:
        return _text(ctx.notebook.entries(str(args["kind"])))
    except Exception as exc:  # noqa: BLE001
        return _text(f"notebook_list failed: {exc}", is_error=True)


async def kg_add_node(ctx: Any, args: dict) -> dict:
    try:
        return _text(ctx.kg.add_node(str(args["id"]), str(args["type"]), str(args["title"])))
    except Exception as exc:  # noqa: BLE001
        return _text(f"kg_add_node failed: {exc}", is_error=True)


async def kg_link(ctx: Any, args: dict) -> dict:
    try:
        return _text(ctx.kg.add_edge(str(args["src"]), str(args["dst"]), str(args["type"])))
    except Exception as exc:  # noqa: BLE001
        return _text(f"kg_link failed: {exc}", is_error=True)


async def kg_query(ctx: Any, args: dict) -> dict:
    return _text(ctx.kg.nodes(type_=str(args.get("type") or "") or None,
                              q=str(args.get("q") or "") or None))


async def kg_lineage(ctx: Any, args: dict) -> dict:
    return _text(ctx.kg.lineage(str(args["id"])))


async def repo_map(ctx: Any, args: dict) -> dict:
    tree = await asyncio.to_thread(repo_mod.repo_map, ctx.root)
    keys = await asyncio.to_thread(repo_mod.key_files_excerpt, ctx.root)
    return _text(tree + "\n\n" + keys)


async def git_read(ctx: Any, args: dict) -> dict:
    parts = str(args.get("args", "")).split()
    if not parts or parts[0] not in READONLY_GIT:
        return _text(f"git_read allows only {sorted(READONLY_GIT)}", is_error=True)
    code, out = await asyncio.to_thread(repo_mod.git, ctx.root, *parts)
    return _text(out or f"(exit {code})", is_error=code != 0)


@dataclass(frozen=True)
class LabTool:
    name: str
    description: str
    params: dict[str, type]
    fn: Callable[[Any, dict], Awaitable[dict]]


LAB_TOOLS = [
    LabTool("paper_search", "Search arXiv, Semantic Scholar, and HF papers. Returns deduplicated JSON.",
            {"query": str, "max_results": int}, paper_search),
    LabTool("paper_fetch", "Fetch best-effort full text of an arXiv paper (ar5iv HTML).",
            {"arxiv_id": str}, paper_fetch),
    LabTool("notebook_create",
            "Create a lab notebook entry. kind: literature|hypothesis|experiment|adr|ticket|report. "
            "fields is a JSON object of template fields.",
            {"kind": str, "title": str, "fields": str}, notebook_create),
    LabTool("notebook_read", "Read a notebook entry by ID (e.g. EXP-003).", {"id": str}, notebook_read),
    LabTool("notebook_append", "Append a timestamped section to a notebook entry.",
            {"id": str, "heading": str, "content": str}, notebook_append),
    LabTool("notebook_set_status", "Set the status frontmatter of a notebook entry.",
            {"id": str, "status": str}, notebook_set_status),
    LabTool("notebook_list", "List notebook entries of a kind with status and links.",
            {"kind": str}, notebook_list),
    LabTool("kg_add_node", f"Add/update a knowledge-graph node. type one of: {sorted(NODE_TYPES)}",
            {"id": str, "type": str, "title": str}, kg_add_node),
    LabTool("kg_link",
            f"Add a typed edge between knowledge-graph nodes. type one of: {sorted(EDGE_TYPES)}",
            {"src": str, "dst": str, "type": str}, kg_link),
    LabTool("kg_query", "Search knowledge-graph nodes by substring and/or type.",
            {"q": str, "type": str}, kg_query),
    LabTool("kg_lineage", "Evidence neighborhood of a node (papers/hypotheses/experiments/results).",
            {"id": str}, kg_lineage),
    LabTool("repo_map", "Compact tree of the repository plus key-file excerpts.", {}, repo_map),
    LabTool("git_read", "Run a READ-ONLY git subcommand (status|log|diff|show|branch|shortlog).",
            {"args": str}, git_read),
]
