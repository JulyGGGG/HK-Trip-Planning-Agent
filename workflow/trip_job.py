"""Run an evidence-grounded Hong Kong itinerary as one Slurm step."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import signal
import time
import zipfile
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlsplit
from urllib.request import urlopen

from trip_contract import ARTIFACTS, ATTEMPT_RE, RUN_RE, SEARCH_ID, WORKFLOW, fingerprint, normalize_trip
from trip_places import PLACES
from trip_report import report
from trip_schedule import build_plan, opening_for, parse_draft, trip_dates
from workflow import Progress, atomic_json, load_json, utc_now


SEARCH_TIMEOUT = 135
MAX_REPAIRS = 3
MODEL = "qwen3.8-27b"


def request_sources(request, queries, output, progress):
    payload = {key: request[key] for key in ("run_id", "attempt_id", "trip")}
    payload.update(request_id=SEARCH_ID, queries=queries)
    signature = fingerprint(payload)
    atomic_json(output / ("search-request-" + SEARCH_ID + ".json"), payload)
    progress.event("tool_call", request_id=SEARCH_ID, tool="official_sources", queries=queries)
    reply_path = output / ("search-response-" + SEARCH_ID + ".json")
    deadline = time.monotonic() + SEARCH_TIMEOUT
    while time.monotonic() < deadline:
        if reply_path.is_file():
            reply = load_json(reply_path)
            if reply.get("request_sha256") != signature or reply.get("request_id") != SEARCH_ID:
                raise RuntimeError("官方资料响应与任务不匹配")
            if not isinstance(reply.get("evidence"), list) or not isinstance(reply.get("errors"), list):
                raise ValueError("官方资料响应格式无效")
            return reply["evidence"], reply["errors"]
        time.sleep(2)
    return [], [{"source": "tool_bridge", "error": "TIMEOUT", "message": "等待官方资料响应超时"}]


def reuse_evidence(fresh, prior):
    combined = {item["id"]: item for item in fresh}
    oldest = datetime.now(timezone.utc) - timedelta(hours=24)
    reused = []
    for item in prior:
        stamp = datetime.fromisoformat(item["fetched_at"])
        if item["id"] not in combined and stamp.tzinfo and stamp >= oldest:
            combined[item["id"]] = item
            reused.append(item["id"])
    return list(combined.values()), reused


def build_model(config, output, progress, metrics):
    import litellm
    from litellm.caching.caching import Cache
    from knowledge_storm.lm import LitellmModel

    base_url = config["base_url"].rstrip("/")
    parsed = urlsplit(base_url)
    if (parsed.scheme != "http" or not parsed.hostname or parsed.port != 8000
            or parsed.path != "/v1" or parsed.username or parsed.password or
            parsed.query or parsed.fragment or config["model"] != MODEL or
            str(config["service_job_id"]) != os.environ.get("SLURM_JOB_ID")):
        raise ValueError("模型服务与当前 allocation 不匹配")
    with urlopen(base_url + "/models", timeout=15) as response:
        if MODEL not in {row.get("id") for row in json.load(response).get("data", [])}:
            raise RuntimeError("课程模型尚未就绪")
    litellm.cache = Cache(disk_cache_dir=str(output / "client-cache"), type="disk")

    class AuditedModel(LitellmModel):
        def __call__(self, prompt=None, messages=None, **kwargs):
            if metrics["model_calls"] >= 2 + MAX_REPAIRS:
                raise RuntimeError("模型调用达到行程预算")
            if len((prompt or "").encode("utf-8")) > 22_000:
                raise ValueError("行程上下文超出模型输入上限")
            metrics["model_calls"] += 1
            call_id = metrics["model_calls"]
            start = time.monotonic()
            progress.event("model_call_start", call_id=call_id, model=MODEL)
            try:
                answers = super().__call__(prompt=prompt, messages=messages, **kwargs)
                usage = self.history[-1]["usage"]
                metrics["successful_model_calls"] += 1
                for token_type in ("prompt_tokens", "completion_tokens"):
                    metrics[token_type] += int(usage.get(token_type) or 0)
                progress.update(model_ready=True, model_calls=metrics["model_calls"])
                progress.event("model_call_end", call_id=call_id, usage=usage,
                               seconds=round(time.monotonic() - start, 3))
                return answers
            except Exception as exc:
                progress.event("model_call_error", call_id=call_id, error_type=type(exc).__name__,
                               seconds=round(time.monotonic() - start, 3))
                raise

    return AuditedModel(model="openai/" + MODEL, api_base=base_url, api_key="EMPTY", model_type="chat",
                        cache=False, temperature=0, max_tokens=2000, timeout=90, num_retries=0,
                        extra_body={"chat_template_kwargs": {"enable_thinking": False}})


def retrieve(queries, evidence, encoder_path, progress):
    import dspy
    import numpy as np
    from knowledge_storm.interface import Retriever
    from knowledge_storm.storm_wiki.modules.storm_dataclass import DialogueTurn, StormInformationTable
    from sentence_transformers import SentenceTransformer

    class OfficialSourceRM(dspy.Retrieve):
        def forward(self, query_or_queries, exclude_urls=None):
            category = query_or_queries[0].split(": ", 1)[0]
            return [{"url": f"{item['url']}#evidence-{item['id']}", "title": item["title"],
                     "description": item["category"], "snippets": [item["excerpt"]],
                     "meta": {"evidence_id": item["id"]}}
                    for item in evidence if item["category"] == category]

    retriever = Retriever(OfficialSourceRM(), max_thread=1)
    search_queries = [f"{category}: {query}" for category, query in queries.items()]
    info = retriever.retrieve(search_queries)
    if not info:
        raise RuntimeError("官方资料未返回可用的交通、住宿或景点证据")
    table = StormInformationTable([("hong-kong-travel", [DialogueTurn(search_results=info)])])
    table.encoder = SentenceTransformer(str(encoder_path), device="cpu", local_files_only=True)
    table.collected_urls = [item.url for item in info]
    table.collected_snippets = [item.snippets[0] for item in info]
    table.encoded_snippets = table.encoder.encode(table.collected_snippets)
    if not np.isfinite(table.encoded_snippets).all():
        raise RuntimeError("检索向量无效")
    selected = table.retrieve_information(search_queries, search_top_k=3)
    ids = [item.meta["evidence_id"] for item in selected]
    progress.event("storm_retrieval", selected=ids, candidates=len(info),
                   embedding_shape=list(table.encoded_snippets.shape), device="cpu")
    return ids


def candidate_context(evidence, dates, selected):
    candidates = {}
    calendar = next((item["facts"] for item in evidence if item["category"] == "calendar"), {})
    for item in evidence:
        if item["category"] not in {"activities", "lodging"}:
            continue
        facts = item["facts"]
        place_id = facts["place_id"]
        place = PLACES[place_id]
        entry = candidates.setdefault(place_id, {"id": place_id, "place": place["english"],
                                                 "category": place["category"], "area": place["area"]})
        if "hours" in facts:
            intervals = [opening_for(facts, date.fromisoformat(day), calendar) for day in dates]
            entry["opening_window_by_date_minutes_after_midnight"] = dict(zip(dates, intervals))
        if "price_cents" in facts:
            price = facts["price_cents"]
            entry["general_price_hkd"] = None if price is None else price / 100
        for name in ("check_in", "check_out"):
            if name in facts:
                entry[name + "_minutes_after_midnight"] = facts[name]
        if item["id"] in selected:
            entry["search_match"] = True
    return sorted(candidates.values(), key=lambda item: not item.get("search_match", False))


def compact(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def itinerary_format(candidates, dates):
    activities = [item["id"] for item in candidates if item["category"] == "activities"]
    lodgings = [item["id"] for item in candidates if item["category"] == "lodging"]
    if not activities:
        raise RuntimeError("官方资料未返回可选活动")
    activity = {"type": "string", "enum": activities}
    clock = {"type": "string", "pattern": "^([01][0-9]|2[0-3]):[0-5][0-9]$"}
    visit = {
        "type": "object",
        "properties": {"place_id": activity, "date": {"type": "string", "enum": dates},
                       "start": clock, "end": clock},
        "required": ["place_id", "date", "start", "end"],
        "additionalProperties": False,
    }
    structure = {
        "type": "object",
        "properties": {
            "lodging_id": {"enum": [*lodgings, None]},
            "visits": {"type": "array", "items": visit, "maxItems": 12},
            "alternatives": {"type": "array", "items": activity, "maxItems": 6},
        },
        "required": ["lodging_id", "visits", "alternatives"],
        "additionalProperties": False,
    }
    return {"type": "json_schema", "json_schema": {"name": "hong_kong_itinerary",
                                                    "strict": True, "schema": structure}}


ITINERARY_INSTRUCTIONS = """Generate a Hong Kong itinerary as exactly one JSON object.
Use lodging_id, visits, and alternatives. Each visit has place_id, date, start, and end.
Schedule at least one activity per day when the visitor's arrival and departure allow it.
Prefer free activities and include required places. An empty opening window means closed
and null means uncertain. Choose a lodging from the candidates when possible, even when
its nightly price is unknown. Select alternatives distinct from visited places.
For airport trips, allow 2h15 between arrival and the first visit and finish the last
visit at least 3h30 before departure. Leave 60 minutes between different visits and time
to return to lodging. On the final day with lodging, allow 70 minutes after 09:00 for
checkout and the first transfer. Preserve confirmed constraints and apply revision feedback.
"""


def collect_plan(request, model, output, progress, metrics):
    import dspy
    import torch

    torch.set_num_threads(max(1, min(int(os.environ.get("SLURM_CPUS_PER_TASK", "2")), 2)))
    trip = request["trip"]
    dates = trip_dates(trip)
    env_root = Path(os.environ["STORM_ENV_ROOT"])

    class ResearchQueries(dspy.Signature):
        """Write three short English keyword searches for official Hong Kong travel sources.
        Cover rail fares and routes, lodging with check-in and room types, and places to visit
        matching the visitor's interests. Return one JSON object with transport, lodging, activities.
        """
        visitor = dspy.InputField()
        queries_json = dspy.OutputField(desc='JSON such as {"transport":"MTR","lodging":"hostel","activities":"art museum"}')

    progress.update("RESEARCHING", "按交通、住宿和活动三个方向检索官方资料")
    with dspy.settings.context(lm=model, show_guidelines=False):
        prediction = dspy.Predict(ResearchQueries)(visitor=compact(trip))
    defaults = {"transport": "MTR Airport Express rail fares", "lodging": "hostel room check-in",
                "activities": "museum park " + " ".join(trip["interests"] + trip["must_visit"])}
    raw = str(prediction.queries_json)
    try:
        generated, _ = json.JSONDecoder().raw_decode(raw[raw.index("{"):])
        queries = {key: str(generated.get(key, value))[:250] for key, value in defaults.items()}
    except (ValueError, AttributeError):
        queries = {key: value[:250] for key, value in defaults.items()}

    metrics["search_calls"] += 1
    new_records, errors = request_sources(request, queries, output, progress)
    records, reused = reuse_evidence(new_records, request.get("prior_evidence", []))
    progress.event("tool_observation", request_id=SEARCH_ID, official_records=len(new_records),
                   reused_evidence_ids=reused, source_errors=errors)
    atomic_json(output / "evidence.json", {"schema_version": 1, "queries": queries, "reused_ids": reused,
                                           "sources": records, "source_errors": errors})
    selected = retrieve(queries, records, env_root / "embedding-model", progress)
    candidates = candidate_context(records, dates, selected)
    visitor = compact({"trip": trip, "dates": dates, "revision_request": request.get("feedback")})
    response_format = itinerary_format(candidates, dates)
    brief = (ITINERARY_INSTRUCTIONS + "\nVisitor: " + visitor +
             "\nOfficial place candidates: " + compact(candidates))
    feedback = "First draft for this trip."
    if request.get("prior_draft"):
        feedback = "Previous version: " + compact(request["prior_draft"])
    draft, plan = None, None
    for revision in range(MAX_REPAIRS + 1):
        progress.update("PLANNING" if revision == 0 else "REPAIRING",
                        "生成结构化行程" if revision == 0 else f"根据校验结果修订行程（第 {revision} 轮）",
                        repair_round=revision)
        [draft_json] = model(prompt=brief + "\nDraft round " + str(revision + 1) + ". " + feedback,
                             response_format=response_format)
        progress.update("CHECKING", "核对开放时间、转乘和港币预算")
        try:
            next_draft = parse_draft(draft_json)
            next_plan = build_plan(trip, next_draft, records)
        except (ValueError, TypeError) as exc:
            feedback = "Invalid itinerary JSON. Return a corrected, complete object. " + str(exc)[:250]
            progress.event("validation_feedback", round=revision, issue=feedback)
            continue
        draft, plan = next_draft, next_plan
        progress.event("validation_feedback", round=revision, conflicts=plan["conflicts"], unknowns=plan["unknowns"])
        if not plan["conflicts"]:
            break
        feedback = "Address these conflicts: " + compact(plan["conflicts"]) + "\nDraft: " + compact(draft)
    if plan is None:
        raise RuntimeError("Qwen 未返回可解析的行程")
    plan.update(schema_version=1, trip=trip, draft=draft, run_id=request["run_id"],
                attempt_id=request["attempt_id"], version=request["version"],
                trip_id=request.get("trip_id", request["run_id"]), parent_run_id=request.get("parent_run_id"))
    metrics["repair_rounds"] = revision
    atomic_json(output / "itinerary.json", plan)
    (output / "report.md").write_text(report(plan, records, source_errors=errors), encoding="utf-8")
    return plan


def publish(output, request, model_config, metrics, plan, failure):
    bundle = output / "artifacts.zip"
    artifacts = []
    with zipfile.ZipFile(bundle, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, kind in ARTIFACTS.items():
            path = output / name
            if path.is_file():
                data = path.read_bytes()
                archive.writestr(name, data)
                artifacts.append({"kind": kind, "path": name, "sha256": hashlib.sha256(data).hexdigest()})
    result = {
        "schema_version": 1, "workflow": WORKFLOW, "run_id": request["run_id"], "attempt_id": request["attempt_id"],
        "version": request["version"], "finished_at": utc_now(), "workflow_job_id": os.environ["SLURM_JOB_ID"],
        "workflow_step_id": os.environ.get("SLURM_STEP_ID"), "model_service_id": model_config.get("model_service_id"),
        "artifacts": artifacts, "bundle_sha256": hashlib.sha256(bundle.read_bytes()).hexdigest(), "metrics": metrics,
        "workflow_state": "FAILED" if failure else "COMPLETED",
    }
    if failure:
        result["failure_reason"] = failure
    if plan:
        result["plan_status"] = plan["status"]
    atomic_json(output / "result.json", result)
    print(json.dumps({key: result[key] for key in ("run_id", "workflow_state", "workflow_job_id", "workflow_step_id")}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", default="request.json")
    parser.add_argument("--model-config", default="model-config.json")
    args = parser.parse_args()
    request = load_json(args.request)
    if request.get("workflow") != WORKFLOW or not RUN_RE.fullmatch(request["run_id"]) or not ATTEMPT_RE.fullmatch(request["attempt_id"]):
        raise ValueError("Invalid trip step identity")
    request["trip"] = normalize_trip(request["trip"])
    root = (Path(os.environ["TACC_USERDIR"]) / os.environ["TACC_PROJECT_RUNS"]).resolve()
    output = (root / request["run_id"] / request["attempt_id"]).resolve()
    output.relative_to(root)
    output.mkdir(parents=True, exist_ok=True)
    if (output / "result.json").exists():
        raise RuntimeError("Attempt already published")
    model_config = load_json(args.model_config)
    start = time.monotonic()
    progress = Progress(output, request)
    metrics = dict(model_calls=0, successful_model_calls=0, search_calls=0, repair_rounds=0,
                   prompt_tokens=0, completion_tokens=0, cache_enabled=False)

    def interrupted(signum, _frame):
        raise InterruptedError("CPU step 已中断" if signum != signal.SIGALRM else "CPU step 达到运行时限")

    for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGALRM):
        signal.signal(signum, interrupted)
    signal.alarm(690)
    plan, failure = None, None
    try:
        environment = load_json(Path(os.environ["STORM_ENV_ROOT"]) / "ready.json")
        diagnostics = {"environment": environment, "model": MODEL, "rendering": "structured_itinerary"}
        if Path("native-diagnostics.json").is_file():
            diagnostics["native_slurm"] = load_json("native-diagnostics.json")
        atomic_json(output / "diagnostics.json", diagnostics)
        model = build_model(model_config, output, progress, metrics)
        plan = collect_plan(request, model, output, progress, metrics)
        progress.update("COMPLETED", "行程和来源已保存", state="COMPLETED", plan_status=plan["status"])
    except Exception as exc:
        failure = f"{type(exc).__name__} · {str(exc)[:320]}"
        progress.event("workflow_failed", error_type=type(exc).__name__, error=failure)
        progress.update("FAILED", failure, state="FAILED")
        (output / "failure.txt").write_text(failure + "\n", encoding="utf-8")
    finally:
        signal.alarm(0)
        progress.close()
    metrics["workflow_seconds"] = round(time.monotonic() - start, 3)
    atomic_json(output / "metrics.json", metrics)
    publish(output, request, model_config, metrics, plan, failure)
    return 1 if failure else 0


if __name__ == "__main__":
    raise SystemExit(main())
