"""Durable run storage and HTTP API for the trip planner."""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit


TERMINAL = {"SUCCEEDED", "FAILED", "CANCELLED", "TIMED_OUT"}
PUBLIC_FIELDS = {
    "run_id", "attempt_id", "status", "created_at", "updated_at", "request",
    "scheduler", "model_service", "workflow", "cancel_requested", "error",
    "result", "metrics", "history", "status_url", "execution",
}
ID_RE = re.compile(r"^run-[a-f0-9]{24}$")
ATTEMPT_RE = re.compile(r"^a[1-9][0-9]*$")


def now():
    return datetime.now(timezone.utc).isoformat()


class Conflict(Exception):
    pass


class Store:
    """Durable state shared by API and worker processes.

    update_run merges top-level fields atomically. Nested objects are replaced.
    expected_attempt_id prevents a stale worker from changing a retried run.
    No startup recovery action submits a job: only the worker reconciles jobs.
    """

    def __init__(self, data_dir):
        self.data_dir = Path(data_dir).resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.data_dir / "state.sqlite3"
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("""CREATE TABLE IF NOT EXISTS runs (
                run_id TEXT PRIMARY KEY, idempotency_key TEXT UNIQUE NOT NULL,
                request_hash TEXT NOT NULL, state_json TEXT NOT NULL)""")
            db.execute("""CREATE TABLE IF NOT EXISTS attempts (
                run_id TEXT NOT NULL, attempt_id TEXT NOT NULL, state_json TEXT NOT NULL,
                PRIMARY KEY(run_id, attempt_id))""")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(str(self.db_path), timeout=30)
        db.execute("PRAGMA busy_timeout=30000")
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def request_hash(payload, key):
        if not isinstance(key, str) or not 1 <= len(key) <= 160 or any(ord(c) < 32 for c in key):
            raise ValueError("必须提供有效的 Idempotency-Key 请求头")
        return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False,
                                         separators=(",", ":")).encode()).hexdigest()

    @staticmethod
    def existing_run(db, key, digest):
        prior = db.execute("SELECT request_hash,state_json FROM runs WHERE idempotency_key=?",
                           (key,)).fetchone()
        if prior:
            if prior[0] != digest:
                raise Conflict("相同幂等键对应不同请求")
            return json.loads(prior[1])

    @staticmethod
    def insert_run(db, payload, key, digest):
        run_id = "run-" + uuid.uuid4().hex[:24]
        timestamp = now()
        state = {"run_id": run_id, "attempt_id": "a1", "status": "ACCEPTED",
                 "created_at": timestamp, "updated_at": timestamp, "request": payload,
                 "cancel_requested": False, "scheduler": {"workflow_job_id": None, "state": None},
                 "model_service": {"service_job_id": None, "readiness": "UNKNOWN"},
                 "workflow": {"phase": "WAITING", "message": "已保存任务，等待后台处理"},
                 "status_url": "/api/runs/" + run_id, "history": []}
        db.execute("INSERT INTO runs VALUES (?,?,?,?)", (run_id, key, digest, json.dumps(state)))
        return state

    @staticmethod
    def validate_payload(payload):
        raise NotImplementedError("The workflow store must validate its request payload.")

    def create_run(self, payload, key):
        payload = self.validate_payload(payload)
        digest = self.request_hash(payload, key)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            prior = self.existing_run(db, key, digest)
            if prior:
                return prior, False
            return self.insert_run(db, payload, key, digest), True

    def get_run(self, run_id):
        with self.connect() as db:
            row = db.execute("SELECT state_json FROM runs WHERE run_id=?", (run_id,)).fetchone()
        if not row:
            raise KeyError(run_id)
        return json.loads(row[0])

    def list_runs(self):
        with self.connect() as db:
            rows = db.execute("SELECT state_json FROM runs ORDER BY rowid DESC").fetchall()
        return [json.loads(row[0]) for row in rows]

    def list_pending_runs(self):
        return [run for run in self.list_runs() if run["status"] not in TERMINAL]

    def update_run(self, run_id, expected_attempt_id=None, **fields):
        if set(fields) & {"run_id", "attempt_id", "created_at", "request", "history"}:
            raise ValueError("不能通过 update_run 更改任务身份或输入")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT state_json FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if not row:
                raise KeyError(run_id)
            state = json.loads(row[0])
            if expected_attempt_id is not None and state["attempt_id"] != expected_attempt_id:
                raise Conflict("旧 attempt 不得修改新的执行尝试")
            state.update(fields)
            state["updated_at"] = now()
            db.execute("UPDATE runs SET state_json=? WHERE run_id=?", (json.dumps(state), run_id))
        return state

    def claim_run(self, run_id):
        """Durable compare-and-set; only one worker can claim an ACCEPTED run."""
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT state_json FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if not row:
                raise KeyError(run_id)
            state = json.loads(row[0])
            if state["status"] != "ACCEPTED" or state["cancel_requested"]:
                return False
            state.update(status="PREPARING", updated_at=now())
            db.execute("UPDATE runs SET state_json=? WHERE run_id=?", (json.dumps(state), run_id))
            return True

    def request_cancel(self, run_id):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT state_json FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if not row:
                raise KeyError(run_id)
            state = json.loads(row[0])
            if state["status"] not in TERMINAL:
                # Cancellation is intent. Worker must confirm scheduler termination.
                state.update(cancel_requested=True, updated_at=now())
                db.execute("UPDATE runs SET state_json=? WHERE run_id=?", (json.dumps(state), run_id))
            return state

    def retry_run(self, run_id):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT state_json FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if not row:
                raise KeyError(run_id)
            prior = json.loads(row[0])
            if prior["status"] not in TERMINAL:
                raise Conflict("执行尚未确认结束；不能重复提交")
            db.execute("INSERT OR REPLACE INTO attempts VALUES (?,?,?)",
                       (run_id, prior["attempt_id"], json.dumps(prior)))
            attempt_id = "a" + str(int(prior["attempt_id"][1:]) + 1)
            state = {"run_id": run_id, "attempt_id": attempt_id, "status": "ACCEPTED",
                     "created_at": prior["created_at"], "updated_at": now(), "request": prior["request"],
                     "cancel_requested": False, "scheduler": {"workflow_job_id": None, "state": None},
                     "model_service": {"service_job_id": None, "readiness": "UNKNOWN"},
                     "workflow": {"phase": "WAITING", "message": "新执行尝试已保存"},
                     "status_url": prior["status_url"], "history": prior.get("history", []) + [
                         {"attempt_id": prior["attempt_id"], "status": prior["status"],
                          "scheduler": prior.get("scheduler"), "updated_at": prior["updated_at"]}]}
            db.execute("UPDATE runs SET state_json=? WHERE run_id=?", (json.dumps(state), run_id))
            return state

    def artifact_dir(self, run_id, attempt_id):
        if not ID_RE.fullmatch(run_id) or not ATTEMPT_RE.fullmatch(attempt_id):
            raise ValueError("无效的任务或执行尝试编号")
        path = (self.data_dir / "artifacts" / run_id / attempt_id).resolve()
        path.relative_to(self.data_dir)
        return path

    def artifacts(self, run_id):
        state = self.get_run(run_id)
        root = self.artifact_dir(run_id, state["attempt_id"])
        manifest_path = root / "result.json"
        if not manifest_path.exists():
            return []
        if manifest_path.resolve().parent != root or manifest_path.stat().st_size > 2_000_000:
            raise ValueError("结果清单无效")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(manifest, dict):
            raise ValueError("结果清单必须为 JSON 对象")
        if manifest.get("run_id") != run_id or manifest.get("attempt_id") != state["attempt_id"]:
            raise ValueError("结果清单与当前执行尝试不符")
        output = []
        entries = manifest.get("artifacts", [])
        if not isinstance(entries, list):
            raise ValueError("产物列表无效")
        for entry in entries + [{"kind": "manifest", "path": "result.json"}]:
            if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
                raise ValueError("产物条目无效")
            relative = Path(entry["path"])
            if relative.is_absolute() or ".." in relative.parts or relative.name != entry["path"] or not re.fullmatch(r"[a-zA-Z0-9_.-]+", entry["path"]):
                raise ValueError("产物路径越界")
            full = (root / relative).resolve()
            full.relative_to(root)
            if full.suffix not in {".json", ".jsonl", ".md", ".txt", ".log"} or not full.is_file():
                continue
            if full.stat().st_size > 16_000_000:
                continue
            artifact_id = str(len(output))
            output.append({"artifact_id": artifact_id, "kind": entry.get("kind", "file"),
                           "path": entry["path"], "size": full.stat().st_size,
                           "url": f"/api/runs/{run_id}/artifacts/{artifact_id}"})
        return output


