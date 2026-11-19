"""Localize a saved itinerary without changing its dates, prices or citations."""

import re
from string import Formatter

if __package__:
    from .trip_contract import GATEWAYS
    from .trip_places import PLACES
else:
    from trip_contract import GATEWAYS
    from trip_places import PLACES


ZH = "zh-CN"
EN = "en"
LANGUAGES = {ZH: "简体中文", EN: "English"}

GATEWAY_NAMES = {
    "airport": (GATEWAYS["airport"], "Hong Kong International Airport"),
    "west_kowloon": (GATEWAYS["west_kowloon"], "West Kowloon Station"),
    "central": (GATEWAYS["central"], "Hong Kong Station"),
}
STATION_NAMES = {
    "Airport": ("机场站", "Airport"),
    "Austin": ("柯士甸站", "Austin"),
    "Hong Kong": ("香港站", "Hong Kong"),
    "Admiralty": ("金钟站", "Admiralty"),
    "Kennedy Town": ("坚尼地城站", "Kennedy Town"),
    "Kowloon": ("九龙站", "Kowloon"),
    "Sham Shui Po": ("深水埗站", "Sham Shui Po"),
    "Tsim Sha Tsui": ("尖沙咀站", "Tsim Sha Tsui"),
    "Tsing Yi": ("青衣站", "Tsing Yi"),
}
_PLACE_NAMES = {key: (row["name"], row["english"]) for key, row in PLACES.items()}
_LOCATION_NAMES = {**STATION_NAMES, **GATEWAY_NAMES, **_PLACE_NAMES}
_LEGACY_LOCATION_NAMES = {zh: (zh, en) for zh, en in _LOCATION_NAMES.values()}

UNIT_NAMES = {
    "room_night": ("间晚", "room-night", "room-nights"),
    "night": ("晚", "night", "nights"),
    "passenger_trip": ("人次", "passenger trip", "passenger trips"),
    "admission": ("人次", "admission", "admissions"),
    "meal": ("人餐", "meal", "meals"),
}

PLAN_STATUS = {
    "FEASIBLE_WITH_ESTIMATES": ("已通过已知约束检查，包含估算", "Passed known constraints. Includes estimates."),
    "INSUFFICIENT_EVIDENCE": ("部分行程或费用待确认", "Some itinerary details or costs need confirmation."),
    "CONFLICTS": ("行程仍有约束冲突", "The itinerary has unresolved conflicts."),
}

PLAN_COPY = {
    "report_title": ("香港旅行行程", "Hong Kong itinerary"),
    "itinerary": ("行程", "Itinerary"),
    "budget": ("预算", "Budget"),
    "budget_limit": ("总预算", "Total budget"),
    "budget_subtotal": ("已计及估算小计", "Known and estimated subtotal"),
    "lodging": ("住宿", "Lodging"),
    "charge": ("收费项目", "Charge"),
    "quantity": ("数量", "Quantity"),
    "amount": ("金额", "Amount"),
    "basis": ("依据", "Basis"),
    "pending": ("待确认", "Pending confirmation"),
    "estimate": ("估算", "Estimate"),
    "official_data": ("官方资料", "Official data"),
    "unknowns": ("待确认", "To confirm"),
    "conflicts": ("需要调整", "Conflicts to resolve"),
    "notes": ("估算依据", "Planning assumptions"),
    "alternatives": ("备选活动", "Alternative activities"),
    "sources": ("官方来源", "Official sources"),
    "original_excerpt": ("原文摘录", "Original excerpt"),
    "fetched_at": ("抓取于 {time} UTC", "Retrieved {time} UTC"),
    "source_errors": ("部分官方来源暂无法读取，请查看待确认项。",
                      "Some official sources were unavailable. See the items to confirm."),
}

