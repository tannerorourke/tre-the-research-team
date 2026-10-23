# Walkthrough: one study, end to end

A realistic session on a repo where you're studying whether curriculum
ordering improves sample efficiency in RLHF fine-tuning.

## 0. Attach TRE

```bash
tre serve                       # once per machine; or install contrib/tre.service
cd ~/code/rlhf-curriculum
tre init .
```

`docs/` now has the notebook scaffold and `charter.md`. Fill the charter's
research question section (or let the study kickoff journal it for you).
Then open Claude Code in the repo with the plugin loaded
(`claude --plugin-dir /path/to/tre-the-research-team`); the `tre` skill
registers the repo with the daemon the first time you ask for a run.

## 1. Talk it through

In Claude Code, load the `research-scientist` skill and think out loud:

```
you: I think curriculum ordering matters for RLHF sample efficiency, but
     I'm not sure the effect survives strong baselines.

Claude (as Research Scientist): The claim is testable but under-specified:
     which curriculum signal (loss-based, length, reward-model score), and
     against what baseline schedule? Random-order with matched compute is
     the fair one. I'd start with a focused review; the danger is curriculum
     papers that never tuned their baselines. Want me to start the
     literature run?
```

Switch to the `tech-lead` skill for the engineering read:

```
you: anything in this repo we can reuse?

Claude (as Tech Lead): trainer.py already logs per-step reward; data loading
     is a clean seam for ordering policies. Missing: seeded run configs and a
     metrics writer. Both are small tickets.
```

When you say go, the `tre` skill calls `run_start`, arms a watch on
`tre wait <run_id>`, and brings each gate back to you as a question. The
same runs work from a terminal, which is what the rest of this walkthrough
shows.

## 2. Literature

```bash
tre run literature "curriculum ordering sample efficiency RLHF"
```

Watch it: query plan → three sources searched in parallel → triage → top-5
full-text reads → 10 structured notes → synthesis → self-critique (maybe one
revision) → `docs/literature/LIT-001...010`, review `REP-001`, citations in the
knowledge graph.

## 3. Design, with a gate

```bash
tre run design "curriculum ordering in RLHF"
```

Three candidate hypotheses scored for falsifiability/novelty/tractability;
the selected one becomes a full pre-registration; the Tech Lead reviews
feasibility (maybe forcing one revision — e.g. "3 seeds won't resolve a +2%
effect on this variance; either 5 seeds or a bigger effect threshold").
Then it pauses:

```
┌ gate: approve_design ──────────────────────────────────┐
│ Register this experiment?                              │
│   Hypothesis: RM-score curricula reach parity with 30% │
│   fewer preference pairs ...                             │
│   Compute budget: 6 GPU-days                           │
│ answer ▸ yes
```

`HYP-001` and `EXP-001` are registered; `EXP-001 --tests--> HYP-001` lands in
the knowledge graph.

## 4. Plan and execute

```bash
tre run plan EXP-001        # ADR-001 + TCK-001..004 with dependency waves
tre run execute EXP-001
```

The execute gate shows the waves; answer `yes`. Wave 1's independent tickets
get parallel Developers in git worktrees (`tck/tck-001`, `tck/tck-002`); each
writes failing tests first, implements, and the *engine* runs the suite. The
Tech Lead reviews real diffs; a `request_changes` triggers a bounded fix
round; approved branches merge and the full suite runs. One ticket's job is
"run the sweep; write metrics JSON to `docs/experiments/runs/EXP-001/`".

If you close your laptop mid-run:

```bash
tre status                                  # shows waiting_gate / running
tre run execute --resume <run-id> --answer approve_execution=yes
```

## 5. Analyze

```bash
tre run analysis EXP-001
```

Sanity check on the result files → deterministic stats (per-seed means, 95%
CIs, Cohen's d, bootstrap CI on the difference) → interpretation strictly
against the pre-registered falsification criteria → red-team pass (seed
variance? baseline under-tuned? leakage?) → `REP-002`, `EXP-001` marked
`analyzed`, and `REP-002 --supports/refutes/inconclusive--> HYP-001`.

## 6. Or run the whole loop

```bash
tre run study "does curriculum ordering improve sample efficiency in RLHF?"
```

Same phases, one resumable run, gates included. The retro then either
concludes or iterates with a sharpened focus (design onward, literature
carried forward). The charter accumulates the journal:

```
- 2026-08-06 — study started: does curriculum ordering improve...
- 2026-08-07 — iteration 0 retro: inconclusive; decision: iterate
- 2026-08-09 — study concluded after iteration 1: supported. ...
```

## 7. Interrogate the record

```bash
tre standup                     # what happened, what's in flight
tre costs                       # spend by workflow
tre graph study                 # the orchestration graph itself
sqlite3 docs/knowledge/graph.db 'select * from edges where dst="HYP-001";'
```

Everything TRE believes is in `docs/` — diffable, greppable, yours.
