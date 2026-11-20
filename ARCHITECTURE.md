# Hong Kong Trip Planner Architecture

[简体中文](ARCHITECTURE.zh.md)

## Deployment

```mermaid
flowchart TB
    Browser["Windows host browser"]

    subgraph Linux["Linux controller VM"]
        Web["Streamlit web UI<br/>trip/ui.py · 8501"]
        API["HTTP API and orchestration<br/>trip/controller.py · 8766"]
        Store[("Run state and reports<br/>SQLite · data/artifacts")]
        SDK["tcloud CLI"]
        Web <-->|"Loopback HTTP"| API
        API <-->|"Read and write"| Store
        API --> SDK
    end

    subgraph TACC["TACC Slurm cluster"]
        Shared[("Shared USERDIR<br/>Dispatch, progress, evidence, results")]
        subgraph Allocation["Shared Slurm allocation"]
            Supervisor["Job supervisor<br/>combined/supervisor.py"]
            Qwen["Qwen GPU inference"]
            Trip["Per-trip CPU step<br/>workflow/trip_job.py · STORM"]
            Supervisor -->|"Start"| Qwen
            Supervisor -->|"Dispatch"| Trip
            Trip <-->|"Queries and draft"| Qwen
        end
        Shared -->|"Read dispatch"| Supervisor
        Trip <-->|"Evidence and artifacts"| Shared
    end

    Sources["Official Hong Kong sources<br/>Venues, transport, lodging, holidays"]
    Browser -->|"VM IP"| Web
    SDK -->|"Start or reclaim"| Supervisor
    SDK <-->|"Submit and retrieve"| Shared
    API <-->|"Fetch sources"| Sources
```

The Windows browser opens Streamlit on the Linux controller. The controller serves the UI and API, persists run state, and fetches source material. TACC runs Qwen and per-trip CPU steps in one Slurm allocation.

## One trip run

```mermaid
sequenceDiagram
    actor User as User
    participant UI as Streamlit
    participant API as Linux controller
    participant Files as TACC USERDIR
    participant Job as Trip CPU step
    participant Model as Qwen GPU service

    User->>UI: Enter dates, party size, budget and preferences
    UI->>API: POST /api/runs
    API-->>UI: 202 Accepted + run_id
    Note over API: Reuse or start a Slurm allocation
    API->>Files: Upload inputs and model binding, then dispatch.json
    Note over Files,Job: The supervisor reads the dispatch and starts the step
    Job->>Model: Generate search queries
    Job->>Files: Write the search request
    API->>Files: Read the request, fetch official sources, and write evidence
    Files-->>Job: Provide evidence
    Note over Job: STORM ranks the evidence
    Job->>Model: Generate a structured itinerary draft
    Model-->>Job: Return the draft
    Note over Job: Python checks timing, transport, and budget
    Job->>Files: Write progress, report, evidence, and the SHA-256 manifest
    API->>Files: Retrieve artifacts and verify the manifest
    Note over API: Check the execution receipt and Slurm outcome, then save results
    UI->>API: Poll progress and results
    API-->>UI: Return the itinerary and downloadable files
    UI-->>User: Show the itinerary and report in the selected language
```

The API returns a run ID immediately. The CPU step asks the controller for official evidence via shared files, ranks that evidence with STORM, and uses Qwen to draft an itinerary. The controller validates the returned artifacts and Slurm outcome before publishing the completed run.

[README](README.md) · [Workflow and API details](workflow/README.md)
