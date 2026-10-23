"""Research Scientist workflow: results analysis.

load results -> sanity check -> deterministic statistics (code, never model
arithmetic) -> interpretation against the pre-registration -> red-team pass
-> persist report, statuses, and supports/refutes edges in the knowledge
graph.
"""

from __future__ import annotations

from typing import Optional

from pydantic import Field

from ..agents.schemas import AnalysisPrep, Interpretation, RedTeam
from ..graph import Context, END, Graph, GraphState, START
from ..tools import stats as stats_mod
from .common import agent_step, bullet, clip

RESULT_SUFFIXES = {".json", ".jsonl", ".csv", ".txt", ".tsv"}


class AnalysisState(GraphState):
    __reducers__ = {"results_files": "merge"}

    exp_id: str = ""
    prereg: str = ""
    results_files: dict[str, str] = Field(default_factory=dict)
    prep: Optional[AnalysisPrep] = None
    stats_md: str = ""
    interpretation: Optional[Interpretation] = None
    red_team: Optional[RedTeam] = None
    analysis_round: int = 0
    report_id: str = ""


# -- tool nodes --------------------------------------------------------------

async def load_results(state: AnalysisState, ctx: Context) -> dict:
    if not state.exp_id:
        raise ValueError("analysis requires an experiment id (EXP-xxx)")
    prereg = ""
    try:
        prereg = ctx.notebook.read(state.exp_id)
    except FileNotFoundError:
        pass
    runs_dir = ctx.docs_dir / "experiments" / "runs" / state.exp_id
    files: dict[str, str] = {}
    if runs_dir.is_dir():
        for path in sorted(runs_dir.rglob("*")):
            if path.is_file() and path.suffix.lower() in RESULT_SUFFIXES and len(files) < 12:
                try:
                    files[path.name] = path.read_text(errors="replace")[:20_000]
                except OSError:
                    continue
    note = (f"load_results: {len(files)} files from {runs_dir}"
            if files else f"load_results: no result files under {runs_dir}")
    return {"prereg": clip(prereg, 8000), "results_files": files, "run_notes": [note]}


async def compute_stats(state: AnalysisState, ctx: Context) -> dict:
    md = stats_mod.describe(state.results_files)
    return {"stats_md": md, "run_notes": ["compute_stats: deterministic stats computed"]}


async def persist_analysis(state: AnalysisState, ctx: Context) -> dict:
    interp = state.interpretation
    assert interp is not None
    red = state.red_team
    body = (
        f"## Verdict\n\n**{interp.verdict.upper()}** — {interp.effect_summary}\n\n"
        f"## Interpretation\n\n{interp.interpretation_md}\n\n"
        f"## Statistics\n\n{state.stats_md}\n\n"
        f"## Threats to validity\n\n{bullet(interp.threats)}\n\n"
        f"## Red team\n\n"
        + (f"Alternative explanations considered:\n{bullet(red.alternative_explanations)}\n\n"
           f"Survives: {red.survives}. {red.notes}\n\n" if red else "_none recorded_\n\n")
        + f"## Follow-ups\n\n{bullet(interp.followups)}\n"
        + (f"\n## Data integrity\n\n{bullet(state.prep.issues)}\n" if state.prep and state.prep.issues else "")
    )
    rep_id, _ = ctx.notebook.create("report", f"Analysis of {state.exp_id}", {
        "experiment": state.exp_id, "links": f"[[{state.exp_id}]]", "body": body,
    })
    hyp_id = ""
    try:
        ctx.notebook.set_frontmatter(state.exp_id, status="analyzed")
        ctx.notebook.append(state.exp_id, "Analysis",
                            f"Verdict: {interp.verdict}. {interp.effect_summary} See [[{rep_id}]].")
        hyp_id = ctx.notebook.frontmatter(state.exp_id).get("hypothesis", "")
    except FileNotFoundError:
        pass
    ctx.kg.add_node(rep_id, "result", f"Analysis of {state.exp_id}: {interp.verdict}")
    if state.exp_id:
        ctx.kg.add_edge(state.exp_id, rep_id, "produced")
    if hyp_id and hyp_id.startswith("HYP"):
        edge = {"supported": "supports", "refuted": "refutes",
                "inconclusive": "inconclusive"}[interp.verdict]
        ctx.kg.add_edge(rep_id, hyp_id, edge)
        status = {"supported": "supported", "refuted": "refuted",
                  "inconclusive": "inconclusive"}[interp.verdict]
        try:
            ctx.notebook.set_frontmatter(hyp_id, status=status)
        except FileNotFoundError:
            pass
    return {"report_id": rep_id,
            "run_notes": [f"persist_analysis: {rep_id} ({interp.verdict})"]}