def public_run(state):
    return {key: value for key, value in state.items() if key in PUBLIC_FIELDS}


def make_server(store, host="127.0.0.1", port=8766, mode="hk-trip"):
    if host not in {"127.0.0.1", "localhost", "::1", "0.0.0.0"}:
        raise ValueError("不支持的 API 监听地址")

    class Handler(BaseHTTPRequestHandler):
        server_version = "TaccE2E/1.0"

        def log_message(self, fmt, *args):
            # Avoid copying user questions or exception details into the HTTP log.
            pass

        def send_body(self, code, body, mime="application/json; charset=utf-8"):
            if not isinstance(body, bytes):
                body = json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'")
            self.end_headers()
            self.wfile.write(body)

        def allowed_host(self):
            host_header = self.headers.get("Host", "")
            address, port = self.connection.getsockname()[:2]
            expected = {f"{address}:{port}"}
            if address == "127.0.0.1":
                expected.add(f"localhost:{port}")
            return host_header in expected

        def do_GET(self):
            if not self.allowed_host():
                return self.send_body(403, {"error": "Host 不受信任"})
            path = unquote(urlsplit(self.path).path)
            try:
                if path == "/api/health":
                    return self.send_body(200, {"ok": True, "mode": mode, "time": now()})
                if path == "/api/runs":
                    return self.send_body(200, {"runs": [public_run(run) for run in store.list_runs()]})
                parts = path.strip("/").split("/")
                if len(parts) >= 3 and parts[:2] == ["api", "runs"] and ID_RE.fullmatch(parts[2]):
                    run_id = parts[2]
                    if len(parts) == 3:
                        return self.send_body(200, public_run(store.get_run(run_id)))
                    if parts[3] == "artifacts" and len(parts) == 4:
                        return self.send_body(200, {"artifacts": store.artifacts(run_id)})
                    if parts[3] == "artifacts" and len(parts) == 5:
                        artifacts = store.artifacts(run_id)
                        entry = next((item for item in artifacts if item["artifact_id"] == parts[4]), None)
                        if entry is None:
                            raise KeyError("artifact")
                        state = store.get_run(run_id)
                        root = store.artifact_dir(run_id, state["attempt_id"])
                        full = (root / entry["path"]).resolve()
                        full.relative_to(root)
                        return self.send_body(200, full.read_bytes(), "text/plain; charset=utf-8")
                self.send_body(404, {"error": "未找到"})
            except KeyError:
                self.send_body(404, {"error": "任务或产物不存在"})
            except (ValueError, OSError):
                self.send_body(409, {"error": "产物尚不可用或校验未通过"})

        def do_POST(self):
            if not self.allowed_host():
                return self.send_body(403, {"error": "Host 不受信任"})
            origin = self.headers.get("Origin")
            if origin and origin != "http://" + self.headers.get("Host", ""):
                return self.send_body(403, {"error": "不接受跨站请求"})
            if self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
                return self.send_body(415, {"error": "请求类型必须为 application/json"})
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 32_768:
                    raise ValueError("请求体为空或过大")
                body = json.loads(self.rfile.read(length))
                path = unquote(urlsplit(self.path).path)
                if path == "/api/runs":
                    state, created = store.create_run(body, self.headers.get("Idempotency-Key"))
                    return self.send_body(202, {**public_run(state), "created": created})
                parts = path.strip("/").split("/")
                if len(parts) == 4 and parts[:2] == ["api", "runs"] and ID_RE.fullmatch(parts[2]):
                    if parts[3] == "revisions" and mode == "hk-trip":
                        state, created = store.revise_run(parts[2], body, self.headers.get("Idempotency-Key"))
                        return self.send_body(202, {**public_run(state), "created": created})
                    if body != {}:
                        raise ValueError("此接口请求体应为空对象")
                    if parts[3] == "cancel":
                        return self.send_body(202, public_run(store.request_cancel(parts[2])))
                    if parts[3] == "retry":
                        return self.send_body(202, public_run(store.retry_run(parts[2])))
                self.send_body(404, {"error": "未找到"})
            except Conflict as exc:
                self.send_body(409, {"error": str(exc)})
            except KeyError:
                self.send_body(404, {"error": "任务不存在"})
            except (ValueError, UnicodeDecodeError) as exc:
                self.send_body(400, {"error": str(exc)})

    return ThreadingHTTPServer((host, port), Handler)
