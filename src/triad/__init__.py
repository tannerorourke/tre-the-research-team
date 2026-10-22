"""Triad — a three-agent ML research lab that lives on your repository.

Personas:
    Research Scientist  (user-facing)  — questions, literature, hypotheses, designs, analysis
    Tech Lead           (user-facing)  — architecture, decomposition, review, integration
    Developer           (internal)     — TDD implementation under the Tech Lead

Everything runs as typed workflow graphs (triad.graph) over the Claude Agent
SDK, grounded in a git repository plus a docs/ lab notebook, with a research
knowledge graph linking hypotheses -> experiments -> results.
"""

__version__ = "0.1.0"
