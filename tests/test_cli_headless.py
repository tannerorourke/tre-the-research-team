from __future__ import annotations

import json
import re
import subprocess

import pytest
from typer.testing import CliRunner

import tre.cli as cli
from tre.graph import RunStore

from conftest import make_ctx

EXIT_LINE = re.compile(r"^\[tre\] (\S+) exit (\d+) status=(\S+) gate=(\S+)$")

runner = CliRunner()


@pytest.fixture
def lab(tmp_path, monkeypatch):
    def fake_build_context(root, *, on_event=None, runtime=None, ensure=True):
        return make_ctx(tmp_path, gates=True)
    monkeypatch.setattr(cli, "build_context", fake_build_context)
    return tmp_path


def invoke(*args: str):
    result = runner.invoke(cli.app, list(args))
    last = result.output.strip().splitlines()[-1]
    match = EXIT_LINE.match(last)
    assert match, f"no exit line in: {result.output!r}"
    run_id, code, status, gate = match.groups()
    assert int(code) == result.exit_code
    return result, run_id, status, gate


def test_headless_gate_exits_3_and_records_message(lab):
    result, run_id, status, gate = invoke("run", "design", "q", "-p", str(lab), "--headless")
    assert result.exit_code == 3
    assert (status, gate) == ("waiting_gate", "approve_design")
    meta = RunStore(lab).meta(run_id)
    assert meta["status"] == "waiting_gate"
    assert meta["gate"] == "approve_design"
    assert meta["gate_message"]


def test_headless_resume_with_answer_exits_0(lab):
    _, run_id, _, _ = invoke("run", "design", "q", "-p", str(lab), "--headless")
    result, resumed, status, gate = invoke(
        "run", "design", "-p", str(lab), "--headless",
        "--resume", run_id, "--answer", "approve_design=yes")
    assert result.exit_code == 0
    assert (resumed, status, gate) == (run_id, "completed", "-")


def test_interactive_run_prints_exit_line(lab):
    result, run_id, status, gate = invoke("run", "design", "q", "-p", str(lab), "--yes")
    assert result.exit_code == 0
    assert (status, gate) == ("completed", "-")
    assert run_id.endswith("-design")


def test_failed_run_exits_1(lab):
    result, run_id, status, gate = invoke("run", "literature", "", "-p", str(lab), "--headless")
    assert result.exit_code == 1
    assert (run_id, status, gate) == ("-", "-", "-")
    result, *_ = invoke("run", "nope", "-p", str(lab))
    assert result.exit_code == 1


def test_wait_maps_status_to_exit_code(lab):
    _, gated, _, _ = invoke("run", "design", "q", "-p", str(lab), "--headless")
    result, run_id, status, gate = invoke("wait", gated, "-p", str(lab))
    assert result.exit_code == 3
    assert (run_id, status, gate) == (gated, "waiting_gate", "approve_design")

    store = RunStore(lab)
    store.init_run("r-failed", "design", None)
    store.finalize("r-failed", "failed", error="boom")
    result, _, status, _ = invoke("wait", "r-failed", "-p", str(lab))
    assert (result.exit_code, status) == (1, "failed")

    result, _, status, _ = invoke("wait", "r-missing", "-p", str(lab))
    assert (result.exit_code, status) == (1, "-")


def test_wait_polls_until_run_leaves_running(lab, monkeypatch):
    store = RunStore(lab)
    store.init_run("r-live", "design", None)
    store.set_status("r-live", "running")
    polls = []

    def fake_sleep(seconds):
        polls.append(seconds)
        if len(polls) == 2:
            store.finalize("r-live", "completed")
    monkeypatch.setattr(cli.time, "sleep", fake_sleep)
    result, _, status, gate = invoke("wait", "r-live", "-p", str(lab), "--poll", "0.5")
    assert result.exit_code == 0
    assert (status, gate) == ("completed", "-")
    assert polls == [0.5, 0.5]


def test_status_json_prints_meta(lab):
    _, run_id, _, _ = invoke("run", "design", "q", "-p", str(lab), "--headless")
    result = runner.invoke(cli.app, ["status", run_id, "-p", str(lab), "--json"])
    assert result.exit_code == 0
    meta = json.loads(result.output)
    assert meta == RunStore(lab).meta(run_id)
    assert meta["gate"] == "approve_design"


def test_run_id_names_a_fresh_run(lab):
    result, run_id, status, _ = invoke("run", "design", "-p", str(lab), "--headless",
                                       "--run-id", "r-preallocated", "--", "q")
    assert (result.exit_code, run_id, status) == (3, "r-preallocated", "waiting_gate")


def test_wait_keeps_polling_through_queued(lab, monkeypatch):
    store = RunStore(lab)
    store.init_run("r-queued", "design", None)
    store.set_status("r-queued", "queued")
    polls = []

    def fake_sleep(seconds):
        polls.append(seconds)
        if len(polls) == 1:
            store.set_status("r-queued", "running")
        if len(polls) == 3:
            store.finalize("r-queued", "completed")
    monkeypatch.setattr(cli.time, "sleep", fake_sleep)
    result, _, status, _ = invoke("wait", "r-queued", "-p", str(lab), "--poll", "0.1")
    assert (result.exit_code, status, len(polls)) == (0, "completed", 3)


def test_wait_ends_on_a_dead_worker_pid_without_writing(lab, monkeypatch):
    store = RunStore(lab)
    store.init_run("r-dead", "design", None)
    store.set_status("r-dead", "running")
    dead = subprocess.Popen(["true"])
    dead.wait()
    (store.dir_of("r-dead") / "worker.json").write_text(json.dumps({"pid": dead.pid, "pgid": dead.pid}))
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)
    result, run_id, status, gate = invoke("wait", "r-dead", "-p", str(lab), "--poll", "0.1")
    assert (result.exit_code, run_id, status, gate) == (1, "r-dead", "died", "-")
    assert store.meta("r-dead")["status"] == "running"
