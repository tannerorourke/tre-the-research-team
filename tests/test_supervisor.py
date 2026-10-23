"""Supervisor and registry against a stub worker that speaks the 'tre run'
argv and writes run state the way the real runner does.

    python -m pytest -q tests/test_supervisor.py
"""

from __future__ import annotations

import json
import os
import signal
import sys
import time
from pathlib import Path

import pytest

from tre.graph import RunStore
from tre.serve import Registry, Supervisor

STUB = r'''
import os, signal, subprocess, sys, time
from tre.graph.checkpoint import RunStore

argv = sys.argv[1:]
flag = lambda name: argv[argv.index(name) + 1] if name in argv else None
workflow, root = argv[1], flag("-p")
resume = flag("--resume")
run_id = resume or flag("--run-id")
answers = [argv[i + 1] for i, a in enumerate(argv) if a == "-a"]
store = RunStore(root)
with open(store.dir_of(run_id) / "invocations.jsonl", "a") as f:
    f.write(__import__("json").dumps(argv) + "\n")
if not resume:
    store.init_run(run_id, workflow)
    store.save_state(run_id, {"state": {}, "current": "start"})

if workflow == "gate":
    if "approve=yes" in answers:
        store.finalize(run_id, "completed")
        sys.exit(0)
    store.set_status(run_id, "waiting_gate", gate="approve", gate_message="ok?")
    sys.exit(3)
if workflow == "crash":
    if resume:
        store.finalize(run_id, "completed")
        sys.exit(0)
    os._exit(1)
if workflow == "hang":
    ignore = "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(300)"
    child = subprocess.Popen([sys.executable, "-c", ignore])
    (store.dir_of(run_id) / "child.pid").write_text(str(child.pid))
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
time.sleep(300)
'''


def wait_for(pred, timeout: float = 15.0):
    deadline = time.monotonic() + timeout
    while not (result := pred()):
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.05)
    return result


def gone(pid: int) -> bool:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except FileNotFoundError:
        return True
    return stat.rsplit(") ", 1)[1].startswith("Z")


@pytest.fixture
def lab(tmp_path):
    stub = tmp_path / "stub_worker.py"
    stub.write_text(STUB)
    root = tmp_path / "repo"
    root.mkdir()
    registry = Registry(tmp_path / "home" / "registry.json")
    make = lambda **kw: Supervisor(registry, worker=[sys.executable, str(stub)], **kw)
    yield root, registry, make
    for worker in (root / ".tre" / "runs").glob("*/worker.json"):
        pgid = json.loads(worker.read_text()).get("pgid")
        if pgid:
            try:
                os.killpg(pgid, signal.SIGKILL)
            except ProcessLookupError:
                pass


# -- a worker writes its final status just before exiting; wait for both
def settled(sup: Supervisor, run_id: str, status: str):
    return wait_for(lambda: (s := sup.status(run_id))["status"] == status
                    and s["alive"] is not True and s)


def test_registry_add_remove_persists(tmp_path):
    registry = Registry(tmp_path / "registry.json")
    a, b = tmp_path / "a", tmp_path / "b"
    registry.add(a)
    registry.add(b)
    registry.add(a)
    assert Registry(tmp_path / "registry.json").roots() == [a.resolve(), b.resolve()]
    registry.remove(a)
    assert registry.roots() == [b.resolve()]


def test_start_spawns_headless_worker_in_new_session_then_answer_resumes(lab):
    root, registry, make = lab
    sup = make()
    run_id = sup.start(root, "gate", "-dash-led question")
    assert registry.roots() == [root.resolve()]
    meta = settled(sup, run_id, "waiting_gate")
    assert (meta["gate"], meta["alive"]) == ("approve", False)

    worker = json.loads((RunStore(root).dir_of(run_id) / "worker.json").read_text())
    assert worker["pid"] and worker["pgid"] == worker["pid"]
    assert worker["argv"][-4:] == ["--run-id", run_id, "--", "-dash-led question"]
    assert "--headless" in worker["argv"]

    sup.answer(run_id, "approve", "yes")
    settled(sup, run_id, "completed")
    resumed = json.loads((RunStore(root).dir_of(run_id) / "invocations.jsonl")
                         .read_text().splitlines()[-1])
    assert resumed[-2:] == ["--resume", run_id] and "approve=yes" in resumed
    with pytest.raises(ValueError):
        sup.answer(run_id, "approve", "yes")


def test_cancel_kills_the_whole_process_group(lab):
    root, _, make = lab
    sup = make()
    run_id = sup.start(root, "hang")
    child_pid = int(wait_for(lambda: (p := RunStore(root).dir_of(run_id) / "child.pid").exists()
                             and p.read_text()))
    t0 = time.monotonic()
    sup.cancel(run_id)
    wait_for(lambda: gone(child_pid), timeout=15 - (time.monotonic() - t0))
    assert time.monotonic() - t0 < 15
    meta = sup.status(run_id)
    assert (meta["status"], meta["alive"]) == ("cancelled", False)


def test_status_marks_a_vanished_running_worker_died(lab):
    root, _, make = lab
    sup = make()
    run_id = sup.start(root, "crash")
    meta = settled(sup, run_id, "died")
    assert meta["alive"] is False
    assert "ended" in RunStore(root).meta(run_id)


def test_rescan_relaunches_died_runs_once_and_leaves_gates_waiting(lab):
    root, _, make = lab
    first = make()
    crashed = first.start(root, "crash")
    gated = first.start(root, "gate")
    settled(first, crashed, "died")
    settled(first, gated, "waiting_gate")

    second = make()
    second.rescan()
    settled(second, crashed, "completed")
    invocations = (RunStore(root).dir_of(crashed) / "invocations.jsonl").read_text().splitlines()
    assert json.loads(invocations[-1])[-2:] == ["--resume", crashed]
    assert RunStore(root).meta(gated)["status"] == "waiting_gate"
    assert len((RunStore(root).dir_of(gated) / "invocations.jsonl").read_text().splitlines()) == 1

    RunStore(root).finalize(crashed, "died")
    third = make()
    third.rescan()
    assert third.status(crashed)["status"] == "died"
    assert len((RunStore(root).dir_of(crashed) / "invocations.jsonl").read_text().splitlines()) == 2


def test_starts_past_the_cap_queue_until_a_slot_frees(lab):
    root, _, make = lab
    sup = make(max_concurrent=2)
    runs = [sup.start(root, "sleep") for _ in range(3)]
    for run_id in runs[:2]:
        wait_for(lambda: sup.status(run_id)["alive"])
    assert (sup.status(runs[2])["status"], sup.status(runs[2])["alive"]) == ("queued", False)

    sup.cancel(runs[0])
    meta = wait_for(lambda: (s := sup.status(runs[2]))["alive"] and s)
    assert meta["status"] == "running"
    assert sup.status(runs[1])["alive"]


def test_default_cap_comes_from_serve_config():
    from tre.config import TreConfig
    assert TreConfig().serve.max_concurrent_runs == 2
    assert Supervisor(Registry()).max_concurrent == 2