# These Chinese templates also identify messages from releases without text keys.
MESSAGES = {
    "place": ("{place}", "{place}"),
    "connection": ("{origin} → {destination}", "{origin} → {destination}"),
    "local_connection": ("同区步行或短程接驳", "Walk or take a short local ride"),
    "travel_time": ("{route}，用时为规划估算", "{route}. Estimated travel time."),
    "shuttle_time": ("{route}，另需安排旅舍接驳，用时为规划估算",
                     "{route}. Arrange a hostel shuttle. Estimated travel time."),
    "arrival_buffer": ("抵达缓冲（估算）", "Arrival buffer (estimated)"),
    "departure_buffer": ("离港手续缓冲（估算）", "Departure formalities buffer (estimated)"),
    "checkout": ("办理退房，随身携带行李", "Check out and take your luggage"),
    "checkin": ("办理入住 · {place}", "Check in at {place}"),
    "visit_note": ("普通参观，特别展览可能另收费", "General admission. Special exhibitions may cost extra."),
    "alternatives_note": ("备选活动，替换前需重新核对开放时间及路线。",
                          "Check opening hours and travel routes before substituting this activity."),
    "room_charge": ("{place} · {rooms} 间 × {nights} 晚", "{place} · {rooms} {room_unit} × {nights} {night_unit}"),
    "lodging_pending": ("住宿待确认", "Lodging to confirm"),
    "admission_charge": ("{place}普通参观", "General admission to {place}"),
    "meals": ("{day} 餐饮预留", "Meal allowance for {day}"),
    "room_terms": ("需向旅舍核对入住日期的房价、空房、会员条件及押金",
                   "Confirm date-specific rates, availability, membership terms and deposits with the hostel."),
    "lodging_missing": ("缺少有官方来源的住宿和房价，需补充住宿地点",
                        "Official lodging and rate information is missing. Add a place to stay."),
    "visit_source": ("活动缺少当前官方来源，请换用候选地点 · {place}",
                     "Current official information is missing for {place}. Choose an available alternative."),
    "visit_date": ("活动超出旅行日期 · {place}", "{place} is scheduled outside the travel dates."),
    "visit_duration": ("活动结束时间必须晚于开始时间 · {place}", "The end time for {place} must be after the start time."),
    "visits_missing": ("行程没有可核对的活动", "The itinerary has no activities that can be verified."),
    "required_source": ("待确认必去地点的官方资料 · {requested}", "Confirm official information for the must-visit place · {requested}"),
    "required_missing": ("请安排必去地点 · {requested}", "Schedule the must-visit place · {requested}"),
    "early_checkout": ("离港较早，需向旅舍确认提前退房", "Confirm early checkout with the hostel before the early departure."),
    "transfer_buffer": ("{day} {origin} → {destination} 需要预留 {minutes} 分钟",
                        "Allow {minutes} minutes for {origin} → {destination} on {day}."),
    "fare_pending": ("待确认车费 · {origin} → {destination}", "Confirm the fare for {origin} → {destination}."),
    "slot_overlap": ("{day} {place} 与上一时段重叠或超出可用时段",
                     "The visit to {place} on {day} overlaps another time slot or falls outside available hours."),
    "hours_pending": ("{day} {place} 的开放时间待核对", "Confirm opening hours for {place} on {day}."),
    "hours_conflict": ("{day} {place} 的活动时间与官方开放时间冲突",
                       "The visit to {place} on {day} falls outside official opening hours."),
    "admission_pending": ("待确认门票 · {place}", "Confirm admission charges for {place}."),
    "checkin_pending": ("需确认住宿最早入住时间", "Confirm the earliest check-in time with the hostel."),
    "checkin_conflict": ("首晚办理入住的时间超出行程时段", "First-night check-in falls outside available hours."),
    "day_end": ("{day} 的行程超出当天结束时间", "The itinerary exceeds the available time on {day}."),
    "over_budget": ("已计费用及估算支出已超出港币总预算", "Known and estimated costs exceed the total HKD budget."),
    "fare_assumption": ("车费使用公开成人价作估算。实际行车、步行和排队时间需出发前核对。",
                        "Fare estimates use published adult prices. Check travel, walking and waiting times before departure."),
    "meal_assumption": ("餐饮为每人每餐 HK$50 的规划预留。机票不计入预算。",
                        "Allow HK$50 per meal per person. Airfare is excluded from the budget."),
}


def localized(pair, language):
    return pair[int(language == EN)]


def plan_text(key, language, **values):
    return localized(PLAN_COPY[key], language).format(**values)


def hkd_text(cents, language):
    return plan_text("pending", language) if cents is None else f"HK${cents / 100:,.2f}"


def _location_name(value, language):
    names = _LOCATION_NAMES.get(value) or _LEGACY_LOCATION_NAMES.get(value)
    return localized(names, language) if names else value


def _route_name(route, language):
    if isinstance(route, str):
        if route == MESSAGES["local_connection"][0]:
            route = []
        else:
            segments = [leg.split(" → ", 1) for leg in route.split("、")]
            if any(len(leg) != 2 for leg in segments):
                return route
            route = segments
    if not route:
        return localized(MESSAGES["local_connection"], language)
    return localized(("、", ", "), language).join(
        f"{_location_name(start, language)} → {_location_name(end, language)}" for start, end in route
    )


def message(key, **values):
    return {"key": key, "values": values}


