# Hong Kong Trip Planner

[简体中文](README.zh.md)

HK-Trip-Planning-Agent is a course project for **CSIT 6000V Advanced AI Computing and Systems**.

Streamlit collects travel dates, group size, budget, and preferences. The controller submits jobs to TACC through tcloud. STORM ranks source material, Qwen drafts the itinerary, and Python checks timing, transport, and costs. The web app and Markdown reports support Simplified Chinese and English.

## Project structure

[Deployment and request-flow diagrams](ARCHITECTURE.md)

| Path | Responsibility |
| --- | --- |
| [`trip/`](trip/) | Linux controller package. [`ui.py`](trip/ui.py) serves the Streamlit UI, [`controller.py`](trip/controller.py) runs the API and dispatches trips, `sources.py` fetches official data, and `store.py` manages trip revisions. |
| [`app.py`](app.py), [`orchestrator.py`](orchestrator.py), [`coallocated.py`](coallocated.py), [`gateway_accounting.py`](gateway_accounting.py) | Shared HTTP API and SQLite job storage, tcloud commands, dispatch within a shared allocation, and independent Slurm accounting over SSH. |
| [`combined/`](combined/) and [`service/`](service/) | The TACC supervisor launches Qwen and per-trip CPU steps in the same Slurm allocation. The service directory contains model startup and accounting code. |
| [`workflow/`](workflow/) | [`trip_job.py`](workflow/trip_job.py) uses STORM and Qwen in the trip step and writes artifacts. `trip_schedule.py` checks timing, transport and budget, `trip_report.py` renders Markdown, and `storm-src/` contains the pinned STORM source. |
| [`install.sh`](install.sh), [`trip/install.py`](trip/install.py), [`trip/deploy.py`](trip/deploy.py), [`.env.example`](.env.example) | Linux entry point, systemd installer, optional SSH publisher, and local configuration template. |
| [`tests/`](tests/) | API and scheduling regression tests, with synthetic fixtures for the bilingual trip tests. Module tests also live alongside code in `trip/`, `combined/`, and `workflow/`. |

Requests go from Streamlit to the Linux controller and then to TACC through tcloud. After the CPU step finishes, the controller checks the Slurm outcome and artifact manifest, stores reports in the installation's `data/artifacts/`, and records run state in `data/state.sqlite3`. The browser can reopen saved trips through the controller.

## Linux deployment

On a Linux controller running systemd, install Git, Python 3.8 or newer with its `venv` module, and the course tcloud client. The Linux service user needs a working `~/.tcloud/.userconfig`, the private key it references, and a TACC gateway host key verified through a trusted channel in `~/.ssh/known_hosts`. The controller needs access to its Python package index. A fresh TACC account also needs access to the client dependencies and MiniLM weights, or readable wheelhouse and model directories on TACC.

Before cloning this private repository, authenticate Git with a GitHub account that has read access.

```bash
git clone https://github.com/JulyGGGG/HK-Trip-Planning-Agent.git hk-trip-planner
cd hk-trip-planner
cp .env.example .env.local
# Set the service user and tcloud path in .env.local for this host.
sudo ./install.sh
```

The installer defaults to `/opt/hk-trip-planner` and the `root` service user. Use `http://<linux-ip>:8501/` for the app and `http://<linux-ip>:8766/api/health` for the API health check. Both listeners default to all IPv4 interfaces. Set both bind values to `127.0.0.1` for local-only access. Clients that can reach the API can submit or cancel trips. Direct access to both ports is intended for a trusted single-user demo network.

The installer reads an explicit `--config` first, then `.env.local` in the checkout, then `/etc/hk-trip-planner.env`. Settings use literal `KEY=value` lines with optional comment lines beginning with `#`. You can override paths, ports, the TACC gateway, and GPU node exclusions. Use `HK_TRIP_PIP_INDEX_URL` to replace PyPI with a controller dependency mirror. The optional gateway overrides apply to verified SSH accounting and artifact retrieval. The course SDK uses its own gateway and identity settings. [Configuration example](.env.example)

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

Run from the repository root. Bilingual tests use synthetic fixtures included in the repository.

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r trip/requirements-control.txt
python -B -m unittest discover -s tests -q
python -B -m unittest discover -s combined -q
python -B -m unittest discover -s workflow -q
python -B -m unittest discover -s trip -q
```

See the workflow guide for cluster execution, API inputs, and artifacts. [STORM workflow](workflow/README.md)
