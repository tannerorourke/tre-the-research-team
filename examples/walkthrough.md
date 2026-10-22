# Walkthrough: one study, end to end

A realistic session on a repo where you're studying whether curriculum
ordering improves sample efficiency in RLHF fine-tuning.

## 0. Attach Triad

```bash
cd ~/code/rlhf-curriculum
triad init .
export ANTHROPIC_API_KEY=...
```

`docs/` now has the notebook scaffold and `charter.md`. Fill the charter's
research question section (or let the study kickoff journal it for you).

## 1. Talk it through

```bash
triad chat
```

```
you ▸ I think curriculum ordering matters for RLHF sample efficiency, but
      I'm not sure the effect survives strong baselines.
┌ Research Scientist ─────────────────────────────────────────────┐
│ The claim is testable but under-specified: which curriculum      │
│ signal (loss-based, length, reward-model score), and against     │
│ what baseline schedule? Random-order with matched compute is the │
│ fair one. I'd start with a focused review — the danger is        │
│ curriculum papers that never tuned their baselines. Kick off:    │
│ /run literature "curriculum ordering sample efficiency RLHF"     │
└──────────────────────────────────────────────────────────────────┘

you ▸ @tl anything in this repo we can reuse?
┌ Tech Lead ───────────────────────────────────────────────────────┐
│ (reads repo via tools) trainer.py already logs per-step reward;  │
│ data loading is a clean seam for ordering policies. Missing:     │
│ seeded run configs and a metrics writer. Both are small tickets. │
└──────────────────────────────────────────────────────────────────┘
```

## 2. Literature

```bash
triad run literature "curriculum ordering sample efficiency RLHF"
```

Watch it: query plan → three sources searched in parallel → triage → top-5
full-text reads → 10 structured notes → synthesis → self-critique (maybe one
revision) → `docs/literature/LIT-001...010`, review `REP-001`, citations in the
knowledge graph.

## 3. Design, with a gate

```bash
triad run design "curriculum ordering in RLHF"
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
triad run plan EXP-001        # ADR-001 + TCK-001..004 with dependency waves
triad run execute EXP-001
```

The execute gate shows the waves; answer `yes`. Wave 1's independent tickets
get parallel Developers in git worktrees (`tck/tck-001`, `tck/tck-002`); each
writes failing tests first, implements, and the *engine* runs the suite. The
Tech Lead reviews real diffs; a `request_changes` triggers a bounded fix
round; approved branches merge and the full suite runs. One ticket's job is
"run the sweep; write metrics JSON to `docs/experiments/runs/EXP-001/`".

If you close your laptop mid-run:

```bash
triad status                                  # shows waiting_gate / running
triad run execute --resume <run-id> --answer approve_execution=yes
```

## 5. Analyze

```bash
triad run analysis EXP-001
```

Sanity check on the result files → deterministic stats (per-seed means, 95%
CIs, Cohen's d, bootstrap CI on the difference) → interpretation strictly
against the pre-registered falsification criteria → red-team pass (seed
variance? baseline under-tuned? leakage?) → `REP-002`, `EXP-001` marked
`analyzed`, and `REP-002 --supports/refutes/inconclusive--> HYP-001`.

## 6. Or run the whole loop

```bash
triad run study "does curriculum ordering improve sample efficiency in RLHF?"
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
triad standup                     # what happened, what's in flight
triad costs                       # spend by workflow
triad graph study                 # the orchestration graph itself
sqlite3 docs/knowledge/graph.db 'select * from edges where dst="HYP-001";'
```

Everything Triad believes is in `docs/` — diffable, greppable, yours.
