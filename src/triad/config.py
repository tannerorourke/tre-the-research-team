"""Lab configuration.

Loaded from `.triad/config.toml` at the repo root; every field has a sane
default so the file is optional. `triad init` writes a commented starter file.
"""

from __future__ import annotations

import sys
from pathlib import Path

from pydantic import BaseModel, Field

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    tomllib = None


class ModelConfig(BaseModel):
    """Model aliases per role. Any Claude model alias or full ID."""

    research_scientist: str = "sonnet"
    tech_lead: str = "sonnet"
    developer: str = "sonnet"
    router: str = "haiku"


class AutonomyConfig(BaseModel):
    gates: bool = True              # human approval gates in workflows
    max_dev_concurrency: int = 2    # parallel Developer instances
    max_study_iterations: int = 2   # study loop bound
    max_review_rounds: int = 2      # TL review / Dev fix cycles per ticket
    max_debug_rounds: int = 3       # Dev test-fix cycles per ticket
    permission_mode: str = "acceptEdits"   # SDK permission mode for agent turns
    max_budget_usd_per_run: float | None = None


class PathsConfig(BaseModel):
    docs: str = "docs"


class SearchConfig(BaseModel):
    sources: list[str] = Field(default_factory=lambda: ["arxiv", "semantic_scholar", "hf_papers"])
    max_results_per_source: int = 12
    top_k_deep_read: int = 5        # papers whose full text we attempt to fetch
    top_k_notes: int = 10           # papers that get structured notes


class TriadConfig(BaseModel):
    models: ModelConfig = Field(default_factory=ModelConfig)
    autonomy: AutonomyConfig = Field(default_factory=AutonomyConfig)
    paths: PathsConfig = Field(default_factory=PathsConfig)
    search: SearchConfig = Field(default_factory=SearchConfig)

    @classmethod
    def load(cls, root: Path) -> "TriadConfig":
        path = root / ".triad" / "config.toml"
        if not path.exists() or tomllib is None:
            return cls()
        with path.open("rb") as f:
            data = tomllib.load(f)
        return cls.model_validate(data)


STARTER_CONFIG = """\
# Triad lab configuration. Every key is optional; these are the defaults.

[models]
research_scientist = "sonnet"   # any Claude alias or full model ID
tech_lead = "sonnet"
developer = "sonnet"
router = "haiku"                # cheap intent routing in `triad chat`

[autonomy]
gates = true                    # pause at human approval gates
max_dev_concurrency = 2         # parallel Developer agents
max_study_iterations = 2        # bound on the study's iterate loop
max_review_rounds = 2           # TL review <-> Dev fix cycles
max_debug_rounds = 3            # Dev test-fix cycles
permission_mode = "acceptEdits" # or "bypassPermissions" for headless runs
# max_budget_usd_per_run = 15.0

[paths]
docs = "docs"

[search]
sources = ["arxiv", "semantic_scholar", "hf_papers"]
max_results_per_source = 12
top_k_deep_read = 5
top_k_notes = 10
"""
