"""Run the Hong Kong trip evaluation cases against a live controller.

Usage (controller and TACC allocation already running; see README):

    python -B -m eval.run_eval --cases eval/cases.json --out eval/results

Each case is submitted once with a unique Idempotency-Key, polled until the run
reaches a terminal state, and its published artifacts are summarised by
``eval/metrics.py``. Outputs:

    results/results.jsonl   one record per case
    results/results.csv     the same records as a table
    results/summary.json    aggregates quoted in the project report

No third-party packages are required.
"""

import argparse
import csv
import json
import os
from pathlib import Path
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from eval.metrics import aggregate, summarize  # noqa: E402

TERMINAL = {"SUCCEEDED", "FAILED", "CANCELLED"}
ARTIFACT_KINDS = ("itinerary", "evidence", "metrics", "trace", "report")


def call(method, url, body=None, headers=None, timeout=30):
    data = None if body is None else json.dumps(body).encode("utf-8")
    request = Request(url, data=data, method=method,
                      headers={"Content-Type": "application/json", **(headers or {})})
    with urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def call_text(url, timeout=30):
    with urlopen(Request(url, method="GET"), timeout=timeout) as response:
        return response.read().decode("utf-8", errors="replace")


def load_cases(path):
    document = json.loads(Path(path).read_text(encoding="utf-8"))
    cases = document["cases"]
    seen = set()
    for case in cases:
        if case["id"] in seen:
            raise ValueError("duplicate case id: " + case["id"])
        seen.add(case["id"])
    return cases


def select(cases, args):
    chosen = cases
    if args.only:
        wanted = {item.strip() for item in args.only.split(",") if item.strip()}
        chosen = [case for case in chosen if case["id"] in wanted]
    if args.category:
        chosen = [case for case in chosen if case["category"] == args.category]
    chosen = chosen[args.offset:]
    if args.limit:
        chosen = chosen[:args.limit]
    return chosen


def submit(base_url, case, stamp):
    key = "eval-%s-%s" % (case["id"], stamp)
    return call("POST", base_url + "/api/runs",
                {"workflow": "hk_trip", "trip": case["trip"]},
                headers={"Idempotency-Key": key})


def wait(base_url, run_id, timeout, poll):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        state = call("GET", base_url + "/api/runs/" + run_id)
        phase = (state.get("workflow") or {}).get("phase")
        if (state.get("status"), phase) != last:
            last = (state.get("status"), phase)
            print("    %-14s %s" % (state.get("status"), phase), flush=True)
        if state.get("status") in TERMINAL:
            return state
        time.sleep(poll)
    raise TimeoutError("run %s did not reach a terminal state in %ss" % (run_id, timeout))


def fetch_artifacts(base_url, run_id):
    listing = call("GET", base_url + "/api/runs/%s/artifacts" % run_id)
    collected = {}
    for entry in listing.get("artifacts") or []:
        if entry.get("kind") not in ARTIFACT_KINDS and entry.get("path") not in (
                "itinerary.json", "evidence.json", "metrics.json", "trace.jsonl", "report.md"):
            continue
        name = entry["path"]
        try:
            collected[name] = call_text(base_url + entry["url"])
        except (HTTPError, URLError, TimeoutError) as exc:
            print("    artifact %s unavailable: %s" % (name, exc), flush=True)
    return collected


def write_outputs(records, out_dir):
    out_dir.mkdir(parents=True, exist_ok=True)
    with (out_dir / "results.jsonl").open("w", encoding="utf-8") as stream:
        for record in records:
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")

    columns = ["case", "category", "run_status", "plan_status", "verdict", "expectation",
               "verdict_detail", "conflicts", "conflict_kinds", "unknowns",
               "first_round_conflicts", "repair_rounds", "model_calls",
               "prompt_tokens", "completion_tokens", "workflow_seconds", "e2e_seconds",
               "days", "alternatives", "citation_coverage", "priced_citation_coverage",
               "sources", "limit_cents", "priced_subtotal_cents", "estimated_cents", "run_id"]
    with (out_dir / "results.csv").open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for record in records:
            writer.writerow(record)

    summary = aggregate(records)
    (out_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return summary


def print_summary(summary, records):
    print("")
    print("case    category                     status                  verdict")
    for record in records:
        print("%-7s %-28s %-23s %s" % (
            record["case"], record["category"], record.get("plan_status") or record.get("run_status"),
            record["verdict"]))
    print("")
    print("cases=%s scored=%s passed=%s failed=%s pass_rate=%s observational=%s"
          % (summary["cases"], summary["scored"], summary["passed"], summary["failed"],
             summary["pass_rate"], summary["observational"]))
    print("conflict_free=%s repaired=%s plan_status=%s"
          % (summary["conflict_free_cases"], summary["cases_repaired"],
             summary["plan_status_counts"]))
    print("mean model_calls=%s tokens=%s+%s workflow_s=%s e2e_s=%s citation_coverage=%s"
          % (summary["mean_model_calls"], summary["mean_prompt_tokens"],
             summary["mean_completion_tokens"], summary["mean_workflow_seconds"],
             summary["mean_e2e_seconds"], summary["mean_citation_coverage"]))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default=os.getenv("HK_TRIP_API", "http://127.0.0.1:8766"))
    parser.add_argument("--cases", default="eval/cases.json")
    parser.add_argument("--out", default="eval/results")
    parser.add_argument("--only", default="", help="comma separated case ids")
    parser.add_argument("--category", default="", help="run one category only")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--poll", type=float, default=15.0)
    parser.add_argument("--timeout", type=float, default=2400.0)
    parser.add_argument("--dry-run", action="store_true", help="validate cases without calling the API")
    args = parser.parse_args(argv)

    cases = select(load_cases(args.cases), args)
    if not cases:
        raise SystemExit("no cases selected")
    print("selected %d case(s) from %s" % (len(cases), args.cases))
    if args.dry_run:
        for case in cases:
            print("  %-6s %-28s %s" % (case["id"], case["category"], case["trip"]["arrival"]))
        return 0

    base_url = args.base_url.rstrip("/")
    health = call("GET", base_url + "/api/health")
    print("controller: %s" % health)

    stamp = time.strftime("%Y%m%dT%H%M%S")
    records = []
    for index, case in enumerate(cases, 1):
        print("[%d/%d] %s (%s)" % (index, len(cases), case["id"], case["category"]), flush=True)
        try:
            state = submit(base_url, case, stamp)
            print("    run %s" % state["run_id"], flush=True)
            state = wait(base_url, state["run_id"], args.timeout, args.poll)
            artifacts = fetch_artifacts(base_url, state["run_id"])
            record = summarize(case, state, artifacts)
        except Exception as exc:  # keep going; one broken case must not stop the suite
            record = summarize(case, {"status": "CLIENT_ERROR", "error": str(exc)}, {})
        records.append(record)
        print("    -> %s (%s)" % (record.get("verdict"), record.get("verdict_detail")), flush=True)

    summary = write_outputs(records, Path(args.out))
    print_summary(summary, records)
    print("")
    print("wrote %s" % args.out)
    return 0 if summary.get("failed", 0) == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())