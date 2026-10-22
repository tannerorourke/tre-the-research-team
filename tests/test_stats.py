from __future__ import annotations

from triad.tools.stats import (bootstrap_diff_ci, ci95, cohens_d, describe,
                               extract_series, mean)


def test_extract_series_json_and_csv():
    json_text = '{"runs": [{"seed": 1, "acc": 0.9}, {"seed": 2, "acc": 0.92}]}'
    series = extract_series(json_text)
    assert series["runs.acc"] == [0.9, 0.92]
    assert series["runs.seed"] == [1.0, 2.0]

    csv_text = "seed,acc\n1,0.90\n2,0.92\n3,0.91\n"
    series = extract_series(csv_text)
    assert series["acc"] == [0.9, 0.92, 0.91]

    assert extract_series("not numbers at all") == {}


def test_ci_and_effect_size():
    a = [0.90, 0.92, 0.91, 0.93, 0.90]
    b = [0.85, 0.86, 0.84, 0.87, 0.85]
    lo, hi = ci95(a)
    assert lo < mean(a) < hi
    d = cohens_d(a, b)
    assert d is not None and d > 2      # clearly separated groups
    blo, bhi = bootstrap_diff_ci(a, b)
    assert blo > 0                       # CI of the diff excludes zero
    # deterministic (seeded) bootstrap
    assert (blo, bhi) == bootstrap_diff_ci(a, b)


def test_describe_two_group_comparison():
    md = describe({
        "treatment.csv": "acc\n0.90\n0.92\n0.91\n",
        "baseline.csv": "acc\n0.85\n0.86\n0.84\n",
    })
    assert "Descriptive statistics" in md
    assert "Two-group comparisons" in md
    assert "Cohen's d" in md
    assert "no model arithmetic" in md


def test_describe_empty():
    assert "No numeric series" in describe({"a.txt": "words only"})
