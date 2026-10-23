"""The persona skills shipped in skills/ match what 'tre plugin-sync' renders
from tre.agents.personas; a charter edit without a re-sync fails here.

    .venv/bin/python -m pytest -q tests/test_plugin_sync.py
"""

from dataclasses import replace
from pathlib import Path

import pytest
from typer.testing import CliRunner

from tre.agents.personas import PERSONAS
from tre.cli import app
from tre.plugin import PERSONA_SKILLS, render_skill, skill_path

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("skill", PERSONA_SKILLS, ids=lambda s: s.slug)
def test_shipped_skill_matches_persona(skill):
    shipped = skill_path(ROOT, skill)
    assert shipped.exists(), f"{shipped} missing; run 'tre plugin-sync'"
    assert shipped.read_text() == render_skill(skill, PERSONAS[skill.persona]), (
        f"{shipped} drifted from persona {skill.persona!r}; run 'tre plugin-sync'")


def test_charter_edit_is_drift():
    skill = next(s for s in PERSONA_SKILLS if s.persona == "tl")
    edited = replace(PERSONAS["tl"], system=PERSONAS["tl"].system + "\n- one more rule\n")
    assert render_skill(skill, edited) != skill_path(ROOT, skill).read_text()


def test_cli_writes_every_skill(tmp_path):
    result = CliRunner().invoke(app, ["plugin-sync", "--root", str(tmp_path)])
    assert result.exit_code == 0, result.output
    for skill in PERSONA_SKILLS:
        text = skill_path(tmp_path, skill).read_text()
        assert text.startswith(f"---\nname: {skill.slug}\n")
        assert PERSONAS[skill.persona].system.strip() in text
