"""Supervisor for headless 'tre run' workers across registered repositories.

Each worker runs in its own session, so 'cancel' signals the whole process
group. 'worker.json' beside a run's 'meta.json' records pid, pgid, and the
inputs that relaunch it. Runs past 'serve.max_concurrent_runs' wait as 'queued'.

    python -m pytest -q tests/test_supervisor.py
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path
from typing import Optional, Sequence

from ..config import ServeConfig
from ..graph.checkpoint import RunStore
from .registry import Registry

DEFAULT_WORKER = (sys.executable, "-m", "tre.cli")
FINAL_STATUSES = {"completed", "failed", "cancelled"}


def _pid_alive(pid: Optional[int]) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    return True


def _group_alive(pgid: int, proc: Optional[subprocess.Popen]) -> bool:
    # -- reap the leader first; an unreaped zombie still counts as a group member
    if proc:
        proc.poll()
    try:
        os.killpg(pgid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    return True


def _signal_group(pgid: int, sig: int) -> None:
    try:
        os.killpg(pgid, sig)
    except ProcessLookupError:
        pass


class Supervisor:
    """Owns headless workers. Call 'rescan' once at startup and 'poll'
    periodically so queued runs start as slots free; 'status' and 'cancel'
    poll as a side effect."""

    def __init__(self, registry: Registry, *, worker: Sequence[str] = DEFAULT_WORKER,
                 max_concurrent: int = ServeConfig().max_concurrent_runs,
                 kill_grace: float = 10.0):
        self.registry = registry
        self.worker = list(worker)
        self.max_concurrent = max_concurrent
        self.kill_grace = kill_grace
        self._roots: dict[str, Path] = {}
        self._procs: dict[str, subprocess.Popen] = {}
        self._adopted: dict[str, int] = {}   # live workers spawned by an earlier supervisor
        self._queue: deque[str] = deque()
        self._lock = threading.RLock()

    def start(self, root: Path, workflow: str, input: str = "",
              answers: Optional[dict[str, str]] = None) -> str:
        root = self.registry.add(root)
        store = RunStore(root)
        with self._lock:
            run_id = store.new_run_id(workflow)
            store.dir_of(run_id).mkdir()
            store.init_run(run_id, workflow)
            self._roots[run_id] = root
            self._write_worker(run_id, {
                "workflow": workflow, "input": input, "pid": None, "pgid": None,
                "relaunches": 0, "answers": {g: [v] for g, v in (answers or {}).items()}})
            self._enqueue(run_id)
        return run_id

    # -- the resume worker carries every answer so far; the runner's checkpoint
    #    counts how many per gate it already consumed
    def answer(self, run_id: str, gate: str, value: str) -> None:
        with self._lock:
            meta = self._store(run_id).meta(run_id)
            if meta["status"] != "waiting_gate":
                raise ValueError(f"run {run_id} is {meta['status']}, not waiting at a gate")
            worker = self._read_worker(run_id) or {
                "workflow": meta["graph"], "input": "", "relaunches": 0, "answers": {}}
            worker["answers"].setdefault(gate, []).append(value)
            self._write_worker(run_id, worker)
            self._enqueue(run_id)

    # -- SIGTERM the worker's process group, SIGKILL after 'kill_grace'
    def cancel(self, run_id: str) -> None:
        with self._lock:
            if run_id in self._queue:
                self._queue.remove(run_id)
            proc = self._procs.get(run_id)
            pgid = (self._read_worker(run_id) or {}).get("pgid")
        # -- signal and wait outside the lock so status calls stay responsive
        if pgid and _group_alive(pgid, proc):
            _signal_group(pgid, signal.SIGTERM)
            if not self._wait_group(pgid, proc, self.kill_grace):
                _signal_group(pgid, signal.SIGKILL)
                self._wait_group(pgid, proc, 5.0)
        with self._lock:
            store = self._store(run_id)
            if store.meta(run_id)["status"] not in FINAL_STATUSES:
                store.finalize(run_id, "cancelled")
            self.poll()

    # -- 'meta.json' plus 'alive'; 'alive' is None for a run no supervisor
    #    launched, whose status is reported as-is
    def status(self, run_id: str) -> dict:
        with self._lock:
            self.poll()
            return self._settle(run_id)

    # -- adopt live workers, requeue 'queued' runs, relaunch each 'died' run
    #    once from its checkpoint; 'waiting_gate' runs keep waiting
    def rescan(self) -> None:
        with self._lock:
            for root in self.registry.roots():
                if not root.is_dir():
                    continue
                for meta in RunStore(root).list_runs():
                    run_id = meta["run_id"]
                    self._roots[run_id] = root
                    worker = self._read_worker(run_id)
                    if worker is None or run_id in self._procs or run_id in self._queue:
                        continue
                    state = self._settle(run_id)
                    if state["alive"]:
                        self._adopted[run_id] = worker["pid"]
                    elif state["status"] == "queued":
                        self._queue.append(run_id)
                    elif state["status"] == "died" and worker["relaunches"] < 1:
                        self._write_worker(run_id, {**worker, "relaunches": worker["relaunches"] + 1})
                        self._enqueue(run_id)
            self.poll()

    # -- reap exited workers, then start queued runs while under the cap
    def poll(self) -> None:
        with self._lock:
            for run_id, proc in list(self._procs.items()):
                if proc.poll() is not None:
                    del self._procs[run_id]
                    self._settle(run_id)
            for run_id, pid in list(self._adopted.items()):
                if not _pid_alive(pid):
                    del self._adopted[run_id]
                    self._settle(run_id)
            while self._queue and len(self._procs) + len(self._adopted) < self.max_concurrent:
                self._spawn(self._queue.popleft())

    def _enqueue(self, run_id: str) -> None:
        self._store(run_id).set_status(run_id, "queued")
        self._queue.append(run_id)
        self.poll()

    def _spawn(self, run_id: str) -> None:
        root, store = self._roots[run_id], self._store(run_id)
        worker = self._read_worker(run_id)
        argv = [*self.worker, "run", worker["workflow"], "--headless", "-p", str(root)]
        for gate, values in worker["answers"].items():
            for value in values:
                argv += ["-a", f"{gate}={value}"]
        # -- a run with a checkpoint resumes from it; '--' keeps a dash-led input positional
        if store.has(run_id):
            argv += ["--resume", run_id]
        else:
            argv += ["--run-id", run_id, "--", worker["input"]]
        # -- set before spawning: the worker's own status writes must land after ours
        store.set_status(run_id, "running")
        with (store.dir_of(run_id) / "worker.log").open("ab") as log:
            proc = subprocess.Popen(argv, cwd=root, stdin=subprocess.DEVNULL, stdout=log,
                                    stderr=subprocess.STDOUT, start_new_session=True)
        self._procs[run_id] = proc
        self._write_worker(run_id, {**worker, "pid": proc.pid,
                                    "pgid": os.getpgid(proc.pid), "argv": argv})

    def _settle(self, run_id: str) -> dict:
        # -- probe before reading meta, so a worker that wrote its final status
        #    and exited between the two reads is never marked 'died'
        store, worker = self._store(run_id), self._read_worker(run_id)
        alive = self._alive(run_id, worker) if worker else None
        meta = store.meta(run_id)
        if meta["status"] == "running" and alive is False:
            store.finalize(run_id, "died")
            meta = store.meta(run_id)
        return {**meta, "alive": alive}

    def _alive(self, run_id: str, worker: dict) -> bool:
        proc = self._procs.get(run_id)
        if proc and proc.pid == worker["pid"]:
            return proc.poll() is None
        return _pid_alive(worker["pid"])

    @staticmethod
    def _wait_group(pgid: int, proc: Optional[subprocess.Popen], timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while _group_alive(pgid, proc):
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.1)
        return True

    def _root_of(self, run_id: str) -> Path:
        if run_id not in self._roots:
            for root in self.registry.roots():
                if (root / ".tre" / "runs" / run_id).is_dir():
                    self._roots[run_id] = root
                    break
            else:
                raise KeyError(f"no registered repository has run {run_id}")
        return self._roots[run_id]

    def _store(self, run_id: str) -> RunStore:
        return RunStore(self._root_of(run_id))

    def _read_worker(self, run_id: str) -> Optional[dict]:
        path = self._store(run_id).dir_of(run_id) / "worker.json"
        return json.loads(path.read_text()) if path.exists() else None

    def _write_worker(self, run_id: str, record: dict) -> None:
        path = self._store(run_id).dir_of(run_id) / "worker.json"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(record, indent=2))
        tmp.replace(path)