# -- agent nodes -------------------------------------------------------------

def _sanity_prompt(state: AnalysisState, ctx: Context) -> str:
    listing = "\n\n".join(f"--- {name} ---\n{clip(text, 2500)}"
                          for name, text in list(state.results_files.items())[:8])
    return (
        f"Data integrity check for {state.exp_id}.\n\n"
        f"Pre-registration (excerpt):\n{clip(state.prereg, 3000)}\n\n"
        f"Result files:\n{listing or '(none found)'}\n\n"
        "Check: are the pre-registered metrics present? Multiple seeds/runs? "
        "Missing conditions, truncated files, suspicious values? data_ok=false "
        "if analysis would be built on sand; list every issue found."
    )


def _interpret_prompt(state: AnalysisState, ctx: Context) -> str:
    red = ""
    if state.red_team and not state.red_team.survives:
        red = (
            f"\n\nRed-team round {state.analysis_round} rejected your previous "
            f"interpretation. Alternatives you must now address:\n"
            f"{bullet(state.red_team.alternative_explanations)}\n{state.red_team.notes}\n"
        )
    return (
        f"Interpret the results of {state.exp_id} against its pre-registration.\n\n"
        f"Pre-registration:\n{clip(state.prereg, 5000)}\n\n"
        f"Computed statistics (arithmetic is done — read, don't recompute):\n"
        f"{clip(state.stats_md, 5000)}\n\n"
        f"Data integrity: {'OK' if state.prep and state.prep.data_ok else 'ISSUES'}"
        f"{' — ' + '; '.join(state.prep.issues) if state.prep and state.prep.issues else ''}\n"
        f"{red}\n"
        "Verdict must follow the pre-registered falsification criteria: if the "
        "criteria are not met and not refuted, the honest verdict is "
        "inconclusive. effect_summary quotes the actual numbers. Threats and "
        "follow-ups are specific."
    )


def _redteam_prompt(state: AnalysisState, ctx: Context) -> str:
    interp = state.interpretation
    assert interp is not None
    return (
        f"Red-team this interpretation of {state.exp_id}. Your job is to "
        "break it: seed variance, baseline under-tuning, data leakage, metric "
        "gaming, regression to the mean, selection effects, implementation "
        "bugs that would produce the same numbers.\n\n"
        f"Verdict: {interp.verdict}\nEffect summary: {interp.effect_summary}\n\n"
        f"Interpretation:\n{clip(interp.interpretation_md, 4000)}\n\n"
        f"Statistics:\n{clip(state.stats_md, 3000)}\n\n"
        "survives=true only if the interpretation withstands your strongest "
        "alternative. Be adversarial; agreement is failure unless earned."
    )


def route_redteam(state: AnalysisState) -> str:
    if state.red_team and not state.red_team.survives and state.analysis_round <= 1:
        return "reinterpret"
    return "persist"


def build_analysis() -> Graph:
    g = Graph("analysis", AnalysisState,
              doc="RS results analysis: sanity -> deterministic stats -> interpretation -> red team loop -> persist")
    g.tool("load_results", load_results)
    g.add("sanity", agent_step(
        "sanity", "rs", _sanity_prompt, AnalysisPrep,
        lambda data, s, c: {"prep": data}, use_lab_tools=False), kind="agent", retries=1)
    g.tool("compute_stats", compute_stats)
    g.add("interpret", agent_step(
        "interpret", "rs", _interpret_prompt, Interpretation,
        lambda data, s, c: {"interpretation": data}, use_lab_tools=False),
        kind="agent", retries=1, max_visits=3)
    g.add("red_team", agent_step(
        "red_team", "rs", _redteam_prompt, RedTeam,
        lambda data, s, c: {"red_team": data, "analysis_round": s.analysis_round + 1},
        use_lab_tools=False), kind="agent", retries=1, max_visits=3)
    g.tool("persist_analysis", persist_analysis)

    g.edge(START, "load_results")
    g.chain("load_results", "sanity", "compute_stats", "interpret", "red_team")
    g.branch("red_team", route_redteam,
             {"reinterpret": "interpret", "persist": "persist_analysis"})
    g.edge("persist_analysis", END)
    return g
