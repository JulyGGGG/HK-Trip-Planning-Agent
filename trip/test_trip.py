import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app import Conflict
from trip.controller import TripWorker
from trip.sources import place_records, route_records
from trip.store import TripStore
from workflow.trip_contract import SEARCH_ID, fingerprint, normalize_trip
from workflow.trip_presentation import EN, ZH, itinerary_view
from workflow.trip_report import report

sys.path.append(str(ROOT / "workflow"))
from trip_schedule import build_plan, parse_draft


TRIP = {
    "arrival": "2026-11-21T09:00", "departure": "2026-11-22T20:00",
    "party_size": 3, "budget_hkd": "3500.00", "interests": ["艺术"],
    "must_visit": ["香港艺术馆"], "walking": "normal", "gateway": "airport",
}
ART = """
<p>Mondays to Wednesdays, Fridays</p><p>10:00am – 6:00pm</p>
<p>Saturdays, Sundays and public holidays</p><p>10:00am – 9:00pm</p>
<p>Closed on Thursdays (except public holiday)</p>
<p>Free admission (except special exhibitions)</p>
"""
HOSTEL = """
<p>Twin Room</p><p>Check-in : After 03:00PM</p><p>Check-out : Before 11:00AM</p>
"""
STAMP = "2026-11-19T12:00:00+00:00"


def source(body):
    return (body, "https://example.com/test", None, STAMP)


def evidence():
    result = [*place_records("art-museum", source(ART)), *place_records("mei-ho-house", source(HOSTEL))]
    result.append({"id": "hk-holidays", "category": "calendar",
                   "facts": {"dates_checked": ["2026-11-21", "2026-11-22"], "holidays": {}}})
    return result


def draft(start="12:00", end="13:00"):
    return parse_draft(json.dumps({
        "lodging_id": "mei-ho-house", "visits": [
            {"place_id": "art-museum", "date": "2026-11-21", "start": start, "end": end},
        ], "alternatives": [],
    }))


class TravelRuleCase(unittest.TestCase):
    def test_party_room_rounding_and_missing_rate(self):
        plan = build_plan(normalize_trip(TRIP), draft(), evidence())
        room = plan["budget"]["items"][0]
        self.assertEqual((room["quantity"], room["unit"], room["total_cents"]), (2, "间晚", None))
        self.assertEqual(plan["status"], "INSUFFICIENT_EVIDENCE")
        self.assertTrue(any("房价" in row for row in plan["unknowns"]))

    def test_official_hours_and_transfer_constraints(self):
        early = build_plan(normalize_trip(TRIP), draft("09:20", "09:55"), evidence())
        self.assertEqual(early["status"], "CONFLICTS")
        self.assertTrue(any("官方开放时间冲突" in row for row in early["conflicts"]))
        self.assertTrue(any("预留" in row for row in early["conflicts"]))

    def test_closed_day_is_not_treated_as_unknown(self):
        trip = normalize_trip({**TRIP, "arrival": "2026-11-19T09:00", "departure": "2026-11-20T20:00"})
        records = evidence()
        records[-1]["facts"]["dates_checked"] = ["2026-11-19", "2026-11-20"]
        proposal = draft()
        proposal["visits"][0]["date"] = "2026-11-19"
        plan = build_plan(trip, proposal, records)
        self.assertTrue(any("官方开放时间冲突" in row for row in plan["conflicts"]))

    def test_operator_fare_keeps_route_and_unit(self):
        data = "ST_FROM,ST_TO,OCT_ADT_FARE\nTsingYi,Airport,66.5\nCentral,TsingYi,2\n"
        found, = route_records(source(data), {"Tsing Yi", "Airport"}, airport=True)
        self.assertEqual(found["facts"]["origin"], "Tsing Yi")
        self.assertEqual(found["facts"]["price_cents"], 6650)
        self.assertEqual(found["price_unit"], "person_trip")

    def test_rejects_nonfinite_currency_and_incorrect_offset(self):
        with self.assertRaises(ValueError):
            normalize_trip({**TRIP, "budget_hkd": "Infinity"})
        with self.assertRaises(ValueError):
            normalize_trip({**TRIP, "arrival": "2026-11-21T09:00+07:00"})


