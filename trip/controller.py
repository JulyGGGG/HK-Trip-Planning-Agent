"""Control the Hong Kong trip API, official-source bridge and Slurm service."""

import argparse
import base64
import hashlib
import json
import os
import re
import shlex
import signal
import subprocess
import tempfile
import threading
import time
import zipfile
from pathlib import Path

import gateway_accounting
from app import TERMINAL, make_server
from coallocated import CoallocatedWorker
from orchestrator import ROOT, RUN_ROOT, SESSION, SCHED_TERMINAL, atomic, load
from trip.sources import search
from trip.settings import DEFAULTS
from trip.store import TripStore
from workflow.trip_contract import ARTIFACTS, SEARCH_ID, WORKFLOW, fingerprint, trip_dates


PROJECT = "hk-trip-planner"
IDLE_SECONDS = 180
TERMINAL_WAIT = 25


class TripWorker(CoallocatedWorker):
    @property
    def staging_root(self):
        return self.data.parent / "staging"

    def stage_service(self, stage, service_id):
        super().stage_service(stage, service_id)
        config = stage / "tuxiv.conf"
        text = config.read_text(encoding="utf-8")
        if text.count("name: mpi-gemm-v1") != 1:
            raise ValueError("Unexpected tcloud template")
        text = text.replace("name: mpi-gemm-v1", "name: proxy")
        excluded = os.environ.get("TACC_GPU_EXCLUDE", "")
        if excluded:
            if not re.fullmatch(r"[A-Za-z0-9-]+(?:,[A-Za-z0-9-]+)*", excluded):
                raise ValueError("Invalid Slurm node exclusion")
            text += f"    - exclude={excluded}\n"
        config.write_text(text, encoding="utf-8")
        profile = {
            "TACC_PROJECT_SESSION": SESSION,
            "TACC_PROJECT_RUNS": RUN_ROOT,
            "TACC_PROJECT_WORKFLOW": WORKFLOW,
            "TACC_ACCOUNT_USER": self.tacc_user,
            "TACC_COURSE_ROOT": os.getenv("TACC_COURSE_ROOT", DEFAULTS["TACC_COURSE_ROOT"]),
        }
        profile["STORM_BOOTSTRAP_PREFIX"] = profile["TACC_COURSE_ROOT"] + "/common/proxy"
        for name in ("STORM_WHEELHOUSE", "STORM_EMBEDDING_SOURCE"):
            if os.getenv(name):
                profile[name] = os.environ[name]
        (stage / "launch-profile.sh").write_text(
            "".join(f"export {name}={shlex.quote(value)}\n" for name, value in profile.items()),
            encoding="utf-8",
        )

    def stage_request(self, run):
        request = super().stage_request(run)
        parent_id = request.get("parent_run_id")
        if parent_id:
            parent = self.store.get_run(parent_id)
            if parent["status"] == "SUCCEEDED":
                root = self.store.artifact_dir(parent_id, parent["attempt_id"])
                plan = load(root / "itinerary.json", {})
                request["prior_draft"] = plan.get("draft")
                old = parent["request"]["trip"]
                trip = request["trip"]
                if all(old[field] == trip[field] for field in ("arrival", "departure", "gateway", "party_size")):
                    evidence = load(root / "evidence.json", {}).get("sources", [])
                    if len(evidence) <= 180:
                        request["prior_evidence"] = evidence
        return request

    def bind_submit(self, run, service):
        # The supervisor has two CPU slots. Keep additional runs unbound so they
        # can wait for the next allocation when this one starts draining.
        bound = sum(
            item.get("scheduler", {}).get("workflow_job_id") == service["service_job_id"]
            for item in self.store.list_pending_runs()
        )
        if bound >= 2:
            self.update(run, status="WAITING_MODEL", model_service=service,
                        workflow={"phase": "WAITING", "message": "等待共享 allocation 内的 CPU step"})
        else:
            super().bind_submit(run, service)

    def read_json(self, path, timeout=14):
        try:
            return super().read_json(path, timeout)
        except (OSError, subprocess.TimeoutExpired):
            with gateway_accounting.connect_gateway() as client:
                text = self.gateway_file(client, path)
            if text:
                value = json.loads(text)
                return value if isinstance(value, dict) else None

    @staticmethod
    def gateway_file(client, path, binary=False):
        if (not path.startswith(SESSION + "/") or not re.fullmatch(r"[A-Za-z0-9_./-]+", path)
                or ".." in path.split("/")):
            raise ValueError("File is outside the travel project's TACC paths")
        command = "base64 -w0 -- " if binary else "cat -- "
        userdir = os.getenv("TACC_GATEWAY_USERDIR")
        target = (shlex.quote(userdir.rstrip("/") + "/" + path) if userdir
                  else '"$HOME"/USERDIR/' + shlex.quote(path))
        stdin, stdout, _ = client.exec_command(command + target, timeout=15)
        stdin.close()
        output, stderr, status = gateway_accounting.read_channel(
            stdout.channel, timeout=16, max_bytes=5_500_000 if binary else 600_000)
        if status:
            if not binary and "No such file" in stderr:
                return None
            raise OSError("TACC gateway could not read a travel project file")
        return base64.b64decode(output, validate=True) if binary else output

    def environment_ready(self):
        service = load(self.service_path, {})
        return service.get("supervisor", {}).get("environment_ready") is True

    def ensure_service(self):
        previous = load(self.service_path, {})
        # v0.4.1 can exit zero after a local credential-read failure. That exact
        # transcript predates any SSH channel, so the corrected release may submit.
        if previous.get("release") != ROOT.name and self.failed_before_connection(previous):
            atomic(self.service_path, dict(previous, submission_state="NOT_SUBMITTED"))
        if previous.get("failure") and previous.get("scheduler_state") in SCHED_TERMINAL:
            waiting_on_failure = any(
                item["model_service"].get("model_service_id") == previous["model_service_id"]
                and not item["scheduler"].get("workflow_job_id")
                for item in self.store.list_pending_runs()
            )
            if previous.get("release") != ROOT.name or not waiting_on_failure:
                finished = dict(previous)
                finished.pop("failure")
                atomic(self.service_path, finished)
        result = super().ensure_service()
        if result.get("model_service_id") != previous.get("model_service_id"):
            result["release"] = ROOT.name
            atomic(self.service_path, result)
        return result

    def failed_before_connection(self, service):
        log = self.service_path.with_suffix(".submit.log")
        if (service.get("job_id") or service.get("submission_state") != "UNKNOWN" or not log.is_file()
                or log.stat().st_mtime < service.get("submitted_at", 0) - 1):
            return False
        lines = log.read_text(encoding="utf-8").splitlines()
        return (len(lines) == 2 and all(line.startswith("[Tcloud Error]") for line in lines)
                and "Failed to read AuthFile" in lines[0] and "Failed to create remote session" in lines[1])

    def refresh_accounting(self):
        try:
            super().refresh_accounting()
        except (OSError, ValueError):
            pass
        except Exception as exc:
            self.log_error("watcher", exc)
        snapshot = load(self.data / "gateway-latest.json", {})
        for record in snapshot.get("records", []):
            if record.get("user") != self.tacc_user:
                continue
            previous = self.accounting.get(record["job_id"], {})
            terminal = record["state"] in SCHED_TERMINAL
            if previous.get("state") in SCHED_TERMINAL and not terminal:
                continue
            if terminal or record["observed_at"] >= previous.get("observed_at", 0):
                self.accounting[record["job_id"]] = record
        terminal = [value for value in self.accounting.values() if value["state"] in SCHED_TERMINAL]
        atomic(self.data / "terminal-accounting.json", {"schema_version": 1, "records": terminal})

    def poll_service(self, service):
        job_id = service.get("service_job_id") or service.get("job_id")
        if not job_id:
            candidate = load(self.data / "reconciled-service.json", {})
            if (candidate.get("model_service_id") == service["model_service_id"]
                    and re.fullmatch(r"[0-9]+", candidate.get("job_id", ""))):
                job_id = candidate["job_id"]
                service.update(job_id=job_id, submission_state="SUBMITTED", readiness="STARTING")
            else:
                return service
        service["service_job_id"] = job_id
        account = self.accounting.get(job_id, {})
        state = account.get("state")
        if state not in SCHED_TERMINAL and time.time() - account.get("observed_at", 0) >= 65:
            state = self.queue(job_id)
        service["scheduler_state"] = state or "UNKNOWN"
        if state in SCHED_TERMINAL:
            service["readiness"] = "STOPPED" if state == "CANCELLED" else "UNAVAILABLE"
            if state in {"FAILED", "OUT_OF_MEMORY", "NODE_FAIL", "BOOT_FAIL"} and not service.get("base_url"):
                service["failure"] = f"模型 allocation {job_id} 在服务就绪前结束（{state}）"
        else:
            snapshot = self.read_json(f"{SESSION}/services/{job_id}.json")
            if snapshot and snapshot.get("model_service_id") == service["model_service_id"]:
                service["supervisor"] = snapshot
            snapshot = service.get("supervisor", {})
            if snapshot.get("failure"):
                service.update(failure=snapshot["failure"], readiness="UNAVAILABLE")
            elif (service.get("release") != ROOT.name or service.get("cancel_requested_at")
                  or snapshot.get("draining") or snapshot.get("stopping")):
                service["readiness"] = "DRAINING"
            elif (state == "RUNNING" and snapshot.get("startup_reported")
                  and snapshot.get("environment_ready")
                  and time.time() - snapshot.get("updated_at", 0) < 45):
                address = snapshot.get("base_url", "")
                if not re.fullmatch(r"http://[A-Za-z0-9.-]+:8000/v1", address):
                    raise ValueError("Invalid model address reported by supervisor")
                service["base_url"] = address
                if service["readiness"] != "READY":
                    service["readiness"] = "STARTUP_REPORTED"
            else:
                service["readiness"] = "STARTING" if state in {"PENDING", "CONFIGURING", "RUNNING"} else "UNKNOWN"
        atomic(self.service_path, service)
        return service

    def stop_service(self, service):
        job = service.get("service_job_id", "")
        if service.get("owner") != SESSION or not re.fullmatch(r"[0-9]+", job):
            return
        if any(item.get("scheduler", {}).get("workflow_job_id") == job for item in self.store.list_pending_runs()):
            return
        if service.get("cancel_requested_at"):
            return
        service["readiness"] = "DRAINING"
        atomic(self.service_path, service)
        status, output = self.command(["cancel", "-j", job], timeout=20)
        if status or "[Tcloud Error]" in output:
            raise RuntimeError("tcloud 未确认回收请求")
        service["cancel_requested_at"] = time.time()
        atomic(self.service_path, service)

    def tick(self):
        super().tick()
        service = load(self.service_path, {})
        if not service.get("service_job_id") and service.get("submission_state") in {"SUBMITTING", "UNKNOWN"}:
            for run in self.store.list_pending_runs():
                if not run.get("scheduler", {}).get("workflow_job_id") and not run.get("cancel_requested"):
                    self.update(run, status="WAITING_MODEL", model_service=service,
                                workflow={"phase": "WAITING", "message": "正在核对模型 allocation 的提交记录"})
        if service.get("readiness") in {"READY", "STARTUP_REPORTED", "STARTING", "DRAINING"}:
            pending = self.store.list_pending_runs()
            idle = not pending and time.time() - self.last_active > IDLE_SECONDS
            draining = service["readiness"] == "DRAINING" and service.get("scheduler_state") in {"PENDING", "RUNNING"}
            if idle or draining:
                self.stop_service(service)

    def collect(self, run, remote, manifest):
        if (manifest.get("run_id") != run["run_id"] or manifest.get("attempt_id") != run["attempt_id"]
                or manifest.get("version") != run["request"]["version"] or manifest.get("workflow") != WORKFLOW
                or manifest.get("model_service_id") != run["model_service"].get("model_service_id")):
            raise ValueError("Travel result identity mismatch")
        dest = self.store.artifact_dir(run["run_id"], run["attempt_id"])
        if load(dest / "result.json") == manifest:
            return
        artifacts = manifest["artifacts"]
        names = {item["path"] for item in artifacts}
        required = set(ARTIFACTS) - {"failure.txt"}
        if (not names.issubset(ARTIFACTS) or len(names) != len(artifacts)
                or (manifest["workflow_state"] == "COMPLETED" and not required.issubset(names))):
            raise ValueError("Travel result artifact list is invalid")
        dest.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=self.data) as temporary:
            bundle = Path(temporary) / "artifacts.zip"
            path = remote + "/artifacts.zip"
            try:
                status, output = self.command(["download", path, str(bundle)], timeout=22)
                if status or "[Tcloud Error]" in output or not bundle.is_file():
                    raise OSError("tcloud did not download the travel result")
            except (OSError, subprocess.TimeoutExpired):
                with gateway_accounting.connect_gateway() as client:
                    bundle.write_bytes(self.gateway_file(client, path, binary=True))
            if bundle.stat().st_size > 4_000_000:
                raise ValueError("Travel result bundle is not readable")
            if hashlib.sha256(bundle.read_bytes()).hexdigest() != manifest["bundle_sha256"]:
                raise ValueError("Travel result bundle digest mismatch")
            with zipfile.ZipFile(bundle) as archive:
                if set(archive.namelist()) != names or len(archive.namelist()) != len(names):
                    raise ValueError("Unexpected entries in travel artifacts")
                for item in artifacts:
                    name = item["path"]
                    if archive.getinfo(name).file_size > 4_000_000:
                        raise ValueError("Travel artifact exceeded size bound")
                    data = archive.read(name)
                    if hashlib.sha256(data).hexdigest() != item["sha256"]:
                        raise ValueError("Travel artifact digest mismatch")
                    (dest / name).write_bytes(data)
        atomic(dest / "result.json", manifest)

    def serve_search(self, run):
        run_id, attempt = run["run_id"], run["attempt_id"]
        state = self.data / "search" / run_id / attempt
        delivered = state / "delivered"
        if delivered.is_file():
            return
        remote = f"{RUN_ROOT}/{run_id}/{attempt}"
        request = self.read_json(f"{remote}/search-request-{SEARCH_ID}.json", timeout=12)
        if not request:
            return
        trip = run["request"]["trip"]
        queries = request.get("queries")
        if (request.get("run_id") != run_id or request.get("attempt_id") != attempt
                or request.get("request_id") != SEARCH_ID or request.get("trip") != trip
                or not isinstance(queries, dict) or set(queries) != {"transport", "lodging", "activities"}
                or not all(isinstance(value, str) and len(value) <= 250 for value in queries.values())):
            raise ValueError("Invalid travel tool request")
        state.mkdir(parents=True, exist_ok=True)
        signature = fingerprint(request)
        path = state / ("search-response-" + SEARCH_ID + ".json")
        response = load(path)
        if not response:
            evidence, errors = search(queries, trip, trip_dates(trip))
            response = {"request_id": SEARCH_ID, "request_sha256": signature, "evidence": evidence, "errors": errors}
            atomic(path, response)
        if response["request_sha256"] != signature:
            raise ValueError("Request ID already used with a different payload")
        if self.store.get_run(run_id)["attempt_id"] != attempt:
            return
        status, output = self.command(["upload", str(path), remote + "/"], timeout=23)
        if status or "[Tcloud Error]" in output or "total size is" not in output:
            raise RuntimeError("Unable to deliver official search results to the CPU step")
        delivered.touch()

    def bridge_loop(self, stop_event):
        while not stop_event.is_set():
            for run in self.store.list_pending_runs():
                if run["status"] not in {"RUNNING", "QUEUED", "UNKNOWN"} or run.get("cancel_requested"):
                    continue
                if run.get("scheduler", {}).get("workflow_job_id"):
                    try:
                        self.serve_search(run)
                    except Exception as exc:
                        self.log_error("search:" + run["run_id"], exc)
            stop_event.wait(3)

    def accounting_loop(self, stop_event):
        while not stop_event.is_set():
            try:
                service = load(self.service_path, {})
                job_id = service.get("service_job_id") or service.get("job_id")
                if job_id and service.get("scheduler_state") not in SCHED_TERMINAL:
                    _, result = gateway_accounting.query(self.data.parent, [job_id], record=False)
                    if result["returncode"] == 0:
                        atomic(self.data / "gateway-latest.json", result)
                elif not job_id and service.get("submission_state") in {"UNKNOWN", "SUBMITTING"}:
                    self.reconcile_service(service)
            except Exception as exc:
                self.log_error("gateway-accounting", exc)
            stop_event.wait(TERMINAL_WAIT)

    def reconcile_service(self, service):
        name = service["model_service_id"]
        if service["owner"] != SESSION or not re.fullmatch(r"qwen-combined-[0-9a-f]{12}", name):
            raise ValueError("Unrecognized service name")
        with gateway_accounting.connect_gateway() as client:
            username = shlex.quote(self.tacc_user)
            stdin, stdout, _ = client.exec_command(
                f"squeue -h -u {username} -n {name} -o '%i|%j|%T'", timeout=15)
            stdin.close()
            data, _, status = gateway_accounting.read_channel(stdout.channel, timeout=20)
        matches = [line.split("|")[0] for line in data.splitlines()
                   if re.fullmatch(r"[0-9]+\|" + re.escape(name) + r"\|[A-Z_]+", line)]
        if status == 0 and len(matches) == 1:
            atomic(self.data / "reconciled-service.json", {"model_service_id": name, "job_id": matches[0]})

    def log_error(self, context, exc):
        with (self.data / "worker-errors.log").open("a", encoding="utf-8") as stream:
            stream.write(f"{time.time()} {context} {type(exc).__name__} {str(exc)[:320]}\n")

    def run(self, stop_event):
        import fcntl

        with (self.data / "worker.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            for target in (self.bridge_loop, self.accounting_loop):
                threading.Thread(target=target, args=(stop_event,), daemon=True).start()
            while not stop_event.is_set():
                try:
                    self.tick()
                except Exception as exc:
                    self.log_error("controller", exc)
                stop_event.wait(5)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--port", type=int, default=os.getenv("HK_TRIP_API_PORT", "8766"))
    args = parser.parse_args()
    if (os.environ.get("TACC_PROJECT_SESSION") != PROJECT
            or os.environ.get("TACC_PROJECT_RUNS") != f"{PROJECT}/runs"):
        raise ValueError("Trip project namespace was not configured")
    store = TripStore(args.data_dir)
    host = os.environ.get("HK_TRIP_API_BIND", "127.0.0.1")
    server = make_server(store, host=host, port=args.port, mode="hk-trip")
    stop_event = threading.Event()
    worker = TripWorker(store)

    def stop(_signum, _frame):
        stop_event.set()
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    threading.Thread(target=worker.run, args=(stop_event,), daemon=True).start()
    try:
        server.serve_forever()
    finally:
        stop_event.set()
        server.server_close()


if __name__ == "__main__":
    main()
