"""TRE's workflow-graph engine.

Purpose-built for agent workflows: typed pydantic state with reducers,
conditional routing, bounded loops, per-node retries/timeouts, human gates,
checkpoint/resume, intra-node parallel maps, graph splicing, Mermaid export.
"""

from .checkpoint import RunStore
from .events import Emitter
from .graph import END, START, Graph, GraphError, Node
from .parallel import MapError, parallel_map, split_errors
from .runner import Context, GraphInterrupt, Runner, RunResult, run_to_completion
from .state import GraphState, REDUCERS, Replace

__all__ = [
    "Context", "Emitter", "END", "Graph", "GraphError", "GraphInterrupt",
    "GraphState", "MapError", "Node", "REDUCERS", "Replace", "Runner", "RunResult",
    "RunStore", "START", "parallel_map", "run_to_completion", "split_errors",
]
