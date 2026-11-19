"""Local contract tests: SQLite/API behavior only; no SSH, jobs, or model calls.

Run from the repository root:
    python -m unittest discover -s course_project/e2e/tests -p test_app.py -v
"""
from __future__ import annotations

import concurrent.futures
import json
import sys
import tempfile
import threading
import unittest
from http.client import HTTPConnection
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen


E2E = Path(__file__).resolve().parents[1]
TEMP_ROOT = E2E / "evidence" / "test-tmp"
sys.path.insert(0, str(E2E))
import app
from trip.store import TripStore
from trip_requests import trip_request


class StoreCase(unittest.TestCase):
    def setUp(self):
        TEMP_ROOT.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(prefix="store-", dir=TEMP_ROOT)
        self.data_dir = Path(self.temporary.name)
        self.store = TripStore(self.data_dir)

    def tearDown(self):
        Path(self.temporary.name).resolve().relative_to(TEMP_ROOT.resolve())
        self.temporary.cleanup()

    def create(self, key="request-1"):
        return self.store.create_run(trip_request(), key)[0]

    def result(self, state, entries=None, **changes):
        root = self.store.artifact_dir(state["run_id"], state["attempt_id"])
        root.mkdir(parents=True, exist_ok=True)
        (root / "report.md").write_text("# Report\nEvidence [1].\n", encoding="utf-8")
        manifest = {"run_id": state["run_id"], "attempt_id": state["attempt_id"],
                    "artifacts": entries if entries is not None else [
                        {"kind": "report", "path": "report.md"}]}
        manifest.update(changes)
        (root / "result.json").write_text(json.dumps(manifest), encoding="utf-8")
        return root

    def test_duplicate_request_returns_original_run_after_worker_progress(self):
        original = self.create()
        self.store.update_run(original["run_id"], expected_attempt_id="a1", status="QUEUED",
                              scheduler={"workflow_job_id": "12345", "state": "PENDING"})
        repeated, created = self.store.create_run(trip_request(budget_hkd=3500, walking="normal"), "request-1")
        self.assertFalse(created)
        self.assertEqual(repeated["run_id"], original["run_id"])
        self.assertEqual(repeated["scheduler"]["workflow_job_id"], "12345")
        self.assertEqual(len(self.store.list_runs()), 1)

    def test_same_key_different_input_conflicts_without_mutation(self):
        original = self.create()
        with self.assertRaises(app.Conflict):
            self.store.create_run(trip_request(party_size=3), "request-1")
        self.assertEqual(self.store.get_run(original["run_id"]), original)

    def test_concurrent_duplicate_requests_create_exactly_one_run(self):
        barrier = threading.Barrier(12)

        def submit(_):
            barrier.wait(timeout=10)
            return self.store.create_run(trip_request(), "shared-key")

        with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
            results = list(pool.map(submit, range(12)))
        self.assertEqual(sum(created for _, created in results), 1)
        self.assertEqual(len({state["run_id"] for state, _ in results}), 1)
        self.assertEqual(len(self.store.list_runs()), 1)

    def test_concurrent_claim_has_one_winner(self):
        state = self.create()
        barrier = threading.Barrier(10)

        def claim(_):
            barrier.wait(timeout=10)
            return self.store.claim_run(state["run_id"])

        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
            claims = list(pool.map(claim, range(10)))
        self.assertEqual(sum(claims), 1)
        self.assertEqual(self.store.get_run(state["run_id"])["status"], "PREPARING")

    def test_cancel_before_claim_blocks_submission_and_remains_intent(self):
        state = self.create()
        cancelled = self.store.request_cancel(state["run_id"])
        self.assertTrue(cancelled["cancel_requested"])
        self.assertEqual(cancelled["status"], "ACCEPTED")
        self.assertFalse(self.store.claim_run(state["run_id"]))
        self.assertIsNone(cancelled["scheduler"]["workflow_job_id"])

    def test_cancel_does_not_claim_running_job_has_stopped(self):
        state = self.create()
        self.store.update_run(state["run_id"], status="RUNNING",
                              scheduler={"workflow_job_id": "12345", "state": "RUNNING"})
        cancelled = self.store.request_cancel(state["run_id"])
        self.assertTrue(cancelled["cancel_requested"])
        self.assertEqual(cancelled["scheduler"]["state"], "RUNNING")
        self.assertNotIn(cancelled["status"], app.TERMINAL)

    def test_cancel_on_confirmed_terminal_run_is_noop(self):
        state = self.create()
        terminal = self.store.update_run(state["run_id"], status="SUCCEEDED")
        self.assertEqual(self.store.request_cancel(state["run_id"]), terminal)

    def test_unknown_and_running_cannot_be_retried(self):
        state = self.create()
        for status in ["ACCEPTED", "SUBMITTING", "QUEUED", "RUNNING", "UNKNOWN"]:
            with self.subTest(status=status):
                self.store.update_run(state["run_id"], status=status)
                with self.assertRaises(app.Conflict):
                    self.store.retry_run(state["run_id"])
                self.assertIn(state["run_id"], [run["run_id"] for run in self.store.list_pending_runs()])

    def test_retry_preserves_history_and_rejects_stale_attempt_updates(self):
        original = self.create()
        old_root = self.result(original)
        self.store.update_run(original["run_id"], status="FAILED",
                              scheduler={"workflow_job_id": "12345", "state": "FAILED"},
                              error={"kind": "model_timeout"})
        retried = self.store.retry_run(original["run_id"])
        self.assertEqual(retried["run_id"], original["run_id"])
        self.assertEqual(retried["attempt_id"], "a2")
        self.assertEqual(retried["status"], "ACCEPTED")
        self.assertIsNone(retried["scheduler"]["workflow_job_id"])
        self.assertEqual(retried["history"][0]["scheduler"]["workflow_job_id"], "12345")
        self.assertNotIn("error", retried)
        with self.assertRaises(app.Conflict):
            self.store.update_run(original["run_id"], expected_attempt_id="a1", status="SUCCEEDED")
        self.assertEqual(self.store.get_run(original["run_id"]), retried)
        self.assertEqual(self.store.artifacts(original["run_id"]), [])
        self.assertTrue((old_root / "report.md").exists())

    def test_restart_keeps_job_identity_and_does_not_reclaim_submitted_work(self):
        original = self.create()
        submitted = self.store.update_run(original["run_id"], status="UNKNOWN",
                                          scheduler={"workflow_job_id": "12345", "state": None},
                                          model_service={"service_job_id": "12340", "readiness": "READY"})
        restarted = TripStore(self.data_dir)
        self.assertEqual(restarted.get_run(original["run_id"]), submitted)
        self.assertFalse(restarted.claim_run(original["run_id"]))
        repeated, created = restarted.create_run(trip_request(), "request-1")
        self.assertFalse(created)
        self.assertEqual(repeated["scheduler"]["workflow_job_id"], "12345")

    def test_worker_cannot_change_task_identity_or_input(self):
        state = self.create()
        for field in ["run_id", "attempt_id", "created_at", "request", "history"]:
            # run_id is also protected by Python's duplicate-argument check.
            with self.subTest(field=field), self.assertRaises((ValueError, TypeError)):
                self.store.update_run(state["run_id"], **{field: "replacement"})
        self.assertEqual(self.store.get_run(state["run_id"]), state)

    def test_valid_artifacts_are_current_attempt_only(self):
        state = self.create()
        self.result(state)
        files = self.store.artifacts(state["run_id"])
        self.assertEqual({item["path"] for item in files}, {"report.md", "result.json"})
        self.assertTrue(all(state["run_id"] in item["url"] for item in files))

    def test_manifest_of_another_run_or_attempt_is_rejected(self):
        state = self.create()
        for changes in [{"run_id": "run-" + "0" * 24}, {"attempt_id": "a2"}]:
            with self.subTest(changes=changes):
                self.result(state, **changes)
                with self.assertRaises(ValueError):
                    self.store.artifacts(state["run_id"])

    def test_manifest_cannot_reference_files_outside_attempt(self):
        state = self.create()
        for path in ["../state.sqlite3", "nested/../../state.sqlite3", "/etc/passwd",
                     "..\\state.sqlite3", "C:\\Users\\secret.txt", "nested/execution.log"]:
            with self.subTest(path=path):
                self.result(state, entries=[{"path": path}])
                with self.assertRaises(ValueError):
                    self.store.artifacts(state["run_id"])

    def test_manifest_must_be_an_object(self):
        state = self.create()
        root = self.result(state)
        for value in [None, [], "not a manifest", 7]:
            with self.subTest(value=value):
                (root / "result.json").write_text(json.dumps(value), encoding="utf-8")
                with self.assertRaises(ValueError):
                    self.store.artifacts(state["run_id"])

    def test_artifact_symlink_cannot_escape_attempt_directory(self):
        state = self.create()
        root = self.result(state)
        secret = self.data_dir / "private.txt"
        secret.write_text("not a result", encoding="utf-8")
        try:
            (root / "outside.txt").symlink_to(secret)
        except (OSError, NotImplementedError):
            self.skipTest("Host does not permit symbolic-link creation")
        self.result(state, entries=[{"path": "outside.txt"}])
        with self.assertRaises(ValueError):
            self.store.artifacts(state["run_id"])

    def test_payload_rejects_untrusted_execution_fields_and_wrong_types(self):
        invalid = [None, [], {}, {**trip_request(), "command": "unexpected"},
                   trip_request(arrival=" "), trip_request(gateway="../private"),
                   trip_request(party_size=True), trip_request(party_size=0), trip_request(party_size=31),
                   trip_request(interests={}), trip_request(interests=None)]
        for payload in invalid:
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                self.store.create_run(payload, "invalid")
        self.assertEqual(self.store.list_runs(), [])

    def test_missing_or_control_character_idempotency_keys_are_rejected(self):
        for key in [None, "", "x\ny", "x" * 161]:
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.store.create_run(trip_request(), key)
        self.assertEqual(self.store.list_runs(), [])

    def test_public_state_omits_internal_top_level_fields(self):
        state = self.create()
        state.update(private_key_path="private", submission_command="private", api_key="private")
        response = app.public_run(state)
        self.assertEqual(response["run_id"], state["run_id"])
        self.assertFalse({"private_key_path", "submission_command", "api_key"} & response.keys())


