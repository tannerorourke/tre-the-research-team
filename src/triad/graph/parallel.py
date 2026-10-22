"""Bounded intra-node parallelism.

Fan-out/join lives *inside* nodes: a map node runs its items concurrently
under a semaphore and returns ordered results. The graph itself stays
sequential, deterministic, and checkpoint-resumable."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Iterable, TypeVar

T = TypeVar("T")


@dataclass
class MapError:
    index: int
    item: Any
    error: str


async def parallel_map(
    items: Iterable[T],
    worker: Callable[[T], Awaitable[Any]],
    *,
    limit: int = 4,
) -> list[Any]:
    """Run `worker` over items with at most `limit` in flight.

    Order of results matches order of items. A failed item yields a MapError
    instead of poisoning the whole map; callers decide whether that's fatal.
    """
    sem = asyncio.Semaphore(max(1, limit))
    items = list(items)

    async def one(i: int, item: T) -> Any:
        async with sem:
            try:
                return await worker(item)
            except Exception as exc:  # noqa: BLE001 — map isolation is the point
                return MapError(index=i, item=_brief(item), error=f"{type(exc).__name__}: {exc}")

    return list(await asyncio.gather(*(one(i, it) for i, it in enumerate(items))))


def split_errors(results: list[Any]) -> tuple[list[Any], list[MapError]]:
    ok = [r for r in results if not isinstance(r, MapError)]
    bad = [r for r in results if isinstance(r, MapError)]
    return ok, bad


def _brief(item: Any) -> Any:
    s = repr(item)
    return s if len(s) <= 200 else s[:200] + "…"
