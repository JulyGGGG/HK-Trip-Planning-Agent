"""Atomic progress snapshots and artifact I/O shared by trip CPU steps."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import threading


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def atomic_json(path, value):
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as file:
        json.dump(value, file, ensure_ascii=False, indent=2)
        file.flush()
        os.fsync(file.fileno())
    os.replace(tmp, path)


class Progress:
    def __init__(self, output, request):
        self.output, self.request = output, request
        self.lock = threading.RLock()
        self.seq = 0
        self.phase = "INITIALIZING"
        self.message = "Preparing the STORM client"
        self.state = "RUNNING"
        self.details = {}
        self.stopped = threading.Event()
        self.update(self.phase, self.message)
        self.thread = threading.Thread(target=self.heartbeat, daemon=True)
        self.thread.start()

    def event(self, kind, **fields):
        with self.lock:
            row = {"time": utc_now(), "kind": kind, "run_id": self.request["run_id"], "attempt_id": self.request["attempt_id"], **fields}
            with (self.output / "trace.jsonl").open("a", encoding="utf-8") as file:
                file.write(json.dumps(row, ensure_ascii=False) + "\n")
                file.flush()

    def update(self, phase=None, message=None, state=None, **fields):
        with self.lock:
            self.phase = phase or self.phase
            self.message = message or self.message
            self.state = state or self.state
            self.details.update(fields)
            self.seq += 1
            atomic_json(self.output / "progress.json", {
                "schema_version": 1, "run_id": self.request["run_id"],
                "attempt_id": self.request["attempt_id"], "seq": self.seq,
                "updated_at": utc_now(), "workflow_state": self.state,
                "workflow_step_id": os.environ.get("SLURM_STEP_ID"),
                "phase": self.phase, "message": self.message, **self.details,
            })

    def heartbeat(self):
        while not self.stopped.wait(5):
            self.update()

    def close(self):
        self.stopped.set()
        self.thread.join(timeout=6)


def load_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))
