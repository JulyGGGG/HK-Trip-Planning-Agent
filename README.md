# Hong Kong Trip Planner

[简体中文](README.zh.md)

HK-Trip-Planning-Agent is a course project for **CSIT 6000V Advanced AI Computing and Systems**.

Streamlit collects travel dates, group size, budget, and preferences. The controller submits jobs to TACC through tcloud. STORM ranks official source material, Qwen drafts the itinerary, and Python checks timing, transport, and costs. The web app and Markdown reports support Simplified Chinese and English.

```
Browser ──► Streamlit (8501) ──► Trip controller (8766) ──tcloud──► TACC Slurm allocation
                                        │                            ├── Qwen 27B (vLLM, 2 GPU)
                                        │                            └── trip CPU step (STORM + checks)
                                        └── official-source bridge ──► MTR / LCSD / 1823 open data
```

## Project structure

[Deployment and request-flow diagrams](ARCHITECTURE.md)

| Path | Responsibility |
| --- | --- |
| [`trip/`](trip/) | Linux controller package. [`ui.py`](trip/ui.py) serves the Streamlit UI, [`controller.py`](trip/controller.py) runs the API and dispatches trips, [`sources.py`](trip/sources.py) fetches official data, and [`store.py`](trip/store.py) manages trip revisions. |
| [`app.py`](app.py), [`orchestrator.py`](orchestrator.py), [`coallocated.py`](coallocated.py), [`gateway_accounting.py`](gateway_accounting.py) | Shared HTTP API and SQLite job storage, tcloud commands, dispatch within a shared allocation, and independent Slurm accounting over SSH. |
| [`combined/`](combined/) and [`service/`](service/) | The TACC supervisor launches Qwen and per-trip CPU steps in the same Slurm allocation. The service directory contains model startup and accounting code. |
| [`workflow/`](workflow/) | [`trip_job.py`](workflow/trip_job.py) uses STORM and Qwen in the trip step and writes artifacts. [`trip_schedule.py`](workflow/trip_schedule.py) checks timing, transport and budget, [`trip_report.py`](workflow/trip_report.py) renders Markdown, and [`storm-src/`](workflow/storm-src/) contains the pinned STORM source. |
| [`eval/`](eval/) | Evaluation cases, the live evaluation runner, and offline metric tests. See [Detailed Instructions](#detailed-instructions). |
| [`install.sh`](install.sh), [`trip/install.py`](trip/install.py), [`trip/deploy.py`](trip/deploy.py), [`.env.example`](.env.example) | Linux entry point, systemd installer, optional SSH publisher, and local configuration template. |
| [`tests/`](tests/) | API and scheduling regression tests, with synthetic fixtures for the bilingual trip tests. Module tests also live alongside code in `trip/`, `combined/`, `workflow/`, and `eval/`. |

Requests go from Streamlit to the Linux controller and then to TACC through tcloud. After the CPU step finishes, the controller checks the Slurm outcome and artifact manifest, stores reports in the installation's `data/artifacts/`, and records run state in `data/state.sqlite3`. The browser can reopen saved trips through the controller.

## Getting Started Instructions

### Prerequisites

| Requirement | Notes |
| --- | --- |
| Linux control machine | Ubuntu 22.04/24.04 or WSL2. The controller uses `fcntl` and POSIX process groups. |
| Python 3.8+ with `venv` | `python3 -m venv` must work. |
| Course `tcloud` client (SDK v0.4.1) | On `PATH`, or set `TCLOUD_BIN` to its absolute path. |
| TACC account with CSIT6000V access | The runtime lives under `/mnt/home/public/course-envs/CSIT6000V/`. |
| SSH key registered with TACC | `tcloud config -u USER -f /path/to/private_key`; the controller also uses it for read-only accounting over the gateway. |
| Outbound HTTPS on the controller | Streamlit/PyPI plus the official Hong Kong sources (`opendata.mtr.com.hk`, `*.lcsd.gov.hk`, `www.1823.gov.hk`, `www.yha.org.hk`). |
| TACC `USERDIR` space | About 1.2 GB measured: 862 MB private STORM venv, 88 MB MiniLM snapshot, 184 MB pip cache. |
| Git and `rsync` | `tcloud submit` synchronises the staged directory with `rsync`. |

The teaching team can run every step below without a GPU on the controller; GPUs are requested only by the TACC allocation.

### 1. Install and configure tcloud

```bash
# Official SDK v0.4.1 (linux-amd64)
unzip linux-amd64-v0.4.1.zip -d ~/tcloud-sdk
chmod +x ~/tcloud-sdk/tcloud
export PATH="$HOME/tcloud-sdk:$PATH"

tcloud config -u YOUR_TACC_USERNAME -f /path/to/private_key
tcloud init
tcloud env ls          # must print a "# conda environments:" block
```

### 2. Register the public course conda path

`service/job.sh` requires `CONDA_PREFIX` to be the shared course environment, not a personal copy. Register it once per account:

```bash
cat > ~/.condarc <<'EOF'
envs_dirs:
  - /mnt/home/public/course-envs/CSIT6000V/common
EOF
tcloud upload ~/.condarc ../.condarc
```

Verify with a one-minute job that prints `$CONDA_PREFIX`; it must end with `/CSIT6000V/common/proxy`.

### 3. Stage the CPU embedding model on TACC

The trip step performs semantic retrieval on CPU with `sentence-transformers/paraphrase-MiniLM-L6-v2`. Compute nodes cannot reach `hf-mirror.com`, so stage the snapshot once:

```bash
python -B prepare_embedding.py                       # writes data/embedding-model (92 MB)
tcloud upload data/embedding-model agent-envs/storm-e2e-v1/embedding-model
```

### 4. Build the private STORM environment on TACC

```bash
cd workflow
cp tuxiv-setup.conf tuxiv.conf
tcloud submit                 # job name: storm-env-setup, 60 min partition limit
tcloud ps -j <JOB_ID>
tcloud cat "slurm_log/storm-env-setup-<JOB_ID>.log" | tail -40
```

Success is the line `STORM_ENV_READY=/mnt/home/<user>/USERDIR/agent-envs/storm-e2e-v1` and a written `ready.json`. The setup installs 68 pinned packages into a nested venv, reuses the read-only course `torch`, and verifies real DSPy imports plus a real CPU retrieval.

If the first run is killed by the 60-minute partition limit, resubmit the same job: the venv is persistent and pip skips satisfied packages.

### 5. Prepare the controller

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r trip/requirements-control.txt   # streamlit, paramiko
```

`sudo ./install.sh` installs the same checkout under `/opt/hk-trip-planner` with two systemd units. The manual launcher below is equivalent and is what was used for validation.

### 6. Start the controller and the UI

```bash
export PYTHONPATH="$PWD"
export TACC_PROJECT_SESSION=hk-trip-planner
export TACC_PROJECT_RUNS=hk-trip-planner/runs
export TCLOUD_BIN="$HOME/tcloud-sdk/tcloud"
export TACC_GATEWAY_HOST=gw.turing.ust.hk
export TACC_GATEWAY_PORT=30041
export TACC_GATEWAY_USERDIR=/mnt/home/YOUR_TACC_USERNAME/USERDIR
export HK_TRIP_API_BIND=127.0.0.1 HK_TRIP_API_PORT=8766

nohup .venv/bin/python -B -m trip.controller --data-dir "$PWD/data" --port 8766 \
      > data/controller.log 2>&1 &

STREAMLIT_SERVER_ADDRESS=127.0.0.1 STREAMLIT_SERVER_PORT=8501 \
  nohup .venv/bin/python -m streamlit run trip/ui.py --server.headless=true \
      > data/ui.log 2>&1 &

curl -s localhost:8766/api/health
# {"ok": true, "mode": "hk-trip", "time": "..."}
```

Open `http://localhost:8501/` for the form, or use the API directly.

### 7. End-to-end example

Submit one trip. `Idempotency-Key` is required; repeating the same key with the same body returns the same run instead of creating a second one.

```bash
curl -s -X POST http://127.0.0.1:8766/api/runs \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: readme-example-1' \
  -d '{"workflow":"hk_trip","trip":{
        "arrival":"2026-12-04T09:00","departure":"2026-12-06T20:00",
        "party_size":2,"budget_hkd":"3500.00",
        "interests":["艺术","博物馆"],"must_visit":["香港藝術館"],
        "walking":"normal","gateway":"airport"}}'
```

Expected response (`202`):

```json
{"run_id": "run-<24 hex>", "attempt_id": "a1", "status": "ACCEPTED",
 "request": {"workflow": "hk_trip", "trip": {"...": "normalised"}, "version": 1},
 "workflow": {"phase": "WAITING", "message": "已保存任务，等待后台处理"},
 "status_url": "/api/runs/run-<24 hex>", "created": true}
```

Poll until `status` is terminal:

```bash
curl -s http://127.0.0.1:8766/api/runs/<RUN_ID>
```

A successful run walks through these phases and ends with `status: "SUCCEEDED"`:

```
WAITING_MODEL → QUEUED → RUNNING (INITIALIZING → RESEARCHING → PLANNING → CHECKING → REPAIRING) → SUCCEEDED
```

First submission also starts the GPU allocation, so expect **20 minutes of model cold start** before `RUNNING` (observed 2026-11-22 on 2×RTX 3090). The trip step itself took **125 s** for this example.

Fetch the artifacts:

```bash
curl -s http://127.0.0.1:8766/api/runs/<RUN_ID>/artifacts
# {"artifacts":[{"artifact_id":"0","kind":"report","path":"report.md",...}, ...]}
curl -s http://127.0.0.1:8766/api/runs/<RUN_ID>/artifacts/0
```

Expected artifacts, all present for a completed run:

| File | Contents |
| --- | --- |
| `report.md` | Human-readable itinerary, budget table, and the official sources with excerpts |
| `itinerary.json` | Structured plan with `status`, `conflicts`, `unknowns`, budget items, alternatives |
| `evidence.json` | Every fetched official record, with URL, excerpt, and `fetched_at` |
| `trace.jsonl` | Per-call events: model calls with token usage, tool calls, and validation feedback per repair round |
| `metrics.json` | `model_calls`, `successful_model_calls`, `repair_rounds`, `prompt_tokens`, `completion_tokens`, `workflow_seconds` |
| `diagnostics.json` | Environment versions (STORM commit, torch path) and model identity |
| `result.json` | Manifest with SHA-256 of each artifact and of the bundle |

Observed values for the example above (2026-11-22):

```
status         SUCCEEDED
plan_status    INSUFFICIENT_EVIDENCE   (lodging price not published; no hard conflicts)
conflicts      0
model_calls    3 (all successful), repair_rounds 1
tokens         1,876 prompt + 544 completion
workflow       120.3 s, step 125 s, peak RSS 1.1 GB
evidence       70 official records fetched, 24 cited in the report
```

### Client screenshots

`docs/screenshots/` holds the interface states used in the project report:

| File | Shows |
| --- | --- |
| `01-input-form.png` | Trip form: dates, arrival/departure times, gateway, party size, budget, interests, must-visit places, walking preference |
| `02-itinerary.png` | Day-by-day itinerary with times, transfers and estimate markers |
| `03-budget-summary.png` | Budget totals: limit, priced subtotal, nights |
| `04-budget-items.png` | Itemised budget with quantity, amount and evidence column |
| `05-sources.png` | Official sources with the quoted excerpt and fetch time |
| `06-conflict-case.png` | Infeasible case: HKD 1.00 budget against HKD 988.60 of priced items, with the conflict banner |
| `07-conflict-budget.png` | The same infeasible case, itemised budget |
| `08-itinerary-en.png` | English interface for the same itinerary |

## Detailed Instructions

### Repository tests

The four suites below need no TACC access and no model:

```bash
python3 -m venv .venv && . .venv/bin/activate
python -m pip install -r trip/requirements-control.txt

python -B -m unittest discover -s tests -q      # 86 tests: API, idempotency, dispatch, accounting
python -B -m unittest discover -s combined -q   #  8 tests: supervisor lifecycle
python -B -m unittest discover -s workflow -q   #  2 tests: workflow contract
python -B -m unittest discover -s trip -q       # 17 tests: controller, installer, bilingual UI
python -B -m unittest discover -s eval -q       # 11 tests: case file + metric/verdict logic
```

`trip/test_trip_ui.py` drives Streamlit's `AppTest`, which has a 3 s per-run timeout; on a slow filesystem one UI test can flake. Re-run that suite alone to confirm.

`eval/test_eval.py` is the offline half of the evaluation: it validates `eval/cases.json` and then runs the metric and verdict logic against `eval/fixtures/sample_run/`, a real recorded run. It proves the harness without spending GPU time.

### Evaluation cases

`eval/cases.json` holds 20 cases across the six conditions in the project proposal:

| Category | Cases | Expected verdict, and how it is derived |
| --- | --- | --- |
| `ordinary` | A01–A04 | `conflicts == []`. Wide windows and a comfortable budget; every required place is open on at least one trip day. |
| `tight_budget` | B01–B04 | `over_budget` must appear. The budget is below meals (HKD 50 per person per meal, a code constant) plus published MTR fares. |
| `required_place_unavailable` | C01–C03 | `required_missing` or an opening-hours conflict. The required museum is closed on that weekday and opens after the visitor has left (Art Museum and Science Museum closed Thursdays, History Museum closed Tuesdays). |
| `restricted_window` | D01–D03 | Observational. Late-night, pre-dawn and after-midnight windows have no deterministic expectation, so the verdict is recorded as `skip`. |
| `missing_information` | E01–E03 | `unknowns >= 1`. Choosing a lodging always records `room_terms` or `lodging_missing`, because nightly prices are not published. |
| `infeasible` | F01–F03 | `over_budget`. Budgets of HKD 1/50/120 cannot cover the meal reserve, whatever the model proposes. |

Cases are authored inputs; the verdicts are not. Each expectation is derivable from a published fact or a code constant, and every case carries a `note` explaining which. Subjective criteria (interest alignment, walking comfort) are deliberately **not** auto-scored; they need human review.

### Rerunning the evaluation

With the controller and a TACC allocation available:

```bash
. .venv/bin/activate

# Validate the case file and list what would run (no GPU needed)
python -B -m eval.run_eval --dry-run

# Full suite
python -B -m eval.run_eval --cases eval/cases.json --out eval/results

# Subsets, for a quick check or to fit one allocation
python -B -m eval.run_eval --only A01,B01,F01
python -B -m eval.run_eval --category ordinary
python -B -m eval.run_eval --limit 6
```

Outputs (all written under `--out`):

| File | Contents |
| --- | --- |
| `results.jsonl` | One record per case: run state, plan status, conflict kinds, first-round and final conflicts, unknowns, budget, citation coverage, sources, tokens, timings, verdict |
| `results.csv` | The same records as a table, ready for the report |
| `summary.json` | `pass_rate`, `conflict_free_cases`, `cases_repaired`, `plan_status_counts`, means of tokens/seconds/citation coverage, and per-category counts |

Runtime: about 2 minutes per trip step once the model is warm, so 20 cases take roughly 25–40 minutes including one cold start. The controller drains an allocation after 45 minutes and keeps two CPU steps in flight, so a full suite may span two allocations.

### Recorded results

All 20 cases were run against one course Qwen instance. `eval/runs.json` maps every
case to the run that produced it, so the table can be rebuilt without spending GPU
time:

```bash
python -B -m eval.replay --runs eval/runs.json --out eval/results
```

The outputs of the recorded suite are committed under `eval/recorded/`
(`summary.json`, `results.csv`, `results.jsonl`).

| Category | Cases | Scored | Passed | What the run showed |
| --- | --- | --- | --- | --- |
| `ordinary` | 4 | 4 | 4 | Zero hard conflicts; one unknown each (lodging terms) |
| `tight_budget` | 4 | 4 | 4 | `over_budget` reported |
| `required_place_unavailable` | 3 | 3 | 3 | Required place or schedule conflict reported |
| `restricted_window` | 3 | – | – | Observational, no assertion |
| `missing_information` | 3 | 3 | 3 | `unknowns >= 1` |
| `infeasible` | 3 | 3 | 3 | `over_budget` reported |

Aggregates from `summary.json`:

```
cases 20 · scored 17 · passed 17 · failed 0 · observational 3
plan status       INSUFFICIENT_EVIDENCE 8 · CONFLICTS 12
conflict-free     8 cases
repaired          3 cases (first draft conflicted, final plan clean)
cost per case     4.0 model calls · 2,968 prompt + 616 completion tokens
latency           147.0 s workflow · 157.6 s end to end
citation support  0.815 of budget items carry a source id
```

Two notes on how these numbers were obtained. One restricted-window attempt was
rejected with `SERVICE_DRAINING` at the allocation's 45-minute boundary and was
retried on a fresh allocation; only the retry is included. Model sampling is
deterministic in temperature but vLLM batching still varies, so the chosen places
and times can differ between runs; the table measures hard-constraint verdicts,
which the checks derive deterministically from the published facts.

### Case revisions

* **C02** — the accepted conflict set was widened to include `day_end` and
  `slot_overlap`. In the recorded run the model scheduled the required museum at
  10:00–11:00, inside opening hours but **after the 09:00 departure**, so the
  checker expressed the infeasibility as `day_end`/`slot_overlap` rather than the
  opening-hours conflict the case originally listed. The fact that the visit
  cannot fit is unchanged; only the expression the harness accepts was corrected.
  The case file in the repository is the corrected version.

### Metrics and where they appear in the report

| Metric (source) | Definition in this artifact | Reported as |
| --- | --- | --- |
| `conflicts` / `conflict_kinds` (`itinerary.json`) | Count of hard-constraint violations after the final repair round | Hard-constraint pass rate (`summary.pass_rate`, `conflict_free_cases`) |
| `first_round_conflicts` (`trace.jsonl`, round 0) | Violations in the first draft, before any repair | Input to the repair-effectiveness comparison |
| `cases_repaired` (`summary.json`) | Cases with `first_round_conflicts > 0` and final `conflicts == 0` | Repair success count |
| `over_budget` conflict | `known + estimates > limit` in `trip_schedule.build_plan` | Budget arithmetic and overspend table |
| `unknowns` (`itinerary.json`) | Unverifiable required facts (currently lodging terms) | Insufficient-evidence rate, reported separately and never counted as success |
| `citation_coverage` (`eval/metrics.py`) | Budget items carrying `source_ids` over all items | Citation support |
| `model_calls`, `prompt_tokens`, `completion_tokens`, `workflow_seconds` (`metrics.json`) | Per-run cost | Cost and latency table |
| `sources`, `source_errors` (`evidence.json`) | Fetched official records and failures | Evidence coverage |

The evaluation deliberately measures repair **within one run** (first draft versus final plan) rather than comparing against a separate zero-repair system. The gap: a true ablation that disables repair is not implemented, because the request schema is closed and exposes no such switch.

### Known limitations and expected variation

* **Cold start dominates latency.** A new allocation needs about 20 minutes for vLLM to load and compile before the first trip step; one trip step takes about 2 minutes. Runs submitted while an allocation is warm skip the cold start.
* **Idle allocations are reclaimed.** The controller cancels a ready allocation after 180 seconds without pending work (`IDLE_SECONDS` in `trip/controller.py`) to release GPUs. The next submission pays the cold start again.
* **Allocation lifetime.** The course partition allows 60 minutes per job; the supervisor stops admitting new steps after 45 minutes. A full 20-case suite can therefore span two allocations.
* **Concurrency.** The supervisor runs at most two trip steps at once; additional submissions stay in `WAITING_MODEL`.
* **Lodging prices are not published.** The YHA page lists room types but no nightly rate, so overnight trips normally end as `INSUFFICIENT_EVIDENCE` with a `room_terms` unknown rather than `FEASIBLE_WITH_ESTIMATES`. The room charge also carries no amount, so it is excluded from the priced subtotal and **the budget check cannot validate the largest expense**. This is intended behaviour: the system reports what it could verify instead of guessing a rate.
* **Compute nodes cannot reach `hf-mirror.com`.** The setup step would fail on a fresh account unless the MiniLM snapshot is pre-staged as in step 3, or `STORM_EMBEDDING_SOURCE` points at a readable directory on TACC.
* **Official pages change.** Fares, opening hours and closure days are fetched live. If an operator revises a page, a previously passing case can legitimately change verdict; that is why every citation records `fetched_at` and the source excerpt.
* **Model variation.** Qwen is called with `temperature=0`, but vLLM batching and prefix caching can still alter token-level output, so the exact itinerary may differ between runs. Hard-constraint verdicts should be stable; the chosen places and times may not be.
* **Private data stays private.** Artifacts under `data/` and TACC `USERDIR` are per-account; nothing is committed to the repository.

## Updates and checks

Run the same installer from the target Git checkout after pending trips have finished and the GPU allocation has been reclaimed. Releases are versioned. SQLite and reports stay in the installation's `data/` directory. A failed startup or health check restores the previous release and systemd configuration. To roll back to an earlier Git version, run the installer from that checkout with the same local configuration.

```bash
systemctl is-active hk-trip-api hk-trip-ui
journalctl -u hk-trip-api -u hk-trip-ui -n 60 --no-pager
```

Optional SSH publishing from Windows uses the same Linux installer. Set `HK_TRIP_SSH_HOST` in the local `.env.local` to the SSH alias and configure `/etc/hk-trip-planner.env` on the target Linux host. The target account needs non-interactive sudo.

```powershell
python -B -m trip.deploy
```

## Tests

See [Repository tests](#repository-tests) above. Cluster execution, API inputs, and artifact details are in the [workflow guide](workflow/README.md).