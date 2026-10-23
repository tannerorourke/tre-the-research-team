"""Typed graph state with reducer semantics.

A workflow's state is a pydantic model. Nodes return *partial updates*
(plain dicts); the engine folds them into state through per-field reducers,
so loops and map steps can accumulate rather than clobber.

Reducers (declared in ``__reducers__ = {"field": "name"}``):
    replace    new value wins (default)
    extend     list concatenation
    merge      dict update
    sum_merge  dict update, numeric values added on key collision
    max        keep the larger value
"""

from __future__ import annotations

from typing import Any, Callable, ClassVar

from pydantic import BaseModel, Field


class GraphError(RuntimeError):
    """Structural or execution error in a workflow graph."""


def _replace(old: Any, new: Any) -> Any:
    return new


def _extend(old: Any, new: Any) -> Any:
    if new is None:
        return old
    return [*(old or []), *new]


def _merge(old: Any, new: Any) -> Any:
    out = dict(old or {})
    out.update(new or {})
    return out


def _sum_merge(old: Any, new: Any) -> Any:
    out = dict(old or {})
    for k, v in (new or {}).items():
        out[k] = out.get(k, 0) + v
    return out


def _max(old: Any, new: Any) -> Any:
    if old is None:
        return new
    if new is None:
        return old
    return max(old, new)


REDUCERS: dict[str, Callable[[Any, Any], Any]] = {
    "replace": _replace,
    "extend": _extend,
    "merge": _merge,
    "sum_merge": _sum_merge,
    "max": _max,
}


class Replace:
    """Wrap an update value to bypass the field's reducer (e.g. reset an
    accumulating dict/list between study iterations)."""

    __slots__ = ("value",)

    def __init__(self, value: Any):
        self.value = value


class GraphState(BaseModel):
    """Base state for every workflow graph.

    ``costs`` and ``run_notes`` exist on all states so any node can report
    spend and leave an audit trail without the workflow declaring them.
    """

    model_config = {"extra": "forbid"}

    __reducers__: ClassVar[dict[str, str]] = {
        "costs": "sum_merge",
        "run_notes": "extend",
    }

    costs: dict[str, float] = Field(default_factory=dict)
    run_notes: list[str] = Field(default_factory=list)

    @classmethod
    def reducers(cls) -> dict[str, str]:
        out: dict[str, str] = {}
        for klass in reversed(cls.__mro__):
            out.update(getattr(klass, "__reducers__", {}))
        return out

    def apply(self, update: dict[str, Any]) -> "GraphState":
        """Fold a partial update into this state, returning a new state."""
        if not update:
            return self
        reducers = type(self).reducers()
        fields = type(self).model_fields
        data = self.model_dump()
        for key, value in update.items():
            if key not in fields:
                raise GraphError(
                    f"node returned unknown state field {key!r} "
                    f"(state {type(self).__name__} has: {sorted(fields)})"
                )
            if isinstance(value, Replace):
                data[key] = _dump(value.value)
                continue
            reducer = REDUCERS[reducers.get(key, "replace")]
            data[key] = reducer(data.get(key), _dump(value))
        return type(self).model_validate(data)


def _dump(value: Any) -> Any:
    """Recursively convert pydantic models inside updates to plain data."""
    if isinstance(value, BaseModel):
        return value.model_dump()
    if isinstance(value, list):
        return [_dump(v) for v in value]
    if isinstance(value, dict):
        return {k: _dump(v) for k, v in value.items()}
    return value
