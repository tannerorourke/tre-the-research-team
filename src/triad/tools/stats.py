"""Deterministic statistics for results analysis.

Principle: models interpret, code computes. The analysis workflow never asks
an LLM to do arithmetic — this module parses whatever numeric series it can
find in results files and produces descriptive stats, a two-group comparison
when the shape allows it, and a bootstrap CI, all in plain Python.
"""

from __future__ import annotations

import csv
import io
import json
import math
import random
from typing import Any, Optional

# t critical values (two-sided, alpha=.05) by df; falls back to 1.96
_T95 = {1: 12.706, 2: 4.303, 3: 3.182, 4: 2.776, 5: 2.571, 6: 2.447, 7: 2.365,
        8: 2.306, 9: 2.262, 10: 2.228, 12: 2.179, 15: 2.131, 20: 2.086,
        25: 2.060, 30: 2.042}


def _t95(df: int) -> float:
    if df <= 0:
        return float("inf")
    if df in _T95:
        return _T95[df]
    for k in sorted(_T95):
        if df <= k:
            return _T95[k]
    return 1.96


def mean(xs: list[float]) -> float:
    return sum(xs) / len(xs)


def stdev(xs: list[float]) -> float:
    if len(xs) < 2:
        return 0.0
    m = mean(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))


def ci95(xs: list[float]) -> tuple[float, float]:
    if len(xs) < 2:
        v = xs[0] if xs else 0.0
        return (v, v)
    m, s, n = mean(xs), stdev(xs), len(xs)
    half = _t95(n - 1) * s / math.sqrt(n)
    return (m - half, m + half)


def cohens_d(a: list[float], b: list[float]) -> Optional[float]:
    if len(a) < 2 or len(b) < 2:
        return None
    na, nb = len(a), len(b)
    pooled = math.sqrt(((na - 1) * stdev(a) ** 2 + (nb - 1) * stdev(b) ** 2) / (na + nb - 2))
    if pooled == 0:
        return None
    return (mean(a) - mean(b)) / pooled


def bootstrap_diff_ci(a: list[float], b: list[float], iters: int = 4000,
                      seed: int = 1729) -> tuple[float, float]:
    rng = random.Random(seed)
    diffs = []
    for _ in range(iters):
        ra = [a[rng.randrange(len(a))] for _ in a]
        rb = [b[rng.randrange(len(b))] for _ in b]
        diffs.append(mean(ra) - mean(rb))
    diffs.sort()
    lo = diffs[int(0.025 * iters)]
    hi = diffs[int(0.975 * iters) - 1]
    return (lo, hi)


# -- parsing -----------------------------------------------------------------

def extract_series(text: str) -> dict[str, list[float]]:
    """Pull named numeric series out of JSON or CSV content."""
    series: dict[str, list[float]] = {}
    text = text.strip()
    if not text:
        return series
    parsed = _try_json(text)
    if parsed is not None:
        _walk_json(parsed, "", series)
    else:
        _walk_csv(text, series)
    return {k: v for k, v in series.items() if len(v) >= 1}


def _try_json(text: str) -> Any:
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        # maybe JSONL
        rows = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except (json.JSONDecodeError, ValueError):
                return None
        return rows or None


def _walk_json(obj: Any, prefix: str, series: dict[str, list[float]]) -> None:
    if isinstance(obj, dict):
        for k, v in obj.items():
            _walk_json(v, f"{prefix}.{k}" if prefix else str(k), series)
    elif isinstance(obj, list):
        if obj and all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in obj):
            series.setdefault(prefix or "values", []).extend(float(x) for x in obj)
        else:
            for item in obj:
                _walk_json(item, prefix, series)
    elif isinstance(obj, (int, float)) and not isinstance(obj, bool):
        series.setdefault(prefix or "values", []).append(float(obj))


def _walk_csv(text: str, series: dict[str, list[float]]) -> None:
    try:
        reader = csv.DictReader(io.StringIO(text))
        for row in reader:
            for k, v in (row or {}).items():
                if k is None or v is None:
                    continue
                try:
                    series.setdefault(k.strip(), []).append(float(v))
                except ValueError:
                    continue
    except csv.Error:
        return


# -- report ------------------------------------------------------------------

def describe(named_texts: dict[str, str]) -> str:
    """Markdown stats over {label: file content}. Groups series by name; when a
    series name appears in exactly two sources, adds a comparison block."""
    per_source: dict[str, dict[str, list[float]]] = {}
    for label, text in named_texts.items():
        found = extract_series(text)
        if found:
            per_source[label] = found
    if not per_source:
        return "_No numeric series found in the provided results._"

    lines = ["### Descriptive statistics", "",
             "| source | series | n | mean | std | 95% CI |",
             "|--------|--------|---|------|-----|--------|"]
    for label, groups in per_source.items():
        for name, xs in sorted(groups.items()):
            lo, hi = ci95(xs)
            lines.append(
                f"| {label} | {name} | {len(xs)} | {mean(xs):.5g} | "
                f"{stdev(xs):.4g} | [{lo:.5g}, {hi:.5g}] |")

    # cross-source comparisons on shared series names
    all_names: dict[str, list[tuple[str, list[float]]]] = {}
    for label, groups in per_source.items():
        for name, xs in groups.items():
            all_names.setdefault(name, []).append((label, xs))
    comparisons = {n: srcs for n, srcs in all_names.items() if len(srcs) == 2}
    if comparisons:
        lines += ["", "### Two-group comparisons", "",
                  "| series | groups | Δ mean | Cohen's d | bootstrap 95% CI of Δ |",
                  "|--------|--------|--------|-----------|------------------------|"]
        for name, ((la, xa), (lb, xb)) in sorted(comparisons.items()):
            if len(xa) < 2 or len(xb) < 2:
                continue
            d = cohens_d(xa, xb)
            lo, hi = bootstrap_diff_ci(xa, xb)
            lines.append(
                f"| {name} | {la} − {lb} | {mean(xa) - mean(xb):+.5g} | "
                f"{d:.3g} | [{lo:+.5g}, {hi:+.5g}] |" if d is not None else
                f"| {name} | {la} − {lb} | {mean(xa) - mean(xb):+.5g} | n/a | [{lo:+.5g}, {hi:+.5g}] |")
    lines += ["", "_Computed deterministically by triad.tools.stats (no model arithmetic)._"]
    return "\n".join(lines)