class HttpCase(unittest.TestCase):
    def setUp(self):
        TEMP_ROOT.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(prefix="http-", dir=TEMP_ROOT)
        self.store = TripStore(self.temporary.name)
        self.server = app.make_server(self.store, port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = "http://127.0.0.1:" + str(self.server.server_port)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        Path(self.temporary.name).resolve().relative_to(TEMP_ROOT.resolve())
        self.temporary.cleanup()

    def request(self, path, method="GET", payload=None, headers=None, raw=None):
        values = {"Connection": "close"}
        values.update(headers or {})
        data = raw
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            values.setdefault("Content-Type", "application/json")
        request = Request(self.base + path, data=data, method=method, headers=values)
        try:
            response = urlopen(request, timeout=5)
        except HTTPError as error:
            response = error
        with response:
            body = response.read()
            parsed = json.loads(body) if response.headers.get_content_type() == "application/json" else body
            return response.code, parsed, response.headers

    def create(self, key="http-request"):
        code, state, _ = self.request("/api/runs", "POST", trip_request(),
                                      {"Idempotency-Key": key})
        self.assertEqual(code, 202)
        return state

    def test_create_read_refresh_and_duplicate_are_async_and_durable(self):
        created = self.create()
        self.assertEqual(created["status"], "ACCEPTED")
        self.assertIsNone(created["scheduler"]["workflow_job_id"])
        duplicate = self.create()
        self.assertFalse(duplicate["created"])
        self.assertEqual(duplicate["run_id"], created["run_id"])
        for _ in range(3):
            code, state, headers = self.request(created["status_url"])
            self.assertEqual(code, 200)
            self.assertEqual(state["run_id"], created["run_id"])
            self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(len(self.store.list_runs()), 1)

    def test_conflict_invalid_request_and_missing_task_have_http_errors(self):
        self.create()
        code, _, _ = self.request("/api/runs", "POST", trip_request(party_size=3),
                                  {"Idempotency-Key": "http-request"})
        self.assertEqual(code, 409)
        code, _, _ = self.request("/api/runs", "POST", trip_request())
        self.assertEqual(code, 400)
        code, _, _ = self.request("/api/runs", "POST", trip_request(walking=[]),
                                  {"Idempotency-Key": "invalid-preference"})
        self.assertEqual(code, 400)
        code, _, _ = self.request("/api/runs/run-" + "0" * 24)
        self.assertEqual(code, 404)
        self.assertEqual(len(self.store.list_runs()), 1)

    def test_cancel_is_intent_and_unconfirmed_run_cannot_be_retried(self):
        created = self.create()
        code, state, _ = self.request(created["status_url"] + "/cancel", "POST", {})
        self.assertEqual(code, 202)
        self.assertTrue(state["cancel_requested"])
        self.assertNotEqual(state["status"], "CANCELLED")
        code, _, _ = self.request(created["status_url"] + "/retry", "POST", {})
        self.assertEqual(code, 409)

    def test_foreign_host_and_cross_site_post_are_rejected(self):
        code, _, _ = self.request("/api/health", headers={"Host": "attacker.example"})
        self.assertEqual(code, 403)
        code, _, _ = self.request("/api/runs", "POST", trip_request(),
                                  {"Origin": "https://attacker.example", "Idempotency-Key": "foreign"})
        self.assertEqual(code, 403)
        self.assertEqual(self.store.list_runs(), [])

    def test_non_json_and_malformed_json_are_rejected(self):
        code, _, _ = self.request("/api/runs", "POST", raw=b"workflow=hk_trip",
                                  headers={"Content-Type": "application/x-www-form-urlencoded"})
        self.assertEqual(code, 415)
        code, _, _ = self.request("/api/runs", "POST", raw=b"{broken",
                                  headers={"Content-Type": "application/json"})
        self.assertEqual(code, 400)
        self.assertEqual(self.store.list_runs(), [])

    def test_health_is_read_only(self):
        code, _, _ = self.request("/")
        self.assertEqual(code, 404)
        code, health, _ = self.request("/api/health")
        self.assertEqual(code, 200)
        self.assertTrue(health["ok"])
        self.assertEqual(health["mode"], "hk-trip")
        self.assertEqual(self.store.list_runs(), [])

    def test_valid_artifact_download_uses_plain_text_and_nosniff(self):
        created = self.create()
        root = self.store.artifact_dir(created["run_id"], "a1")
        root.mkdir(parents=True)
        report = "# Evidence\n<script>not executed</script>"
        (root / "report.md").write_bytes(report.encode("utf-8"))
        (root / "result.json").write_text(json.dumps({"run_id": created["run_id"], "attempt_id": "a1",
                                                      "artifacts": [{"kind": "report", "path": "report.md"}]}), encoding="utf-8")
        code, listing, _ = self.request(created["status_url"] + "/artifacts")
        self.assertEqual(code, 200)
        entry = next(item for item in listing["artifacts"] if item["kind"] == "report")
        code, body, headers = self.request(entry["url"])
        self.assertEqual(code, 200)
        self.assertEqual(body.decode(), report)
        self.assertEqual(headers.get_content_type(), "text/plain")
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")

    def test_cross_attempt_manifest_returns_conflict_without_file_content(self):
        created = self.create()
        root = self.store.artifact_dir(created["run_id"], "a1")
        root.mkdir(parents=True)
        (root / "result.json").write_text(json.dumps({"run_id": created["run_id"], "attempt_id": "a9",
                                                      "artifacts": []}), encoding="utf-8")
        code, body, _ = self.request(created["status_url"] + "/artifacts")
        self.assertEqual(code, 409)
        self.assertIn("error", body)

    def test_failed_run_log_download_is_plain_text(self):
        created = self.create()
        self.store.update_run(created['run_id'], status='FAILED')
        root = self.store.artifact_dir(created['run_id'], 'a1')
        root.mkdir(parents=True)
        content = b'Traceback: <script>not executed</script>\n'
        (root / 'execution.log').write_bytes(content)
        (root / 'result.json').write_text(json.dumps({
            'run_id': created['run_id'], 'attempt_id': 'a1', 'workflow_state': 'FAILED',
            'artifacts': [{'kind': 'diagnostic', 'path': 'execution.log'}]}), encoding='utf-8')
        code, listing, _ = self.request(created['status_url'] + '/artifacts')
        self.assertEqual(code, 200)
        entry = next(item for item in listing['artifacts'] if item['path'] == 'execution.log')
        code, body, headers = self.request(entry['url'])
        self.assertEqual((code, body), (200, content))
        self.assertEqual(headers.get_content_type(), 'text/plain')
        self.assertEqual(headers['X-Content-Type-Options'], 'nosniff')

    def test_wildcard_listener_checks_the_address_that_received_the_request(self):
        with app.make_server(self.store, host="0.0.0.0", port=0) as server:
            self.assertEqual(server.server_address[0], "0.0.0.0")
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                port = server.server_port
                for address, host, expected in [
                    ("127.0.0.2", f"127.0.0.2:{port}", 200),
                    ("127.0.0.1", f"localhost:{port}", 200),
                    ("127.0.0.2", f"127.0.0.1:{port}", 403),
                    ("127.0.0.2", f"0.0.0.0:{port}", 403),
                ]:
                    with self.subTest(address=address, host=host):
                        connection = HTTPConnection(address, port, timeout=5)
                        try:
                            connection.request("GET", "/api/health", headers={"Host": host})
                            with connection.getresponse() as response:
                                self.assertEqual(response.status, expected)
                        finally:
                            connection.close()
            finally:
                server.shutdown()
                thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
