# Trip evaluation harness

Evaluation for the Hong Kong itinerary workflow. It is split into an offline half
that needs no TACC and an online half that drives the real controller and model.

```
eval/
├── cases.json           20 cases across six conditions, each with a note explaining its expectation
├── metrics.py           pure functions: summarise one run, check its expectation, aggregate a suite
├── run_eval.py          live runner: submit -> poll -> fetch artifacts -> results.jsonl/csv/summary.json
├── test_eval.py         offline tests (case file + metric logic against a recorded run)
└── fixtures/sample_run/ one real recorded run (itinerary, evidence, metrics, trace, report, result)
```

## Offline checks

```bash
python -B -m unittest discover -s eval -q     # 11 tests, no model, no TACC
python -B -m eval.run_eval --dry-run          # validate and list the cases
```

## Live run

Requires the controller and a TACC allocation (see the main README).

```bash
python -B -m eval.run_eval --cases eval/cases.json --out eval/results
python -B -m eval.run_eval --only A01,B01,F01
python -B -m eval.run_eval --category ordinary --limit 4
```

Each case is submitted once with `Idempotency-Key: eval-<case>-<timestamp>`, polled
until it reaches `SUCCEEDED`, `FAILED`, or `CANCELLED`, and summarised from the
artifacts the run publishes.

## How verdicts are defined

Inputs are authored; verdicts are not. Every expectation is derivable from a
published fact or a code constant, and each case records which one in `note`.

| Category | Expectation | Derivation |
| --- | --- | --- |
| `ordinary` | no conflicts | wide windows, budget above meal reserve plus published fares |
| `tight_budget` | `over_budget` present | budget below meal reserve (HKD 50/person/meal) plus fares |
| `required_place_unavailable` | a required-place or opening-hours conflict | the museum is closed that weekday and opens after departure |
| `restricted_window` | none (`observational`) | no deterministic rule applies; numbers are recorded only |
| `missing_information` | `unknowns >= 1` | choosing a lodging always records `room_terms` or `lodging_missing` |
| `infeasible` | `over_budget` present | the budget cannot cover the meal reserve at all |

Subjective qualities (interest alignment, walking comfort) are intentionally not
auto-scored. They need a human rubric and are outside this harness.

## Adding a case

Append to `cases.json` and run the offline tests. The test suite enforces:

* exactly the six categories above are present;
* every case has an `expect` block and a `note`;
* `trip` contains only the fields the API accepts;
* the trip spans 2–4 Hong Kong calendar days.

For a new expectation type, add it to `metrics.check()` and cover it in
`test_eval.py`; keep every rule derivable from a published fact.

## Outputs

`results.jsonl` and `results.csv` carry one record per case, including

* `conflicts` / `conflict_kinds` — hard-constraint outcome after the final repair;
* `first_round_conflicts` — the first draft, before repair (from `trace.jsonl`);
* `unknowns` — unverifiable required facts;
* `citation_coverage` — share of budget items carrying source ids;
* `model_calls`, `prompt_tokens`, `completion_tokens`, `workflow_seconds`, `e2e_seconds`;
* `verdict`, `expectation`, `verdict_detail`.

`summary.json` aggregates `pass_rate`, `conflict_free_cases`, `cases_repaired`,
`plan_status_counts`, per-category counts, and means for cost and latency.

## Known limitation

The harness compares the first draft with the final plan inside one run. It does
not run a separate zero-repair system, because the request schema exposes no
switch to disable repair. Treat `cases_repaired` as a within-run repair effect,
not as an ablation against a no-check baseline.