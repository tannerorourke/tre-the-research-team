# Triad

**A three-agent ML research system that lives on your repository.**

Triad puts a Research Scientist, a Tech Lead, and a Developer on top of any git repo plus a `docs/` notebook. You talk to the Scientist and the Lead; the Developer works under the Lead. Every role runs typed workflow graphs — built on a purpose-built graph engine over the [Claude Agent SDK](https://docs.claude.com/en/api/agent-sdk/overview) — and everything they learn, decide, build, and conclude lands in the notebook and a research knowledge graph with full lineage: hypothesis → experiment → result.

```text
you ──── chat ────┬── Research Scientist ── owns the science
                  │     literature · hypotheses · pre-registered designs
                  │     analysis · interpretation · red-teaming
                  │
                  └── Tech Lead ─────────── owns the engineering
                        recon · ADRs · ticket DAGs · code review · integration
                              │
                              └── Developer(s) ── implement under the Lead
                                    TDD · bounded debug loops · self-review
                                    parallel via git worktrees
```

## Why graphs

Agent quality comes from structure, not vibes. Each workflow is an explicit directed graph: typed pydantic state with reducer semantics, deterministic edges, conditional routers, bounded loops, per-node retries, human approval gates, and a checkpoint after every step — so any run (including one paused at a gate, or one that died mid-flight) resumes exactly where it stopped. Fan-out happens inside map nodes with bounded concurrency, so the graph stays deterministic while searches, paper annotations, and Developer tickets run in parallel. Every graph exports to Mermaid (`triad graph study`, or the committed exports in [`docs/graphs/`](docs/graphs)).

The second graph is the **research knowledge graph** (`docs/knowledge/graph.db`): papers, hypotheses, experiments, results, tickets, and ADRs as typed nodes, with `tests`, `supports`, `refutes`, `implements`, `depends_on` edges. "What evidence bears on HYP-002?" is one lineage query, and every workflow maintains it as it works.

## The workflows

| workflow       | owner     | graph                                           | what happens                                                                                                                                                                                                                                                                                  |
| -------------- | --------- | ----------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `literature` | Scientist | [`literature.mmd`](docs/graphs/literature.mmd) | query planning → parallel arXiv + Semantic Scholar + HF Papers search → triage against explicit criteria → full-text deep read of top papers → one structured note per paper (parallel) → synthesis → adversarial self-critique loop → notes, review, and citations persisted          |
| `design`     | Scientist | [`design.mmd`](docs/graphs/design.mmd)         | 3 candidate hypotheses (falsifiability, predicted effect, novelty/tractability/impact scores) → pre-registered design (baselines, ablations, seeds, power, stop criteria) → **Tech Lead feasibility review loop** → **human approval gate** → HYP/EXP registered              |
| `plan`       | Lead      | [`plan.mmd`](docs/graphs/plan.mmd)             | repo recon → ADR → 2–6 tickets with verifiable acceptance criteria, test plans, and dependencies → dependency waves computed                                                                                                                                                              |
| `execute`    | Lead      | [`execute.mmd`](docs/graphs/execute.mmd)       | **human go/no-go** → per wave: parallel Developers in isolated git worktrees → Lead reviews real diffs against a fixed rubric → bounded fix rounds (a blocked Developer's question gets answered in review comments) → merge + full test suite → integration report                |
| `ticket`     | Developer | [`ticket.mmd`](docs/graphs/ticket.mmd)         | one ticket: gather context → plan → tests-first implementation → engine-run test suite (the agent never grades its own homework) → bounded debug loop → structured self-review → commit + hand-off                                                                                      |
| `analysis`   | Scientist | [`analysis.mmd`](docs/graphs/analysis.mmd)     | load results → data-integrity check → **deterministic statistics in code** (means, CIs, Cohen's d, bootstrap — models interpret, code computes) → interpretation against the pre-registered falsification criteria → red-team pass → report, statuses, and supports/refutes edges |
| `study`      | all       | [`study.mmd`](docs/graphs/study.mmd)           | the full loop: literature → design → plan → execute → analysis → retro, then **iterate** (with a sharpened focus) or **conclude** — bounded, gated, resumable                                                                                                               |

Techniques wired in where they earn their keep: best-of-N hypothesis generation with explicit scoring, reflexion-style self-critique loops, adversarial red-teaming of conclusions, plan-then-execute decomposition, TDD with execution feedback, cross-role review contracts, structured outputs validated at the SDK layer, deterministic code for all arithmetic, and cost accounting per node.

## The graphs

Every workflow is a graph, and every graph ships as a Mermaid file under [`docs/graphs/`](docs/graphs): one per workflow, plus `knowledge.mmd` for the research knowledge graph. `triad graph <workflow>` prints one; `triad graph --export` rewrites the whole folder from the current code, so the files are the generated record of what the engine runs.

Node shapes carry the node kind: `([ ])` is an agent turn, `[[ ]]` a tool or code node, `{ }` a human gate, `[ ]` glue. The study graph splices the five phase graphs (literature, design, plan, execute, analysis) into one checkpointed run, with a retro that iterates or concludes:

```mermaid
flowchart TD
    S((" ")):::term
    E(("done")):::term
    lit_pull_context[["lit pull context"]]:::tool
    lit_plan_queries(["lit plan queries"]):::agent
    lit_search[["lit search"]]:::tool
    lit_triage(["lit triage"]):::agent
    lit_deep_read[["lit deep read"]]:::tool
    lit_annotate[["lit annotate"]]:::tool
    lit_synthesize(["lit synthesize"]):::agent
    lit_self_critique(["lit self critique"]):::agent
    lit_persist[["lit persist"]]:::tool
    des_pull_design_context[["des pull design context"]]:::tool
    des_hypothesize(["des hypothesize"]):::agent
    des_draft_design(["des draft design"]):::agent
    des_feasibility(["des feasibility"]):::agent
    des_approve_design{"des approve design"}:::gate
    des_register[["des register"]]:::tool
    plan_recon[["plan recon"]]:::tool
    plan_draft_adr(["plan draft adr"]):::agent
    plan_decompose(["plan decompose"]):::agent
    plan_persist_plan[["plan persist plan"]]:::tool
    exec_load_plan[["exec load plan"]]:::tool
    exec_approve_execution{"exec approve execution"}:::gate
    exec_abort["exec abort"]:::task
    exec_dispatch_wave[["exec dispatch wave"]]:::tool
    exec_review_wave[["exec review wave"]]:::tool
    exec_fix_wave[["exec fix wave"]]:::tool
    exec_integrate[["exec integrate"]]:::tool
    exec_report(["exec report"]):::agent
    exec_persist_exec[["exec persist exec"]]:::tool
    ana_load_results[["ana load results"]]:::tool
    ana_sanity(["ana sanity"]):::agent
    ana_compute_stats[["ana compute stats"]]:::tool
    ana_interpret(["ana interpret"]):::agent
    ana_red_team(["ana red team"]):::agent
    ana_persist_analysis[["ana persist analysis"]]:::tool
    kickoff["kickoff"]:::task
    bridge_lit_design["bridge lit design"]:::task
    check_design_approval["check design approval"]:::task
    study_halted["study halted"]:::task
    bridge_exec_analysis["bridge exec analysis"]:::task
    retro(["retro"]):::agent
    iterate_reset["iterate reset"]:::task
    conclude["conclude"]:::task
    lit_pull_context --> lit_plan_queries
    lit_plan_queries --> lit_search
    lit_search --> lit_triage
    lit_triage --> lit_deep_read
    lit_deep_read --> lit_annotate
    lit_annotate --> lit_synthesize
    lit_synthesize --> lit_self_critique
    des_pull_design_context --> des_hypothesize
    des_hypothesize --> des_draft_design
    des_draft_design --> des_feasibility
    des_approve_design --> des_register
    plan_recon --> plan_draft_adr
    plan_draft_adr --> plan_decompose
    plan_decompose --> plan_persist_plan
    exec_load_plan --> exec_approve_execution
    exec_dispatch_wave --> exec_review_wave
    exec_fix_wave --> exec_review_wave
    exec_report --> exec_persist_exec
    ana_load_results --> ana_sanity
    ana_sanity --> ana_compute_stats
    ana_compute_stats --> ana_interpret
    ana_interpret --> ana_red_team
    S --> kickoff
    kickoff --> lit_pull_context
    lit_persist --> bridge_lit_design
    bridge_lit_design --> des_pull_design_context
    des_register --> check_design_approval
    study_halted --> E
    plan_persist_plan --> exec_load_plan
    exec_abort --> E
    exec_persist_exec --> bridge_exec_analysis
    bridge_exec_analysis --> ana_load_results
    ana_persist_analysis --> retro
    iterate_reset --> des_pull_design_context
    conclude --> E
    lit_self_critique -->|revise| lit_synthesize
    lit_self_critique -->|accept| lit_persist
    des_feasibility -->|revise| des_draft_design
    des_feasibility -->|approve| des_approve_design
    exec_approve_execution -->|go| exec_dispatch_wave
    exec_approve_execution -->|abort| exec_abort
    exec_review_wave -->|fix| exec_fix_wave
    exec_review_wave -->|integrate| exec_integrate
    exec_integrate -->|next| exec_dispatch_wave
    exec_integrate -->|report| exec_report
    ana_red_team -->|reinterpret| ana_interpret
    ana_red_team -->|persist| ana_persist_analysis
    check_design_approval -->|go| plan_recon
    check_design_approval -->|halt| study_halted
    retro -->|iterate| iterate_reset
    retro -->|conclude| conclude
    classDef agent fill:#dbeafe,stroke:#2563eb,color:#1e3a8a
    classDef tool fill:#dcfce7,stroke:#16a34a,color:#14532d
    classDef task fill:#f1f5f9,stroke:#64748b,color:#0f172a
    classDef gate fill:#fef3c7,stroke:#d97706,color:#78350f
    classDef sub fill:#ede9fe,stroke:#7c3aed,color:#4c1d95
    classDef term fill:#0f172a,stroke:#0f172a,color:#f8fafc
```

`knowledge.mmd` is the knowledge graph of a fresh `triad init`: the type legend and no nodes. It fills as workflows register papers, hypotheses, experiments, tickets, and reports.

## Quickstart

```bash
pip install -e .                      # from this repo; Python 3.10+
export ANTHROPIC_API_KEY=...

cd ~/code/my-research-repo
triad init .                          # docs/ notebook + knowledge graph + config

triad chat                            # talk to the Scientist and the Lead
triad run literature "does curriculum ordering improve sample efficiency in RLHF?"
triad run design     "curriculum ordering in RLHF"
triad run plan       EXP-001
triad run execute    EXP-001
triad run analysis   EXP-001

triad run study "does curriculum ordering improve sample efficiency in RLHF?"   # or all of it
```

Runs stream progress live. A gate pauses the run and asks you; if you walk away, resume any time:

```bash
triad run study --resume 20260806-142355-study --answer des_approve_design=yes
triad run study "..." --yes           # headless: auto-approve all gates
triad status                          # runs, states, gates, costs
triad costs                           # spend by workflow
triad standup                         # today's summary from runs + notebook
triad graph study                     # Mermaid for any workflow
```

In chat: plain messages auto-route (rules first, small-model router on ambiguity), `@rs`/`@tl` address someone directly, `@both` convenes the pair — the Scientist drafts, the Lead adds the engineering take. `/run`, `/standup`, `/status`, `/costs`, `/graph` work inline. Both principals hold persistent sessions primed with the current notebook digest, and check the notebook/knowledge graph/repo through tools before asserting project state.

## The notebook convention

```
docs/
├── charter.md            # the study's single source of truth + running journal
├── literature/LIT-...      # one structured note per paper
├── hypotheses/HYP-...      # statement, rationale, falsification criteria, status
├── experiments/EXP-...     # pre-registration + append-only log; results in runs/EXP-.../
├── decisions/ADR-...       # architecture decision records
├── tickets/TCK-...         # goal, acceptance criteria, test plan, activity trail
├── reports/REP-...         # literature reviews, analyses, integration reports
├── standups/             # generated daily summaries
├── knowledge/graph.db    # the research knowledge graph (SQLite + JSON export)
└── graphs/*.mmd          # Mermaid exports of the workflow graphs
```

Entries carry flat frontmatter (id, status, links) and `[[ID]]` cross-links; INDEX.md files are generated, never hand-edited. Humans edit the notebook freely — it's markdown; agents read and write it through tools. Statuses move honestly: a hypothesis ends `supported`, `refuted`, or `inconclusive`, never quietly forgotten.

## Guardrails

Agent turns run under deny-by-default hooks regardless of prompt content: no `git push`, no `sudo`, no destructive `rm` on absolute paths, no piping downloads into shells, no writes outside the repository. Review gates are human-owned unless you pass `--yes`. Loops are bounded everywhere (revision rounds, debug rounds, fix rounds, study iterations, per-node visit caps, global step caps), and `max_budget_usd_per_run` can cap spend per agent call. The engine — not any agent — runs test suites and computes statistics.

## Configuration

`.triad/config.toml` (written by `triad init`, every key optional):

```toml
[models]
research_scientist = "sonnet"    # any Claude alias or full model ID per role
tech_lead = "sonnet"
developer = "sonnet"
router = "haiku"

[autonomy]
gates = true                     # human approval gates on/off
max_dev_concurrency = 2          # parallel Developers (worktree isolation)
max_study_iterations = 2
max_review_rounds = 2
max_debug_rounds = 3
permission_mode = "acceptEdits"  # "bypassPermissions" for headless boxes

[search]
sources = ["arxiv", "semantic_scholar", "hf_papers"]
top_k_deep_read = 5              # papers fetched in full text
top_k_notes = 10                 # papers that get structured notes
```

## Extending

A new workflow is ~100 lines: define a `GraphState` subclass, add nodes (`agent_step(...)` for persona turns with a pydantic output contract, plain async functions for tool nodes), wire edges/branches/gates, register it in `triad/workflows/__init__.py`. New agent tools go in `triad/tools/server.py` (`@tool` + one entry in `LAB_TOOL_NAMES`). New personas are a system prompt plus a `Persona` entry. The engine (`triad/graph/`) is ~600 lines you own end to end — no framework underneath.

Tests run fully offline: `MockRuntime` swaps for the SDK, canned papers swap for the network, and the whole study loop executes in under a second (`pytest -q`).

## Layout

```text
src/triad/
├── graph/        # the engine: state+reducers, graphs, runner, checkpoints, events, parallel maps
├── agents/       # personas (role charters), typed schemas (the contracts), SDK/mock runtimes
├── workflows/    # literature, design, planning, developer, execution, analysis, study
├── tools/        # papers (arXiv/S2/HF), repo+worktrees, deterministic stats, MCP server
├── memory/       # notebook (templates, IDs, indexes) + knowledge graph (SQLite)
├── chat/         # routed two-principal chat sessions + standup
├── templates/    # notebook entry templates
├── lab.py        # context bootstrap
└── cli.py        # triad init/chat/run/graph/status/costs/standup
```

MIT license.
