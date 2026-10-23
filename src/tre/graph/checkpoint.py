"""Run persistence: every step checkpoints state to `.tre/runs/<run_id>/`,
so any run — including one paused at a human gate — resumes exactly where it
stopped."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


class RunStore:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.runs_dir = self.root / ".tre" / "runs"
        self.runs_dir.mkdir(parents=True, exist_ok=True)

    # -- ids ---------------------------------------------------------------

    def new_run_id(self, graph_name: str) -> str:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        base = f"{stamp}-{re.sub(r'[^a-z0-9_-]', '', graph_name.lower())}"
        run_id, n = base, 1
        while (self.runs_dir / run_id).exists():
            n += 1
            run_id = f"{base}-{n}"
        return run_id

    def dir_of(self, run_id: str) -> Path:
        return self.runs_dir / run_id

    def has(self, run_id: str) -> bool:
        return (self.dir_of(run_id) / "state.json").exists()

    # -- lifecycle ---------------------------------------------------------

    def init_run(self, run_id: str, graph_name: str, parent: Optional[str] = None) -> None:
        d = self.dir_of(run_id)
        d.mkdir(parents=True, exist_ok=True)
        self._write_json(d / "meta.json", {
            "run_id": run_id,
            "graph": graph_name,
            "parent": parent,
            "status": "running",
            "started": _now(),
        })

    def finalize(self, run_id: str, status: str, **extra: Any) -> None:
        meta = self.meta(run_id) or {}
        meta.update({"status": status, "ended": _now(), **extra})
        self._write_json(self.dir_of(run_id) / "meta.json", meta)

    def set_status(self, run_id: str, status: str, **extra: Any) -> None:
        meta = self.meta(run_id) or {}
        meta.update({"status": status, **extra})
        meta.pop("ended", None)
        self._write_json(self.dir_of(run_id) / "meta.json", meta)

    def meta(self, run_id: str) -> Optional[dict]:
        p = self.dir_of(run_id) / "meta.json"
        if not p.exists():
            return None
        return json.loads(p.read_text())

    # -- checkpoints -------------------------------------------------------

    def save_state(self, run_id: str, payload: dict) -> None:
        payload = {**payload, "saved": _now()}
        self._write_json(self.dir_of(run_id) / "state.json", payload)

    def load_state(self, run_id: str) -> dict:
        return json.loads((self.dir_of(run_id) / "state.json").read_text())

    def append_step(self, run_id: str, record: dict) -> None:
        self._append_jsonl(self.dir_of(run_id) / "steps.jsonl", {**record, "ts": _now()})

    def append_event(self, run_id: str, record: dict) -> None:
        self._append_jsonl(self.dir_of(run_id) / "events.jsonl", record)

    def steps(self, run_id: str) -> list[dict]:
        p = self.dir_of(run_id) / "steps.jsonl"
        if not p.exists():
            return []
        return [json.loads(line) for line in p.read_text().splitlines() if line.strip()]

    def list_runs(self) -> list[dict]:
        out = []
        for d in sorted(self.runs_dir.iterdir()):
            if d.is_dir():
                meta = self.meta(d.name)
                if meta:
                    out.append(meta)
        return out

    # -- io ----------------------------------------------------------------

    @staticmethod
    def _write_json(path: Path, data: dict) -> None:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2, default=str))
        tmp.replace(path)

    @staticmethod
    def _append_jsonl(path: Path, record: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as f:
            f.write(json.dumps(record, default=str) + "\n")