class TripPresentationCase(unittest.TestCase):
    def test_archived_and_new_reports_use_the_same_itinerary(self):
        archive = ROOT / "tests" / "fixtures" / "bilingual_trip"
        saved = json.loads((archive / "itinerary.json").read_bytes())
        sources = json.loads((archive / "evidence.json").read_bytes())
        regenerated = build_plan(saved["trip"], saved["draft"], sources["sources"])
        self.assertNotIn("messages", saved)
        self.assertIn("messages", regenerated)

        texts = {}
        for language in (ZH, EN):
            texts[language] = report(saved, sources["sources"], language, sources["source_errors"])
            self.assertEqual(texts[language], report(regenerated, sources["sources"], language, sources["source_errors"]))
            self.assertIn("HK$849.00", texts[language])
            self.assertIn("[mtr-airport-tsing-yi]", texts[language])
            self.assertIn("09:45–10:50", texts[language])
        self.assertIn("日期的房价", texts[ZH])
        self.assertIn("date-specific rates", texts[EN])
        self.assertIn("Hong Kong Museum of Art", texts[EN])
        self.assertIn("MTR Airport Express adult Octopus fares", texts[EN])

    def test_dynamic_conflicts_are_translated_without_changing_schedule(self):
        trip = normalize_trip({**TRIP, "budget_hkd": "5.00"})
        plan = build_plan(trip, draft("09:10", "10:00"), evidence())
        english = itinerary_view(plan, EN)
        self.assertEqual(english["status"], "CONFLICTS")
        self.assertTrue(any("official opening hours" in item for item in english["conflicts"]))
        self.assertTrue(any("Allow 65 minutes" in item for item in english["conflicts"]))
        self.assertIn("Known and estimated costs exceed the total HKD budget.", english["conflicts"])
        self.assertEqual(english["budget"]["priced_subtotal_cents"], plan["budget"]["priced_subtotal_cents"])
        for day, actual in zip(plan["days"], english["days"]):
            for slot, translated in zip(day["items"], actual["items"]):
                for field in ("start", "end", "source_ids"):
                    self.assertEqual(slot[field], translated[field])


class StoreAndBridgeCase(unittest.TestCase):
    def setUp(self):
        parent = ROOT / "evidence" / "test-tmp"
        parent.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="trip-", dir=parent)
        self.addCleanup(self.temp.cleanup)
        self.store = TripStore(self.temp.name)
        self.run, _ = self.store.create_run({"workflow": "hk_trip", "trip": TRIP}, "initial")

    def test_revision_is_idempotent_and_retains_confirmed_inputs(self):
        run_id = self.run["run_id"]
        self.store.update_run(run_id, status="SUCCEEDED")
        patch_data = {"changes": {"budget_hkd": 2800}, "feedback": "减少户外活动"}
        child, created = self.store.revise_run(run_id, patch_data, "revision")
        self.assertTrue(created)
        duplicate, created = TripStore(self.temp.name).revise_run(run_id, patch_data, "revision")
        self.assertFalse(created)
        self.assertEqual(child["run_id"], duplicate["run_id"])
        self.assertEqual(child["request"]["trip"]["must_visit"], ["香港艺术馆"])
        self.assertEqual(child["request"]["version"], 2)
        with self.assertRaises(Conflict):
            self.store.revise_run(run_id, patch_data, "stale")
        self.store.update_run(child["run_id"], status="FAILED")
        retried = self.store.retry_run(child["run_id"])
        self.assertEqual((retried["attempt_id"], retried["request"]["version"]), ("a2", 2))

    def test_bridge_retries_transport_from_durable_result(self):
        worker = TripWorker(self.store)
        payload = {"run_id": self.run["run_id"], "attempt_id": "a1", "request_id": SEARCH_ID,
                   "trip": self.run["request"]["trip"],
                   "queries": {"transport": "rail", "lodging": "beds", "activities": "museums"}}
        with patch.object(worker, "read_json", return_value=payload), \
                patch.object(worker, "command", side_effect=[(1, "failed"), (0, "total size is 10")]), \
                patch("trip.controller.search", return_value=([], [{"error": "TIMEOUT"}])) as source_call:
            with self.assertRaises(RuntimeError):
                worker.serve_search(self.run)
            worker.serve_search(self.run)
            worker.serve_search(self.run)
            self.assertEqual(source_call.call_count, 1)
        response = Path(self.temp.name) / "search" / self.run["run_id"] / "a1" / f"search-response-{SEARCH_ID}.json"
        self.assertEqual(json.loads(response.read_bytes())["request_sha256"], fingerprint(payload))

    def test_only_unambiguous_preconnection_failure_can_resume(self):
        worker = TripWorker(self.store)
        log = worker.service_path.with_suffix(".submit.log")
        transcript = "[Tcloud Error] Failed to read AuthFile\n[Tcloud Error] Failed to create remote session\n"
        log.write_text(transcript)
        service = {"submission_state": "UNKNOWN", "submitted_at": log.stat().st_mtime + 0.005}
        self.assertTrue(worker.failed_before_connection(service))
        log.write_text(transcript + "Unexpected additional submit output\n")
        self.assertFalse(worker.failed_before_connection(service))


if __name__ == "__main__":
    unittest.main()
