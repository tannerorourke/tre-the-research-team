"""Run events: a single stream feeding both the live CLI display and the
persistent events.jsonl audit log."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable, Optional

EventCallback = Callable[[dict], None]


class Emitter:
    def __init__(self, callback: Optional[EventCallback] = None, sink: Optional[Callable[[str, dict], None]] = None):
        self.callback = callback
        self.sink = sink  # (run_id, event) -> persisted

    def emit(self, run_id: str, type_: str, **data: Any) -> None:
        event = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "run": run_id,
            "type": type_,
            **data,
        }
        if self.sink:
            try:
                self.sink(run_id, event)
            except Exception:
                pass
        if self.callback:
            try:
                self.callback(event)
            except Exception:
                pass

    def child(self) -> "Emitter":
        return self
