from __future__ import annotations

import pytest

from triad.memory import KnowledgeGraph, LabNotebook, links_of, parse_frontmatter


def test_notebook_scaffold_create_ids_and_index(tmp_path):
    nb = LabNotebook(tmp_path / "docs")
    nb.ensure_scaffold()
    assert (tmp_path / "docs" / "charter.md").exists()

    hyp_id, path = nb.create("hypothesis", "Bigger is better",
                             {"statement": "s", "rationale": "r",
                              "falsification": "f", "predicted_effect": "+1",
                              "alternatives": "none"})
    assert hyp_id == "HYP-001"
    assert path.exists()
    assert nb.next_id("hypothesis") == "HYP-002"

    fm = nb.frontmatter(hyp_id)
    assert fm["id"] == "HYP-001"
    assert fm["status"] == "proposed"
    nb.set_frontmatter(hyp_id, status="testing")
    assert nb.frontmatter(hyp_id)["status"] == "testing"

    index = (tmp_path / "docs" / "hypotheses" / "INDEX.md").read_text()
    assert "HYP-001" in index and "testing" in index


def test_notebook_preserves_dollar_signs_in_values(tmp_path):
    nb = LabNotebook(tmp_path / "docs")
    nb.ensure_scaffold()
    hyp_id, _ = nb.create("hypothesis", "Latex survives",
                          {"statement": "loss $L(x) = \\sum x_i$ decreases",
                           "rationale": "r", "falsification": "f",
                           "predicted_effect": "p", "alternatives": "a"})
    text = nb.read(hyp_id)
    assert "$L(x) = \\sum x_i$" in text


def test_notebook_append_and_links(tmp_path):
    nb = LabNotebook(tmp_path / "docs")
    nb.ensure_scaffold()
    exp_id, _ = nb.create("experiment", "Run the thing", {"hypothesis": "HYP-001"})
    nb.append(exp_id, "Log entry", "seeds done, see [[HYP-001]] and [[REP-002]]")
    text = nb.read(exp_id)
    assert "Log entry" in text
    assert links_of(text) == ["HYP-001", "REP-002"]


def test_frontmatter_parser_roundtrip():
    fm, body = parse_frontmatter("---\nid: EXP-001\nstatus: designed\n---\n\n# Body\n")
    assert fm == {"id": "EXP-001", "status": "designed"}
    assert body.startswith("# Body")
    fm2, body2 = parse_frontmatter("no frontmatter here")
    assert fm2 == {} and body2 == "no frontmatter here"


def test_kg_nodes_edges_lineage_mermaid(tmp_path):
    kg = KnowledgeGraph(tmp_path / "graph.db")
    kg.add_node("HYP-001", "hypothesis", "H one")
    kg.add_node("EXP-001", "experiment", "E one")
    kg.add_edge("EXP-001", "HYP-001", "tests")
    kg.add_edge("REP-001", "HYP-001", "supports")   # REP auto-stubbed

    assert kg.get("REP-001")["type"] == "stub"
    kg.add_node("REP-001", "result", "Analysis")    # upgrade the stub
    assert kg.get("REP-001")["type"] == "result"

    lineage = kg.lineage("HYP-001")
    ids = {n["id"] for n in lineage["nodes"]}
    assert ids == {"HYP-001", "EXP-001", "REP-001"}
    assert len(lineage["edges"]) == 2

    mermaid = kg.to_mermaid()
    assert "graph LR" in mermaid and "tests" in mermaid

    stats = kg.stats()
    assert stats["nodes"] == 3 and stats["edges"] == 2

    with pytest.raises(ValueError):
        kg.add_node("X", "not-a-type", "t")
    with pytest.raises(ValueError):
        kg.add_edge("HYP-001", "EXP-001", "not-an-edge")
    kg.close()
