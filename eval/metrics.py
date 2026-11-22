"""Summarise one evaluated trip run into comparable metrics.

Pure standard library. Works on the artifacts published by ``workflow/trip_job.py``:

* ``itinerary.json``  final plan, conflicts, unknowns, budget items
* ``evidence.json``   official sources used by the run
* ``metrics.json``    model calls, token counts, repair rounds, wall time
* ``trace.jsonl``     per-round validation feedback (kind == "validation_feedback")

Expectations are derived from published facts, not from model output; see
``cases.json`` for the rationale attached to each case. A case with
``"observational": true`` records numbers without asserting a verdict.
"""

import json
from collections import Counter


def _load(text):
    return json.loads(text) if isinstance(text, str) else text


def itinerary_of(artifacts):
    raw = artifacts.get("itinerary.json")
    return _load(raw) if raw else None


def evidence_of(artifacts):
    raw = artifacts.get("evidence.json")
    return _load(raw) if raw else None


def metrics_of(artifacts):
    raw = artifacts.get("metrics.json")
    return _load(raw) if raw else {}


def trace_rows(artifacts):
    raw = artifacts.get("trace.jsonl") or ""
    rows = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue
    return rows


def round_feedback(rows):
    """Map repair round -> number of conflicts reported before that round's repair."""
    rounds = {}
    for row in rows:
        if row.get("kind") != "validation_feedback":
            continue
        index = row.get("round")
        if not isinstance(index, int):
            continue
        conflicts = row.get("conflicts") or []
        kinds = [item.get("kind") for item in conflicts if isinstance(item, dict)]
        rounds[index] = {"conflicts": len(conflicts), "kinds": kinds,
                         "unknowns": len(row.get("unknowns") or [])}
    return rounds


def conflict_kinds(itinerary):
    """Machine-readable conflict keys.

    ``itinerary.json`` stores two views of every message: ``conflicts`` holds the
    localised text for humans, and ``messages.conflicts`` holds ``{"key", "values"}``
    specs for programs. Read the specs first and fall back to an empty list so an
    older artifact without ``messages`` simply reports no kinds.
    """
    return _message_keys(itinerary, "conflicts")


def unknown_kinds(itinerary):
    return _message_keys(itinerary, "unknowns")


def _message_keys(itinerary, group):
    messages = (itinerary or {}).get("messages") or {}
    specs = messages.get(group) or []
    return [item["key"] for item in specs
            if isinstance(item, dict) and isinstance(item.get("key"), str)]


def budget_summary(itinerary):
    budget = (itinerary or {}).get("budget") or {}
    items = budget.get("items") or []
    priced = [item for item in items if item.get("total_cents") is not None]
    cited = [item for item in items if item.get("source_ids")]
    priced_cited = [item for item in priced if item.get("source_ids")]
    return {
        "limit_cents": budget.get("limit_cents"),
        "priced_subtotal_cents": budget.get("priced_subtotal_cents"),
        "estimated_cents": budget.get("estimated_cents"),
        "documented_cents": budget.get("documented_cents"),
        "nights": budget.get("nights"),
        "items": len(items),
        "priced_items": len(priced),
        "cited_items": len(cited),
        "priced_cited_items": len(priced_cited),
        "citation_coverage": round(len(cited) / len(items), 4) if items else None,
        "priced_citation_coverage": round(len(priced_cited) / len(priced), 4) if priced else None,
    }


def source_summary(evidence):
    sources = (evidence or {}).get("sources") or []
    by_category = Counter(item.get("category") for item in sources)
    errors = (evidence or {}).get("source_errors") or []
    return {"sources": len(sources), "by_category": dict(by_category),
            "source_errors": len(errors)}


def summarize(case, state=None, artifacts=None):
    """Return one flat record for CSV/JSON output."""
    artifacts = artifacts or {}
    state = state or {}
    itinerary = itinerary_of(artifacts)
    evidence = evidence_of(artifacts)
    metrics = metrics_of(artifacts)
    rows = trace_rows(artifacts)
    rounds = round_feedback(rows)
    first = rounds.get(0, {})

    record = {
        "case": case.get("id"),
        "category": case.get("category"),
        "run_id": state.get("run_id"),
        "attempt_id": state.get("attempt_id"),
        "run_status": state.get("status"),
        "error": (state.get("error") or "")[:200] or None,
        "plan_status": (itinerary or {}).get("status"),
        "conflicts": len(conflict_kinds(itinerary)),
        "conflict_kinds": ",".join(conflict_kinds(itinerary)),
        "unknowns": len((itinerary or {}).get("unknowns") or []),
        "unknown_kinds": ",".join(unknown_kinds(itinerary)),
        "first_round_conflicts": first.get("conflicts"),
        "first_round_kinds": ",".join(first.get("kinds") or []),
        "repair_rounds": metrics.get("repair_rounds"),
        "model_calls": metrics.get("model_calls"),
        "prompt_tokens": metrics.get("prompt_tokens"),
        "completion_tokens": metrics.get("completion_tokens"),
        "workflow_seconds": metrics.get("workflow_seconds"),
        "e2e_seconds": _e2e(state),
        "days": len(((itinerary or {}).get("days") or [])),
        "alternatives": len(((itinerary or {}).get("alternatives") or [])),
    }
    record.update(budget_summary(itinerary))
    record.update(source_summary(evidence))
    record.update(check(record, case.get("expect") or {}))
    return record