def message_text(spec, language=ZH):
    key = spec["key"]
    values = dict(spec["values"])
    for name in ("place", "origin", "destination"):
        if name in values:
            values[name] = _location_name(values[name], language)
    if "route" in values:
        values["route"] = _route_name(values["route"], language)
    if key == "room_charge":
        values["room_unit"] = "room" if int(values["rooms"]) == 1 else "rooms"
        values["night_unit"] = "night" if int(values["nights"]) == 1 else "nights"
    return localized(MESSAGES[key], language).format(**values)


def message_fields(field, spec):
    return {field: message_text(spec), field + "_message": spec}


def unit_text(unit_id, quantity, language):
    names = UNIT_NAMES[unit_id]
    return names[0] if language != EN else names[1 if quantity == 1 else 2]


def quantity_text(quantity, unit):
    return f"{quantity} {unit}"


def _legacy_pattern(key):
    fields = {"day": r"\d{4}-\d{2}-\d{2}", "rooms": r"\d+", "nights": r"\d+", "minutes": r"\d+"}
    parts = []
    for literal, name, _, _ in Formatter().parse(MESSAGES[key][0]):
        parts.append(re.escape(literal))
        if name:
            parts.append(f"(?P<{name}>{fields.get(name, '.+?')})")
    return re.compile("".join(parts), re.DOTALL)


_LEGACY_KEYS = {
    "slot": ("arrival_buffer", "departure_buffer", "checkout", "checkin", "connection", "place"),
    "slot_note": ("shuttle_time", "travel_time", "visit_note"),
    "charge": ("room_charge", "lodging_pending", "admission_charge", "meals", "connection"),
    "alternative": ("place",),
    "alternative_note": ("alternatives_note",),
    "conflicts": ("visit_source", "visit_date", "visit_duration", "visits_missing", "required_missing",
                  "transfer_buffer", "slot_overlap", "hours_conflict", "checkin_conflict", "day_end", "over_budget"),
    "unknowns": ("room_terms", "lodging_missing", "required_source", "early_checkout", "fare_pending",
                 "hours_pending", "admission_pending", "checkin_pending"),
    "notes": ("fare_assumption", "meal_assumption"),
}
_LEGACY_PATTERNS = {group: [(key, _legacy_pattern(key)) for key in keys] for group, keys in _LEGACY_KEYS.items()}


def _saved_text(value, spec, group, language):
    if spec and spec.get("key") in MESSAGES:
        return message_text(spec, language)
    for key, pattern in _LEGACY_PATTERNS[group]:
        if match := pattern.fullmatch(value):
            return message_text(message(key, **match.groupdict()), language)
    return value


def _view_row(row, field, group, language, **extra):
    updated = {**row, **extra, field: _saved_text(row[field], row.get(field + "_message"), group, language)}
    if row.get("note"):
        updated["note"] = _saved_text(row["note"], row.get("note_message"), group + "_note", language)
    return updated


def _charge_view(row, language):
    unit_id = row.get("unit_id") or {
        "间晚": "room_night", "晚": "night", "人餐": "meal",
        "人次": "passenger_trip" if " → " in row["label"] else "admission",
    }.get(row["unit"])
    unit = unit_text(unit_id, row["quantity"], language) if unit_id in UNIT_NAMES else row["unit"]
    return _view_row(row, "label", "charge", language, unit=unit)


def itinerary_view(plan, language):
    days = [{**day, "items": [_view_row(row, "label", "slot", language) for row in day["items"]]}
            for day in plan["days"]]
    budget = plan["budget"]
    view = {**plan, "days": days,
            "budget": {**budget, "items": [_charge_view(row, language) for row in budget["items"]]},
            "alternatives": [_view_row(row, "name", "alternative", language) for row in plan["alternatives"]]}
    for group in ("conflicts", "unknowns", "notes"):
        specs = plan.get("messages", {}).get(group, [])
        values = plan[group]
        if len(specs) != len(values):
            specs = [None] * len(values)
        view[group] = [_saved_text(val, spec, group, language) for val, spec in zip(values, specs)]
    return view


def source_title(record, language):
    facts = record.get("facts", {})
    if facts.get("place_id") in _PLACE_NAMES:
        return _location_name(facts["place_id"], language)
    if record["category"] == "transport":
        names = (("港鐵機場快綫八達通成人車費", "MTR Airport Express adult Octopus fares") if facts.get("airport_express")
                 else ("港鐵八達通成人車費", "MTR adult Octopus fares"))
        return localized(names, language)
    if record["id"] == "hk-holidays":
        return localized(("香港公眾假期（1823）", "Hong Kong public holidays (1823)"), language)
    return record["title"]
