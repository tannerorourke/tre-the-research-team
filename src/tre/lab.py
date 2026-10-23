"""Lab bootstrap: build a fully wired Context for a repository."""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional

from .config import TreConfig
from .graph import Context, Emitter, RunStore
from .memory import KnowledgeGraph, LabNotebook

# -- default runtime marker: an explicit 'runtime=None' leaves the context
#    without an agent runtime and never imports the SDK
SDK_RUNTIME = object()


def build_context(
    root: Path,
    *,
    on_event: Optional[Callable[[dict], None]] = None,
    runtime: Optional[object] = SDK_RUNTIME,
    ensure: bool = True,
) -> Context:
    root = Path(root).resolve()
    config = TreConfig.load(root)
    store = RunStore(root)

    def sink(run_id: str, event: dict) -> None:
        if run_id:
            store.append_event(run_id, event)

    emitter = Emitter(callback=on_event, sink=sink)
    notebook = LabNotebook(root / config.paths.docs)
    if ensure:
        notebook.ensure_scaffold()
    kg = KnowledgeGraph(root / config.paths.docs / "knowledge" / "graph.db")
    ctx = Context(root=root, config=config, store=store, emitter=emitter,
                  notebook=notebook, kg=kg)
    if runtime is SDK_RUNTIME:
        from .agents.runtime import SDKRuntime
        runtime = SDKRuntime(ctx)
    ctx.runtime = runtime
    return ctx
