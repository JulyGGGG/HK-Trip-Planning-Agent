import io
import json
import logging
import sys
import tempfile
import unittest
from datetime import date, time
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from trip.store import TripStore

try:
    import streamlit as st
    from streamlit.testing.v1 import AppTest
except ModuleNotFoundError as exc:
    if exc.name != "streamlit":
        raise
    AppTest = None


@unittest.skipUnless(AppTest, "Streamlit test dependency is not installed")
class TripLanguageCase(unittest.TestCase):
    def setUp(self):
        parent = ROOT / "evidence" / "test-tmp"
        parent.mkdir(parents=True, exist_ok=True)
        work = tempfile.TemporaryDirectory(prefix="trip-ui-", dir=parent)
        self.addCleanup(work.cleanup)
        self.store = TripStore(work.name)
        archive = ROOT / "tests" / "fixtures" / "bilingual_trip"
        self.artifacts = {name: json.loads((archive / name).read_bytes())
                          for name in ("itinerary.json", "evidence.json")}
        self.posts = []
        urlopen = patch("urllib.request.urlopen", side_effect=self.api)
        urlopen.start()
        self.addCleanup(urlopen.stop)
        old_logging = logging.root.manager.disable
        logging.disable(logging.WARNING)
        self.addCleanup(logging.disable, old_logging)
        self.app = AppTest.from_file(str(ROOT / "trip" / "ui.py"))

    def api(self, request, timeout):
        path = urlsplit(request.full_url).path
        parts = path.strip("/").split("/")
        if request.data is None:
            if path == "/api/runs":
                value = {"runs": self.store.list_runs()}
            elif len(parts) == 3:
                value = self.store.get_run(parts[2])
            elif len(parts) == 4 and parts[3] == "artifacts":
                value = {"artifacts": [{"path": name, "url": f"{path}/{index}"}
                                       for index, name in enumerate(self.artifacts)]}
            elif len(parts) == 5 and parts[3] == "artifacts":
                value = list(self.artifacts.values())[int(parts[4])]
            else:
                self.fail(f"Unexpected API path {path}")
        else:
            body = json.loads(request.data)
            key = request.get_header("Idempotency-key")
            self.posts.append((path, body, key))
            try:
                if path == "/api/runs":
                    value, _ = self.store.create_run(body, key)
                elif parts[3] == "revisions":
                    value, _ = self.store.revise_run(parts[2], body, key)
                elif parts[3] == "retry":
                    value = self.store.retry_run(parts[2])
                elif parts[3] == "cancel":
                    value = self.store.request_cancel(parts[2])
                else:
                    self.fail(f"Unexpected API action {path}")
            except ValueError as exc:
                data = json.dumps({"error": str(exc)}).encode("utf-8")
                raise HTTPError(request.full_url, 400, "Bad Request", {}, io.BytesIO(data)) from exc
        return io.BytesIO(json.dumps(value).encode("utf-8"))

    def saved_run(self, status, **fields):
        run, _ = self.store.create_run({"workflow": "hk_trip", "trip": self.artifacts["itinerary.json"]["trip"]},
                                       "saved")
        self.store.update_run(run["run_id"], status=status, **fields)
        self.app.query_params.update(run=run["run_id"], lang="en")
        self.app.run()
        self.assertFalse(self.app.exception)
        return run

    def test_switch_retains_draft_and_submits_from_english(self):
        self.app.query_params["lang"] = "invalid"
        self.app.run()
        self.assertEqual(self.app.title[0].value, "香港行程规划")
        initial_key = self.app.query_params["submit_key"][0]
        self.app.date_input[0].set_value(date(2026, 11, 21))
        self.app.date_input[1].set_value(date(2026, 11, 22))
        self.app.time_input[0].set_value(time(10))
        self.app.number_input[0].set_value(4)
        self.app.selectbox[0].set_value("west_kowloon")
        self.app.text_input[0].set_value("history、室内")
        self.app.text_input[1].set_value("Hong Kong Museum of History")
        self.app.radio[0].set_value("low")
        self.app.radio(key="app_language").set_value("en").run()
        self.assertFalse(self.app.exception)
        self.assertEqual(self.app.title[0].value, "Hong Kong trip planner")
        self.assertEqual(self.app.query_params["submit_key"], [initial_key])
        self.assertFalse(self.posts)
        self.app.button(key="generate").click().run()
        self.assertFalse(self.app.exception)
        path, body, key = self.posts[0]
        self.assertEqual((path, key), ("/api/runs", initial_key))
        self.assertEqual(body["trip"], {
            "arrival": "2026-11-21T10:00+08:00", "departure": "2026-11-22T20:00+08:00",
            "party_size": 4, "budget_hkd": "3500.00", "gateway": "west_kowloon", "walking": "low",
            "interests": ["history", "室内"], "must_visit": ["Hong Kong Museum of History"],
        })
        run_id = self.app.query_params["run"][0]
        self.assertIn("Waiting for compute resources", [item.value for item in self.app.text])
        # Reopen the link to exercise the same path as a browser refresh.
        self.app = AppTest.from_file(str(ROOT / "trip" / "ui.py"))
        self.app.query_params.update(lang="en", run=run_id)
        self.app.run()
        self.app.radio(key="app_language").set_value("zh-CN").run()
        self.assertEqual(self.app.query_params["run"], [run_id])
        self.assertEqual(len(self.posts), 1)
        self.app.button(key="cancel").click().run()
        self.assertEqual(self.posts[-1][0], f"/api/runs/{run_id}/cancel")
        self.assertTrue(self.store.get_run(run_id)["cancel_requested"])

    def test_validation_uses_selected_language(self):
        self.app.query_params["lang"] = "en"
        self.app.run()
        self.app.date_input[1].set_value(self.app.date_input[0].value)
        self.app.button(key="generate").click().run()
        self.assertEqual(self.app.error[0].value, "Choose a trip spanning 2–4 Hong Kong calendar days.")
        self.assertFalse(self.store.list_runs())

    def test_saved_run_download_and_revision_language(self):
        with patch("streamlit.download_button", wraps=st.download_button) as download:
            run = self.saved_run("SUCCEEDED")
            run_id = run["run_id"]
            args, kwargs = download.call_args
            self.assertEqual(kwargs["file_name"], f"{run_id}.en.md")
            self.assertTrue(args[1].startswith(b"# Hong Kong itinerary"))
            self.assertIn(b"HK$849.00", args[1])
            self.app.text_area[0].set_value("Add indoor activities")
            self.app.radio(key="app_language").set_value("zh-CN").run()
            args, kwargs = download.call_args
            self.assertEqual(kwargs["file_name"], f"{run_id}.zh-CN.md")
            self.assertTrue(args[1].decode("utf-8").startswith("# 香港旅行行程"))
            self.assertIn(b"HK$849.00", args[1])
        self.assertEqual(self.app.query_params["run"], [run_id])
        self.assertFalse(self.posts)
        self.app.button(key="revise").click().run()
        self.assertFalse(self.app.exception)
        self.assertEqual(self.posts[0][1], {"changes": {}, "feedback": "Add indoor activities"})
        revised = self.store.get_run(self.app.query_params["run"][0])
        self.assertEqual(revised["request"]["version"], 2)
        self.assertEqual(self.app.query_params["lang"], ["zh-CN"])

    def test_retry_preserves_run_and_uses_selected_language(self):
        run = self.saved_run("FAILED", error="RuntimeError · Qwen 未返回可解析的行程")
        self.assertEqual(self.app.warning[0].value, "Qwen did not return a readable itinerary.")
        self.app.button(key="retry").click().run()
        self.assertFalse(self.app.exception)
        self.assertEqual(self.app.query_params["lang"], ["en"])
        self.assertEqual(self.app.query_params["run"], [run["run_id"]])
        self.assertEqual(self.store.get_run(run["run_id"])["attempt_id"], "a2")