def _e2e(state):
    execution = state.get("execution") or {}
    started = execution.get("started_at")
    finished = execution.get("finished_at")
    if isinstance(started, (int, float)) and isinstance(finished, (int, float)):
        return round(finished - started, 3)
    return None


def check(record, expect):
    """Compare one record with its expectation; return verdict fields."""
    if expect.get("observational"):
        return {"expectation": "observational", "verdict": "skip",
                "verdict_detail": "recorded without an assertion"}
    if record.get("run_status") != "SUCCEEDED":
        return {"expectation": "run", "verdict": "fail",
                "verdict_detail": "run did not succeed: %s" % (record.get("error") or record.get("run_status"))}

    kinds = [kind for kind in (record.get("conflict_kinds") or "").split(",") if kind]

    if "conflict_free" in expect:
        want = bool(expect["conflict_free"])
        got = record.get("conflicts") == 0
        return {"expectation": "conflict_free=%s" % want,
                "verdict": "pass" if got == want else "fail",
                "verdict_detail": "conflicts=%s kinds=%s" % (record.get("conflicts"), kinds or "none")}

    if "conflict_kinds_include" in expect:
        missing = [kind for kind in expect["conflict_kinds_include"] if kind not in kinds]
        return {"expectation": "kinds_include=%s" % ",".join(expect["conflict_kinds_include"]),
                "verdict": "fail" if missing else "pass",
                "verdict_detail": "missing=%s observed=%s" % (missing or "none", kinds or "none")}

    if "conflict_kinds_any" in expect:
        hit = [kind for kind in expect["conflict_kinds_any"] if kind in kinds]
        return {"expectation": "kinds_any=%s" % ",".join(expect["conflict_kinds_any"]),
                "verdict": "pass" if hit else "fail",
                "verdict_detail": "matched=%s observed=%s" % (hit or "none", kinds or "none")}

    if "unknowns_min" in expect:
        want = int(expect["unknowns_min"])
        got = int(record.get("unknowns") or 0)
        return {"expectation": "unknowns>=%d" % want,
                "verdict": "pass" if got >= want else "fail",
                "verdict_detail": "unknowns=%d" % got}

    return {"expectation": "none", "verdict": "skip", "verdict_detail": "no expectation defined"}


def aggregate(records):
    """Aggregate per-case records into the numbers quoted in the report."""
    scored = [row for row in records if row.get("verdict") in ("pass", "fail")]
    passed = [row for row in scored if row["verdict"] == "pass"]
    by_category = {}
    for row in records:
        bucket = by_category.setdefault(row.get("category"), {"cases": 0, "pass": 0, "fail": 0, "skip": 0})
        bucket["cases"] += 1
        bucket[{"pass": "pass", "fail": "fail"}.get(row.get("verdict"), "skip")] += 1

    def mean(field):
        values = [row[field] for row in records if isinstance(row.get(field), (int, float))]
        return round(sum(values) / len(values), 3) if values else None

    statuses = Counter(row.get("plan_status") for row in records)
    return {
        "cases": len(records),
        "scored": len(scored),
        "passed": len(passed),
        "failed": len(scored) - len(passed),
        "pass_rate": round(len(passed) / len(scored), 4) if scored else None,
        "observational": sum(1 for row in records if row.get("verdict") == "skip"),
        "plan_status_counts": dict(statuses),
        "conflict_free_cases": sum(1 for row in records if row.get("conflicts") == 0),
        "cases_repaired": sum(1 for row in records
                              if (row.get("first_round_conflicts") or 0) > 0 and row.get("conflicts") == 0),
        "mean_model_calls": mean("model_calls"),
        "mean_prompt_tokens": mean("prompt_tokens"),
        "mean_completion_tokens": mean("completion_tokens"),
        "mean_workflow_seconds": mean("workflow_seconds"),
        "mean_e2e_seconds": mean("e2e_seconds"),
        "mean_citation_coverage": mean("citation_coverage"),
        "by_category": by_category,
    }