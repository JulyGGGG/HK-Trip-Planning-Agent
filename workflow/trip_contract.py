"""Input shared by the application API and the compute job."""

import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation


HK = timezone(timedelta(hours=8), name="Asia/Hong_Kong")
WORKFLOW = "hk_trip"
SEARCH_ID = "research-v1"
ARTIFACTS = {
    "report.md": "report", "itinerary.json": "itinerary", "evidence.json": "evidence",
    "trace.jsonl": "trace", "metrics.json": "metrics", "diagnostics.json": "diagnostics",
    "failure.txt": "diagnostic",
}
GATEWAYS = {"airport": "香港国际机场", "west_kowloon": "西九龙站", "central": "香港站"}
PHASES = {
    "WAITING": "等候计算资源",
    "INITIALIZING": "准备 STORM",
    "RESEARCHING": "研究官方资料",
    "PLANNING": "生成行程",
    "CHECKING": "检查时间和预算",
    "REPAIRING": "修订草案",
    "COMPLETED": "行程已保存",
    "FAILED": "执行失败",
}
TRIP_FIELDS = {
    "arrival", "departure", "party_size", "budget_hkd", "interests",
    "must_visit", "walking", "gateway",
}
DATETIME_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?:\+08:00)?")
RUN_RE = re.compile(r"run-[0-9a-f]{24}")
ATTEMPT_RE = re.compile(r"a[1-9][0-9]*")


def fingerprint(payload):
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=True,
                                     separators=(",", ":")).encode("ascii")).hexdigest()


def local_time(value, label):
    if not isinstance(value, str) or not DATETIME_RE.fullmatch(value):
        raise ValueError(label + "须为香港时间，格式 YYYY-MM-DDTHH:MM")
    try:
        return datetime.fromisoformat(value).replace(tzinfo=HK)
    except ValueError as exc:
        raise ValueError(label + "无效") from exc


def trip_dates(trip):
    start = local_time(trip["arrival"], "抵港时间").date()
    end = local_time(trip["departure"], "离港时间").date()
    return [(start + timedelta(days=i)).isoformat() for i in range((end - start).days + 1)]


def hkd_cents(value):
    if type(value) not in (int, float, str) or len(str(value)) > 18:
        raise ValueError("预算须为港币数字，最多两位小数")
    try:
        amount = Decimal(str(value))
        cents = amount * 100
        if not cents.is_finite() or not 0 < cents <= 100_000_000 or cents != cents.to_integral_value():
            raise InvalidOperation
        return int(cents)
    except InvalidOperation as exc:
        raise ValueError("预算须大于零，最多 HK$1,000,000 和两位小数") from exc


def short_text(value, label, limit):
    if not isinstance(value, str) or len(value.strip()) > limit or "\x00" in value:
        raise ValueError(f"{label}须为不超过 {limit} 字的文本")
    return value.strip()


def short_list(value, label):
    if not isinstance(value, list) or len(value) > 8:
        raise ValueError(label + "须为最多 8 项的列表")
    return list(dict.fromkeys(
        item for item in (short_text(item, label, 72) for item in value) if item
    ))


def normalize_trip(data):
    if not isinstance(data, dict) or set(data) - TRIP_FIELDS:
        raise ValueError("行程资料包含无效字段")
    arrival = local_time(data.get("arrival"), "抵港时间")
    departure = local_time(data.get("departure"), "离港时间")
    if not 1 <= (departure.date() - arrival.date()).days <= 3:
        raise ValueError("请选择跨 2–4 个香港日历日的行程")
    party = data.get("party_size")
    if type(party) is not int or not 1 <= party <= 30:
        raise ValueError("人数须为 1–30 的整数")
    budget = hkd_cents(data.get("budget_hkd"))
    walk = data.get("walking", "normal")
    gateway = data.get("gateway", "airport")
    if walk not in ("low", "normal") or not isinstance(gateway, str) or gateway not in GATEWAYS:
        raise ValueError("步行偏好或抵离地点无效")
    return {
        "arrival": arrival.isoformat(timespec="minutes"),
        "departure": departure.isoformat(timespec="minutes"),
        "party_size": party,
        "budget_hkd": f"{Decimal(budget) / 100:.2f}",
        "interests": short_list(data.get("interests", []), "兴趣"),
        "must_visit": short_list(data.get("must_visit", []), "必去地点"),
        "walking": walk,
        "gateway": gateway,
    }


def normalize_submission(data):
    if not isinstance(data, dict) or set(data) != {"workflow", "trip"} or data["workflow"] != WORKFLOW:
        raise ValueError("提交须包含 workflow=hk_trip 和 trip")
    return {"workflow": WORKFLOW, "trip": normalize_trip(data["trip"]), "version": 1}


def normalize_revision(body):
    if not isinstance(body, dict) or set(body) - {"changes", "feedback"}:
        raise ValueError("修订须包含 changes 或 feedback")
    changes = body.get("changes", {})
    if not isinstance(changes, dict) or set(changes) - TRIP_FIELDS:
        raise ValueError("修订包含无效字段")
    feedback = short_text(body.get("feedback", ""), "修订要求", 500)
    if not changes and not feedback:
        raise ValueError("请提供要修改的资料或修订要求")
    return changes, feedback
