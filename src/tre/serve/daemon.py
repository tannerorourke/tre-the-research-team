"""The TRE MCP server: a supervisor over on-disk runs, on loopback HTTP or stdio.

Exposes the lab tools per registered repository (each takes 'repo'), the run
lifecycle through the 'Supervisor', and read-only resources for repositories,
open gates, and run events. Run state lives on disk; the process holds pids,
the registry, and one lazily built 'Context' per repository.

One supervisor per machine: a stdio server first probes the HTTP port and, when
a daemon answers, proxies every request to it instead of supervising itself.

    tre serve [--host 127.0.0.1] [--port 8765]
    tre serve --stdio
    python -m pytest -q tests/test_daemon.py
"""

from __future__ import annotations

import asyncio
import inspect
import ipaddress
import json
import socket
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Collection, Optional, Sequence

import anyio
import uvicorn
from mcp import Client
from mcp.server.lowlevel import Server
from mcp.server.mcpserver import MCPServer
from mcp.server.stdio import stdio_server
from mcp.server.mcpserver.exceptions import ResourceNotFoundError, ToolError
from mcp.types import CallToolResult, TextContent

from .. import __version__
from ..config import ServeConfig, tomllib
from ..graph import Context, RunStore
from ..lab import build_context
from ..tools.lab_tools import LAB_TOOLS, LabTool
from .registry import Registry
from .supervisor import DEFAULT_WORKER, Supervisor

USER_CONFIG = Path.home() / ".tre" / "config.toml"

INSTRUCTIONS = (
    "TRE runs typed research workflows on registered repositories. Call repo_register "
    "on a repository root first; every lab tool takes that root as 'repo'. run_start returns "
    "a run id; block on 'tre wait <run_id> -p <repo>', then read run_status. A run at "
    "status waiting_gate carries gate and gate_message; answer it with run_answer."
)


def is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def daemon_listening(host: str, port: int, timeout: float = 0.5) -> bool:
    with socket.socket() as sock:
        sock.settimeout(timeout)
        return sock.connect_ex((host, port)) == 0


# -- resources are read with the cache bypassed: 'tre://gates' changes under
#    the client, and the daemon is on the same machine
async def proxy_stdio(url: str) -> None:
    async with Client(url) as client:
        server: Server = Server(
            "tre", version=__version__, instructions=client.instructions,
            on_list_tools=lambda ctx, params: client.list_tools(),
            on_call_tool=lambda ctx, params: client.call_tool(params.name, params.arguments),
            on_list_resources=lambda ctx, params: client.list_resources(cache_mode="bypass"),
            on_list_resource_templates=lambda ctx, params: client.list_resource_templates(),
            on_read_resource=lambda ctx, params: client.read_resource(params.uri, cache_mode="bypass"),
        )
        async with stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())


# -- the cap spans repositories, so it comes from the user-level config and
#    never from a repository's own '.tre/config.toml'
def load_serve_config(path: Path = USER_CONFIG) -> ServeConfig:
    if not path.exists() or tomllib is None:
        return ServeConfig()
    with path.open("rb") as f:
        return ServeConfig.model_validate(tomllib.load(f).get("serve", {}))


def _result(payload: dict) -> CallToolResult:
    content = [TextContent(type="text", text=block["text"]) for block in payload["content"]]
    return CallToolResult(content=content, is_error=bool(payload.get("is_error")))


