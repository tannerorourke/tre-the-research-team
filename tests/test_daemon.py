"""The MCP daemon over real streamable HTTP on an ephemeral loopback port,
driven by the 'mcp' client, with a stub worker in place of 'tre run'; the
stdio server as a spawned subprocess, proxying or supervising in-process.

    python -m pytest -q tests/test_daemon.py
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import socket
import sys
import threading
import time

import pytest
from mcp import Client, StdioServerParameters
from typer.testing import CliRunner

from tre.cli import app
from tre.serve import Registry, Supervisor
from tre.serve.daemon import Daemon, daemon_listening, load_serve_config

STUB = r'''
import sys, time
from tre.graph.checkpoint import RunStore

argv = sys.argv[1:]
flag = lambda name: argv[argv.index(name) + 1] if name in argv else None
workflow, root, resume = argv[1], flag("-p"), flag("--resume")
run_id = resume or flag("--run-id")
store = RunStore(root)
if not resume:
    store.init_run(run_id, workflow)
    store.save_state(run_id, {"state": {}, "current": "start"})
store.append_event(run_id, {"type": "node_start", "node": "start", "resume": bool(resume)})
if workflow == "gate":
    if "approve=yes" in [argv[i + 1] for i, a in enumerate(argv) if a == "-a"]:
        store.finalize(run_id, "completed")
        sys.exit(0)
    store.set_status(run_id, "waiting_gate", gate="approve", gate_message="ship it?")
    sys.exit(3)
time.sleep(300)
'''


@pytest.fixture
def daemon(tmp_path):
    stub = tmp_path / "stub_worker.py"
    stub.write_text(STUB)
    repo = tmp_path / "repo"
    repo.mkdir()
    registry = Registry(tmp_path / "home" / "registry.json")
    supervisor = Supervisor(registry, worker=[sys.executable, str(stub)])
    daemon = Daemon(registry, supervisor, workflows={"gate", "sleep"})

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = daemon.http_server("127.0.0.1", port)
    thread = threading.Thread(
        target=lambda: asyncio.run(daemon.serve(server, sockets=[sock], poll_interval=0.1)),
        daemon=True)
    thread.start()
    wait_for(lambda: server.started)
    yield f"http://127.0.0.1:{port}/mcp", repo
    server.should_exit = True
    thread.join(10)
    for worker in (repo / ".tre" / "runs").glob("*/worker.json"):
        pgid = json.loads(worker.read_text()).get("pgid")
        if pgid:
            try:
                os.killpg(pgid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def port_of(url: str) -> int:
    return int(url.rsplit(":", 1)[1].split("/")[0])


# -- HOME is redirected so the subprocess reads and writes a scratch registry
def stdio_server(tmp_path, port: int) -> StdioServerParameters:
    return StdioServerParameters(
        command=sys.executable, args=["-m", "tre.cli", "serve", "--stdio", "--port", str(port)],
        env={**os.environ, "HOME": str(tmp_path / "home")})


def wait_for(pred, timeout: float = 15.0):
    deadline = time.monotonic() + timeout
    while not (result := pred()):
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.05)
    return result


def data(result):
    assert not result.is_error, result.content[0].text
    payload = result.structured_content
    return payload["result"] if set(payload) == {"result"} else payload


async def resource(client: Client, uri: str):
    return json.loads((await client.read_resource(uri)).contents[0].text)


async def settled(client: Client, run_id: str, status: str) -> dict:
    deadline = time.monotonic() + 15
    while True:
        meta = data(await client.call_tool("run_status", {"run_id": run_id}))
        if meta["status"] == status and meta["alive"] is not True:
            return meta
        assert time.monotonic() < deadline, f"run {run_id} stuck at {meta['status']}"
        await asyncio.sleep(0.05)


async def test_repo_register_lists_through_tool_and_resource(daemon):
    url, repo = daemon
    async with Client(url) as client:
        assert data(await client.call_tool("repo_register", {"path": str(repo)})) == {"repo": str(repo)}
        assert data(await client.call_tool("repo_list", {})) == [{"repo": str(repo), "exists": True}]
        assert await resource(client, "tre://repos") == [str(repo)]
        missing = await client.call_tool("repo_register", {"path": str(repo / "nope")})
        assert missing.is_error


async def test_run_start_with_stub_worker_reaches_the_gate(daemon):
    url, repo = daemon
    async with Client(url) as client:
        await client.call_tool("repo_register", {"path": str(repo)})
        started = data(await client.call_tool(
            "run_start", {"repo": str(repo), "workflow": "gate", "input": "q"}))
        run_id = started["run_id"]
        assert started["wait"] == f"tre wait {run_id} -p {repo}"
        meta = await settled(client, run_id, "waiting_gate")
        assert (meta["gate"], meta["gate_message"]) == ("approve", "ship it?")
        listed = data(await client.call_tool("run_list", {"repo": str(repo)}))
        assert [m["run_id"] for m in listed] == [run_id]


async def test_gates_resource_lists_every_waiting_run_with_its_message(daemon):
    url, repo = daemon
    async with Client(url) as client:
        await client.call_tool("repo_register", {"path": str(repo)})
        run_id = data(await client.call_tool(
            "run_start", {"repo": str(repo), "workflow": "gate"}))["run_id"]
        await settled(client, run_id, "waiting_gate")
        assert await resource(client, "tre://gates") == [{
            "run_id": run_id, "repo": str(repo), "graph": "gate",
            "gate": "approve", "gate_message": "ship it?"}]


async def test_run_answer_resumes_the_run_to_completion(daemon):
    url, repo = daemon
    async with Client(url) as client:
        await client.call_tool("repo_register", {"path": str(repo)})
        run_id = data(await client.call_tool(
            "run_start", {"repo": str(repo), "workflow": "gate"}))["run_id"]
        await settled(client, run_id, "waiting_gate")
        await client.call_tool("run_answer", {"run_id": run_id, "gate": "approve", "value": "yes"})
        await settled(client, run_id, "completed")
        assert await resource(client, "tre://gates") == []
        again = await client.call_tool("run_answer", {"run_id": run_id, "gate": "approve", "value": "yes"})
        assert again.is_error and "not waiting at a gate" in again.content[0].text

        tail = (await client.read_resource(f"tre://runs/{run_id}/events?tail=1")).contents[0].text
        assert json.loads(tail)["resume"] is True


async def test_run_start_refuses_unknown_workflows_and_unregistered_repos(daemon):
    url, repo = daemon
    async with Client(url) as client:
        unregistered = await client.call_tool("run_start", {"repo": str(repo), "workflow": "gate"})
        assert unregistered.is_error and "repo_register" in unregistered.content[0].text
        await client.call_tool("repo_register", {"path": str(repo)})
        unknown = await client.call_tool("run_start", {"repo": str(repo), "workflow": "nope"})
        assert unknown.is_error and "unknown workflow" in unknown.content[0].text


async def test_run_cancel_stops_a_live_worker(daemon):
    url, repo = daemon
    async with Client(url) as client:
        await client.call_tool("repo_register", {"path": str(repo)})
        run_id = data(await client.call_tool(
            "run_start", {"repo": str(repo), "workflow": "sleep"}))["run_id"]
        assert data(await client.call_tool("run_status", {"run_id": run_id}))["alive"]
        meta = data(await client.call_tool("run_cancel", {"run_id": run_id}))
        assert (meta["status"], meta["alive"]) == ("cancelled", False)


async def test_lab_tools_route_to_the_named_repository(daemon):
    url, repo = daemon
    async with Client(url) as client:
        names = {t.name for t in (await client.list_tools()).tools}
        assert {"kg_add_node", "kg_query", "git_read", "repo_map", "costs", "standup"} <= names
        await client.call_tool("repo_register", {"path": str(repo)})
        added = await client.call_tool("kg_add_node", {"repo": str(repo), "id": "PAPER-1",
                                                       "type": "paper", "title": "Attention"})
        assert not added.is_error, added.content[0].text
        found = await client.call_tool("kg_query", {"repo": str(repo), "q": "Attention", "type": ""})
        assert [n["id"] for n in json.loads(found.content[0].text)] == ["PAPER-1"]
    assert (repo / "docs" / "knowledge" / "graph.db").exists()


def test_serve_refuses_a_non_loopback_host():
    result = CliRunner().invoke(app, ["serve", "--host", "0.0.0.0", "--port", "8799"])
    assert result.exit_code == 2


def test_global_cap_comes_from_the_user_config(tmp_path):
    config = tmp_path / "config.toml"
    assert load_serve_config(config).max_concurrent_runs == 2
    config.write_text("[serve]\nmax_concurrent_runs = 5\n")
    assert load_serve_config(config).max_concurrent_runs == 5


def test_daemon_listening_probes_the_port(daemon):
    url, _ = daemon
    assert daemon_listening("127.0.0.1", port_of(url))
    assert not daemon_listening("127.0.0.1", free_port())


async def test_stdio_proxies_to_a_listening_daemon(daemon, tmp_path):
    url, repo = daemon
    async with Client(stdio_server(tmp_path, port_of(url))) as proxy, Client(url) as direct:
        proxied = {t.name for t in (await proxy.list_tools()).tools}
        assert proxied == {t.name for t in (await direct.list_tools()).tools}
        assert data(await proxy.call_tool("repo_register", {"path": str(repo)})) == {"repo": str(repo)}
        assert data(await direct.call_tool("repo_list", {})) == [{"repo": str(repo), "exists": True}]
        assert await resource(proxy, "tre://repos") == [str(repo)]
        assert await resource(proxy, "tre://gates") == []
        missing = await proxy.call_tool("run_start", {"repo": str(repo / "nope"), "workflow": "gate"})
        assert missing.is_error and "repo_register" in missing.content[0].text


async def test_stdio_supervises_in_process_when_no_daemon_listens(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    async with Client(stdio_server(tmp_path, free_port())) as client:
        assert "run_start" in {t.name for t in (await client.list_tools()).tools}
        assert data(await client.call_tool("repo_register", {"path": str(repo)})) == {"repo": str(repo)}
        assert await resource(client, "tre://repos") == [str(repo)]
    registry = json.loads((tmp_path / "home" / ".tre" / "registry.json").read_text())
    assert registry["roots"] == [str(repo)]
