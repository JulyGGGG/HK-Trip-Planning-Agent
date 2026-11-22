"""Offline tests for the evaluation harness.

These run without TACC and without a model: they validate the case file and the
metric/verdict logic against a real recorded run kept in
``eval/fixtures/sample_run/``.
"""

import json
from pathlib import Path
import unittest

from eval.metrics import (aggregate, budget_summary, check, conflict_kinds, round_feedback,
                          summarize, trace_rows, unknown_kinds)

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "sample_run"
INFEASIBLE = Path(__file__).resolve().parent / "fixtures" / "infeasible_run"
CASES = Path(__file__).resolve().parent / "cases.json"


def load_fixture(directory=FIXTURES):
    artifacts = {}
    for name in ("itinerary.json", "evidence.json", "metrics.json", "trace.jsonl", "report.md"):
        path = directory / name
        if path.is_file():
            artifacts[name] = path.read_text(encoding="utf-8")
    return artifacts


class CaseFileTest(unittest.TestCase):
    def setUp(self):
        self.document = json.loads(CASES.read_text(encoding="utf-8"))
        self.cases = self.document["cases"]

    def test_case_count_and_unique_ids(self):
        self.assertEqual(len(self.cases), 20)
        ids = [case["id"] for case in self.cases]
        self.assertEqual(len(ids), len(set(ids)))

    def test_categories_cover_the_evaluation_plan(self):
        categories = {case["category"] for case in self.cases}
        self.assertEqual(categories, {
            "ordinary", "tight_budget", "required_place_unavailable",
            "restricted_window", "missing_information", "infeasible",
        })

    def test_every_case_has_an_expectation(self):
        for case in self.cases:
            self.assertTrue(case.get("expect"), case["id"])
            self.assertTrue(case.get("note"), case["id"])

    def test_trip_fields_follow_the_contract(self):
        allowed = {"arrival", "departure", "party_size", "budget_hkd", "interests",
                   "must_visit", "walking", "gateway"}
        for case in self.cases:
            trip = case["trip"]
            self.assertEqual(set(trip), allowed, case["id"])
            self.assertIn(trip["walking"], ("low", "normal"))
            self.assertIn(trip["gateway"], ("airport", "west_kowloon", "central"))
            self.assertTrue(1 <= trip["party_size"] <= 30, case["id"])
            self.assertGreater(float(trip["budget_hkd"]), 0, case["id"])
            start = trip["arrival"][:10]
            end = trip["departure"][:10]
            from datetime import date
            span = (date.fromisoformat(end) - date.fromisoformat(start)).days
            self.assertTrue(1 <= span <= 3, "%s spans %s days" % (case["id"], span))


class InfeasibleRunTest(unittest.TestCase):
    """A recorded 1 HKD run: the plan must be CONFLICTS with an over_budget key."""

    @classmethod
    def setUpClass(cls):
        cls.artifacts = load_fixture(INFEASIBLE)
        cls.itinerary = json.loads(cls.artifacts["itinerary.json"])

    def test_conflict_keys_are_read_from_messages(self):
        self.assertEqual(conflict_kinds(self.itinerary), ["over_budget"])
        self.assertEqual(unknown_kinds(self.itinerary), ["room_terms"])

    def test_over_budget_expectation_passes(self):
        case = {"id": "F01", "category": "infeasible",
                "expect": {"conflict_kinds_include": ["over_budget"]}}
        state = {"run_id": "fixture-f01", "attempt_id": "a1", "status": "SUCCEEDED"}
        record = summarize(case, state, self.artifacts)
        self.assertEqual(record["plan_status"], "CONFLICTS")
        self.assertEqual(record["conflict_kinds"], "over_budget")
        self.assertEqual(record["verdict"], "pass")

    def test_budget_lower_bound_is_visible(self):
        self.assertEqual(self.itinerary["budget"]["limit_cents"], 100)
        self.assertGreater(self.itinerary["budget"]["priced_subtotal_cents"], 100)


class RecordedRunTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.artifacts = load_fixture()

    def test_trace_rounds_are_parsed(self):
        rounds = round_feedback(trace_rows(self.artifacts))
        self.assertIn(0, rounds)
        self.assertGreaterEqual(rounds[0]["conflicts"], 1)

    def test_summary_of_recorded_run(self):
        case = {"id": "A01", "category": "ordinary", "expect": {"conflict_free": True}}
        state = {"run_id": "fixture", "attempt_id": "a1", "status": "SUCCEEDED"}
        record = summarize(case, state, self.artifacts)
        self.assertEqual(record["plan_status"], "INSUFFICIENT_EVIDENCE")
        self.assertEqual(record["conflicts"], 0)
        self.assertEqual(record["verdict"], "pass")
        self.assertEqual(record["days"], 2)
        self.assertEqual(record["model_calls"], 3)
        self.assertGreater(record["sources"], 0)
        self.assertIsNotNone(record["citation_coverage"])
        self.assertGreater(record["priced_subtotal_cents"], 0)

    def test_budget_summary_counts_uncited_items(self):
        itinerary = json.loads(self.artifacts["itinerary.json"])
        summary = budget_summary(itinerary)
        self.assertGreater(summary["items"], 0)
        self.assertLessEqual(summary["cited_items"], summary["items"])
        self.assertEqual(summary["nights"], 1)

    def test_missing_expectation_kind_fails(self):
        record = {"run_status": "SUCCEEDED", "conflict_kinds": "", "conflicts": 0, "unknowns": 0}
        verdict = check(record, {"conflict_kinds_include": ["over_budget"]})
        self.assertEqual(verdict["verdict"], "fail")

    def test_unknown_threshold(self):
        record = {"run_status": "SUCCEEDED", "conflict_kinds": "", "conflicts": 0, "unknowns": 2}
        self.assertEqual(check(record, {"unknowns_min": 1})["verdict"], "pass")
        self.assertEqual(check(record, {"unknowns_min": 3})["verdict"], "fail")

    def test_observational_and_failed_runs(self):
        self.assertEqual(check({}, {"observational": True})["verdict"], "skip")
        failed = check({"run_status": "FAILED", "error": "boom"}, {"conflict_free": True})
        self.assertEqual(failed["verdict"], "fail")

    def test_aggregate(self):
        case = {"id": "A01", "category": "ordinary", "expect": {"conflict_free": True}}
        state = {"run_id": "fixture", "attempt_id": "a1", "status": "SUCCEEDED"}
        record = summarize(case, state, self.artifacts)
        summary = aggregate([record, dict(record, case="D01", category="restricted_window",
                                          verdict="skip", expectation="observational")])
        self.assertEqual(summary["cases"], 2)
        self.assertEqual(summary["scored"], 1)
        self.assertEqual(summary["passed"], 1)
        self.assertEqual(summary["observational"], 1)
        self.assertEqual(summary["plan_status_counts"]["INSUFFICIENT_EVIDENCE"], 2)


if __name__ == "__main__":
    unittest.main()