class Daemon:
    """Owns the 'MCPServer'. 'workflows' bounds what 'run_start' accepts;
    None means every workflow the package ships."""

    def __init__(self, registry: Registry, supervisor: Supervisor, *,
                 workflows: Optional[Collection[str]] = None):
        if workflows is None:
            from ..workflows import WORKFLOWS
            workflows = WORKFLOWS
        self.registry, self.supervisor = registry, supervisor
        self.workflows = set(workflows)
        self._contexts: dict[Path, Context] = {}
        self.mcp = MCPServer("tre", version=__version__, instructions=INSTRUCTIONS)
        for spec in LAB_TOOLS:
            self.mcp.add_tool(self._lab_handler(spec), name=spec.name,
                              description=f"{spec.description} 'repo' is a registered repository root.")
        self._register_run_tools()
        self._register_resources()

    def root(self, repo: str) -> Path:
        root = Path(repo).expanduser().resolve()
        if root not in self.registry.roots():
            raise ToolError(f"{root} is not registered; call repo_register first")
        return root

    # -- built on the event-loop thread, which owns the knowledge graph's
    #    sqlite connection; 'runtime=None' keeps the SDK out of the daemon
    def context(self, repo: str) -> Context:
        root = self.root(repo)
        if root not in self._contexts:
            self._contexts[root] = build_context(root, ensure=False, runtime=None)
        return self._contexts[root]

    def run_root(self, run_id: str) -> Path:
        for root in self.registry.roots():
            if (RunStore(root).dir_of(run_id) / "meta.json").exists():
                return root
        raise ResourceNotFoundError(f"no registered repository has run {run_id}")

    def runs(self, repo: str = "") -> list[dict]:
        roots = [self.root(repo)] if repo else [r for r in self.registry.roots() if r.is_dir()]
        return [{**meta, "repo": str(root)} for root in roots for meta in RunStore(root).list_runs()]

    # -- the SDK derives a tool's input schema from its signature, so each lab
    #    tool gets one built from its param spec, every param required
    def _lab_handler(self, spec: LabTool):
        async def handler(repo: str, **args: Any) -> CallToolResult:
            return _result(await spec.fn(self.context(repo), args))

        params = [inspect.Parameter("repo", inspect.Parameter.KEYWORD_ONLY, annotation=str)]
        params += [inspect.Parameter(name, inspect.Parameter.KEYWORD_ONLY, annotation=kind)
                   for name, kind in spec.params.items()]
        handler.__signature__ = inspect.Signature(params, return_annotation=CallToolResult)
        handler.__name__ = spec.name
        return handler

    async def _supervise(self, fn, *args) -> Any:
        try:
            return await asyncio.to_thread(fn, *args)
        except (KeyError, ValueError) as exc:
            raise ToolError(exc.args[0] if exc.args else str(exc)) from exc

    def _register_run_tools(self) -> None:
        mcp, sup = self.mcp, self.supervisor

        @mcp.tool(description="Attach a repository root to the daemon. Idempotent.")
        def repo_register(path: str) -> dict[str, Any]:
            root = Path(path).expanduser().resolve()
            if not root.is_dir():
                raise ToolError(f"{root} is not a directory")
            return {"repo": str(self.registry.add(root))}

        @mcp.tool(description="List registered repository roots.")
        def repo_list() -> list[dict[str, Any]]:
            return [{"repo": str(r), "exists": r.is_dir()} for r in self.registry.roots()]

        @mcp.tool(description="Start a workflow run as a headless worker. 'answers' pre-answers "
                              "gates by name. Returns the run id and the command that waits on it.")
        async def run_start(repo: str, workflow: str, input: str = "",
                            answers: Optional[dict[str, str]] = None) -> dict[str, Any]:
            root = self.root(repo)
            if workflow not in self.workflows:
                raise ToolError(f"unknown workflow {workflow!r}; one of {sorted(self.workflows)}")
            run_id = await self._supervise(sup.start, root, workflow, input, answers)
            return {"run_id": run_id, "repo": str(root),
                    "wait": f"tre wait {run_id} -p {root}"}

        @mcp.tool(description="A run's meta.json plus 'alive'; a vanished worker reads 'died'.")
        async def run_status(run_id: str) -> dict[str, Any]:
            return await self._supervise(sup.status, run_id)

        @mcp.tool(description="Answer a run waiting at a gate; starts the resume worker.")
        async def run_answer(run_id: str, gate: str, value: str) -> dict[str, Any]:
            await self._supervise(sup.answer, run_id, gate, value)
            return await self._supervise(sup.status, run_id)

        @mcp.tool(description="Cancel a run: SIGTERM its process group, SIGKILL after a grace period.")
        async def run_cancel(run_id: str) -> dict[str, Any]:
            await self._supervise(sup.cancel, run_id)
            return await self._supervise(sup.status, run_id)

        @mcp.tool(description="List runs in one repository, or across all when 'repo' is empty.")
        def run_list(repo: str = "") -> list[dict[str, Any]]:
            return self.runs(repo)

        @mcp.tool(description="Aggregate recorded spend by workflow, for one repository or all.")
        def costs(repo: str = "") -> dict[str, Any]:
            by_graph: dict[str, float] = {}
            for meta in self.runs(repo):
                cost = meta.get("cost_usd")
                if isinstance(cost, (int, float)):
                    by_graph[meta.get("graph", "?")] = by_graph.get(meta.get("graph", "?"), 0.0) + cost
            return {"by_graph": by_graph, "total": sum(by_graph.values())}

        @mcp.tool(description="Generate and save today's standup for a repository from runs and notebook state.")
        def standup(repo: str) -> dict[str, Any]:
            from ..memory.standup import save_standup, standup_text
            ctx = self.context(repo)
            return {"text": standup_text(ctx), "path": save_standup(ctx)}

    def _register_resources(self) -> None:
        mcp = self.mcp

        @mcp.resource("tre://repos", mime_type="application/json",
                      description="Registered repository roots.")
        def repos() -> str:
            return json.dumps([str(r) for r in self.registry.roots()])

        @mcp.resource("tre://gates", mime_type="application/json",
                      description="Every run waiting at a gate, across repositories, with its message.")
        def gates() -> str:
            return json.dumps([
                {k: meta.get(k) for k in ("run_id", "repo", "graph", "gate", "gate_message")}
                for meta in self.runs() if meta.get("status") == "waiting_gate"])

        @mcp.resource("tre://runs/{run_id}/events{?tail}", mime_type="application/x-ndjson",
                      description="The last 'tail' lines of a run's events.jsonl.")
        def events(run_id: str, tail: int = 50) -> str:
            path = RunStore(self.run_root(run_id)).dir_of(run_id) / "events.jsonl"
            lines = path.read_text().splitlines() if path.exists() else []
            return "\n".join(lines[-tail:] if tail > 0 else [])

    def http_server(self, host: str, port: int) -> uvicorn.Server:
        app = self.mcp.streamable_http_app(host=host)
        return uvicorn.Server(uvicorn.Config(app, host=host, port=port, log_level="warning"))

    # -- polling starts queued runs as slots free and reaps exited workers
    @asynccontextmanager
    async def polling(self, interval: float = 2.0) -> AsyncIterator[None]:
        async def poll() -> None:
            while True:
                await anyio.to_thread.run_sync(self.supervisor.poll)
                await anyio.sleep(interval)

        async with anyio.create_task_group() as tg:
            tg.start_soon(poll)
            yield
            tg.cancel_scope.cancel()

    async def serve(self, server: uvicorn.Server, *, sockets: Optional[Sequence] = None,
                    poll_interval: float = 2.0) -> None:
        async with self.polling(poll_interval):
            await server.serve(sockets=sockets)

    async def serve_stdio(self, *, poll_interval: float = 2.0) -> None:
        async with self.polling(poll_interval):
            await self.mcp.run_stdio_async()


def run(host: str, port: int, *, stdio: bool = False, registry: Optional[Registry] = None,
        worker: Sequence[str] = DEFAULT_WORKER, config_path: Path = USER_CONFIG) -> None:
    url = f"http://{host}:{port}/mcp"
    if stdio and daemon_listening(host, port):
        print(f"tre serve --stdio: proxying {url}", file=sys.stderr)
        anyio.run(proxy_stdio, url)
        return
    registry = registry or Registry()
    supervisor = Supervisor(registry, worker=worker,
                            max_concurrent=load_serve_config(config_path).max_concurrent_runs)
    supervisor.rescan()
    daemon = Daemon(registry, supervisor)
    if stdio:
        print("tre serve --stdio: supervising in-process", file=sys.stderr)
        anyio.run(daemon.serve_stdio)
    else:
        anyio.run(daemon.serve, daemon.http_server(host, port))
