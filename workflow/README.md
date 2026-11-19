# STORM CPU workflows on TACC

## Hong Kong trip workflow

The deployed travel application runs `trip_job.py` in a 2-CPU Slurm step alongside the public Qwen service in one allocation. The supervisor validates the pinned STORM environment and CPU MiniLM model before admitting steps. `job.sh` loads the course runtime and runs the client's own Python with `qwen-run`. [Deployment guide](../README.md)

### Request and execution

Submit `POST /api/runs` with an `Idempotency-Key` to `http://<linux-ip>:8766` or use the Streamlit form on port `8501`. Calls from the controller can use `http://127.0.0.1:8766`. Pick future dates when adapting the sample.

```json
{
  "workflow": "hk_trip",
  "trip": {
    "arrival": "2026-11-21T09:00+08:00",
    "departure": "2026-11-22T20:00+08:00",
    "party_size": 2,
    "budget_hkd": "3500.00",
    "interests": ["艺术", "博物馆"],
    "must_visit": ["香港藝術館"],
    "walking": "normal",
    "gateway": "airport"
  }
}
```

The API returns `202` and a `run_id`. The controller stages `request.json` and `model-config.json` with the attempt ID and the active model instance. It uploads `dispatch.json` last as the commit marker. Use `GET /api/runs/<run_id>` to follow the attempt and `GET /api/runs/<run_id>/artifacts` to retrieve result links. `POST /api/runs/<run_id>/revisions` takes an idempotency key and a body with `changes`, `feedback`, or both. It creates a new trip version from the latest terminal version. `POST /api/runs/<run_id>/retry` with `{}` starts a new attempt for the same terminal run and trip version.

1. The CPU step checks the bound model at `/v1/models`. It uses DSPy and the STORM model adapter to generate queries about transport, accommodation and activities.
2. The step writes `search-request-research-v1.json` under the attempt directory. The controller fetches pages and open data from its official-source catalog, archives the response by request ID and publishes `search-response-research-v1.json`. The step verifies the request hash and uses STORM and the local CPU embedding model to rank the retrieved evidence. The tool wait is bounded to 135 seconds.
3. Qwen emits the itinerary draft using a server-side JSON schema. Python checks time windows, opening hours, transfer buffers and budget arithmetic. The draft can undergo up to three repair rounds. A generation request has a 90-second timeout.

### Output and verification

The attempt directory is `TACC_USERDIR/hk-trip-planner/runs/<run_id>/<attempt_id>/`. The workflow writes `progress.json`, `itinerary.json`, `report.md`, `evidence.json`, `trace.jsonl`, `metrics.json` and `diagnostics.json`. It publishes `artifacts.zip` and then `result.json`, which records the bundle hash and the file SHA-256 hashes. The supervisor writes `execution.json`. The controller caches verified results and checks the Slurm step state and exit code before publishing task success.

### Runtime setup

`setup_env.sh` checks the personal environment in `TACC_USERDIR/agent-envs/storm-e2e-v1/`.
It loads the course runtime, installs the pinned client requirements and bundled STORM source when needed,
and validates CPU MiniLM encoding and semantic retrieval before writing `ready.json`.
The supervisor checks that record before dispatching steps.

On a fresh TACC account the setup step downloads dependencies and the pinned MiniLM snapshot. When the compute environment cannot reach those download sources, set `STORM_WHEELHOUSE` and `STORM_EMBEDDING_SOURCE` to readable directories on TACC in the controller's local deployment configuration.
