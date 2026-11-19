"""Deterministic scheduling, citation and budget checks for a model draft."""

import json
import re
from collections import defaultdict
from datetime import date, datetime, timedelta

from trip_contract import HK, hkd_cents, local_time, trip_dates
from trip_places import GATEWAY_POINTS, PLACES, matching_activity
from trip_presentation import UNIT_NAMES, message, message_fields, message_text


def clock_value(value):
    if not isinstance(value, str) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
        raise ValueError("时段须为 HH:MM")
    hours, minutes = value.split(":")
    return int(hours) * 60 + int(minutes)


def parse_draft(raw):
    if not isinstance(raw, str) or len(raw) > 18_000 or "{" not in raw:
        raise ValueError("模型没有返回行程 JSON")
    draft, _ = json.JSONDecoder().raw_decode(raw[raw.index("{"):])
    if not isinstance(draft, dict):
        raise ValueError("行程应为 JSON 对象")
    visits, lodgings, alternatives = draft.get("visits"), draft.get("lodging_id"), draft.get("alternatives", [])
    if not isinstance(visits, list) or len(visits) > 12:
        raise ValueError("visits 须为最多 12 项的列表")
    if lodgings is not None and (not isinstance(lodgings, str) or len(lodgings) > 60):
        raise ValueError("lodging_id 须为旅舍 ID 或 null")
    if not isinstance(alternatives, list) or len(alternatives) > 6:
        raise ValueError("alternatives 须为列表")
    parsed = []
    for item in visits:
        if not isinstance(item, dict) or not all(isinstance(item.get(key), str) and len(item[key]) <= 60
                                                  for key in ("place_id", "date", "start", "end")):
            raise ValueError("每项活动须包含 place_id、date、start、end")
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", item["date"]):
            raise ValueError("活动日期须为 YYYY-MM-DD")
        date.fromisoformat(item["date"])
        clock_value(item["start"])
        clock_value(item["end"])
        parsed.append({key: item[key] for key in ("place_id", "date", "start", "end")})
    return {"visits": parsed, "lodging_id": lodgings,
            "alternatives": list(dict.fromkeys(x for x in alternatives if isinstance(x, str) and len(x) <= 60))}


def minute_of_day(value):
    return value.hour * 60 + value.minute


def slot(label, kind, start, end, sources=(), note=None, **fields):
    row = {
        "kind": kind, **message_fields("label", label), "start": start.isoformat(timespec="minutes"),
        "end": end.isoformat(timespec="minutes"), "source_ids": list(dict.fromkeys(sources)), **fields,
    }
    if note:
        row.update(message_fields("note", note))
    return row


def money_item(label, unit_id, quantity, unit_cents, evidence_id=None, estimated=False):
    return {
        **message_fields("label", label), "unit": UNIT_NAMES[unit_id][0], "unit_id": unit_id,
        "quantity": quantity, "unit_cents": unit_cents,
        "total_cents": None if unit_cents is None else quantity * unit_cents,
        "source_ids": [evidence_id] if evidence_id else [],
        "estimated": estimated,
    }


def transfer_details(origin, destination, fares, low_walking):
    """Set a conservative planning buffer and match rail segments to fare rows."""
    from_id, from_station, from_area = origin
    to_id, to_station, to_area = destination
    if origin == destination:
        return None
    paths = []
    if "Airport" in (from_station, to_station):
        other = to_station if from_station == "Airport" else from_station
        airport_stop = "Hong Kong" if other in {"Hong Kong", "Admiralty", "Kennedy Town"} else "Tsing Yi"
        if from_station == "Airport":
            paths = [("Airport", airport_stop), (airport_stop, other)]
        else:
            paths = [(other, airport_stop), (airport_stop, "Airport")]
        duration = 75 if low_walking else 65
    elif from_station != to_station:
        paths = [(from_station, to_station)]
        duration = 55 if low_walking else 45
    else:
        duration = 15 if from_area == to_area else 25
        if low_walking:
            duration += 5
    shuttle = "mt-davis" in (from_area, to_area)
    if shuttle:
        duration += 25
    paths = [(start, end) for start, end in paths if start != end]
    parts = [(start, end, fares.get((start, end))) for start, end in paths]
    label = message("connection", origin=from_id, destination=to_id)
    note = message("shuttle_time" if shuttle else "travel_time", route=paths)
    return label, duration, parts, note


def venue_point(place_id):
    place = PLACES[place_id]
    return place_id, place["station"], place["area"]


def opening_for(facts, day, calendar):
    if not facts.get("hours"):
        return None
    if facts.get("scope") == "outdoor park":
        return facts["hours"][day.weekday()]
    checked = calendar.get("dates_checked", [])
    if day.isoformat() not in checked or day.strftime("%m-%d") == "12-24":
        return None
    holidays = calendar["holidays"]
    label = holidays.get(day.isoformat(), "").lower()
    tomorrow = holidays.get((day + timedelta(days=1)).isoformat(), "").lower()
    if "lunar new year" in label:
        return None
    if "lunar new year" in tomorrow and "fourth" not in tomorrow:
        return None
    return facts["holiday_hours"] if label else facts["hours"][day.weekday()]


def build_plan(trip, draft, evidence):
    dates = trip_dates(trip)
    arrival = local_time(trip["arrival"], "抵港时间")
    departure = local_time(trip["departure"], "离港时间")
    from_airport = trip["gateway"] == "airport"
    travelers = trip["party_size"]
    nights = len(dates) - 1
    low_walking = trip["walking"] == "low"
    limit = hkd_cents(trip["budget_hkd"])
    calendar = {}
    place_facts = defaultdict(dict)
    place_sources = defaultdict(list)
    place_prices, fares = {}, {}
    for item in evidence:
        facts = item["facts"]
        if item["category"] == "calendar":
            calendar = facts
        elif item["category"] == "transport":
            fares[(facts["origin"], facts["destination"])] = item
        else:
            place_id = facts["place_id"]
            place_facts[place_id].update(facts)
            place_sources[place_id].append(item["id"])
            if item["price_unit"]:
                place_prices[place_id] = item

    conflicts, unknowns, items, days = [], [], [], []
    lodging_id = draft["lodging_id"]
    lodging = (lodging_id in PLACES and PLACES[lodging_id]["category"] == "lodging"
               and bool(place_facts[lodging_id].get("room_capacity")))
    if lodging:
        room = place_prices[lodging_id]
        capacity = place_facts[lodging_id]["room_capacity"]
        rooms = (travelers + capacity - 1) // capacity
        items.append(money_item(message("room_charge", place=lodging_id, rooms=rooms, nights=nights),
                                "room_night", rooms * nights,
                                room["facts"]["price_cents"], room["id"]))
        unknowns.append(message("room_terms"))
    else:
        unknowns.append(message("lodging_missing"))
        items.append(money_item(message("lodging_pending"), "night", nights, None))

    by_date = defaultdict(list)
    accepted_places = set()
    for visit in draft["visits"]:
        place_id, day = visit["place_id"], visit["date"]
        if place_id not in PLACES or PLACES[place_id]["category"] != "activities" or not place_sources[place_id]:
            conflicts.append(message("visit_source", place=place_id))
            continue
        if day not in dates:
            conflicts.append(message("visit_date", place=place_id))
            continue
        if clock_value(visit["end"]) <= clock_value(visit["start"]):
            conflicts.append(message("visit_duration", place=place_id))
            continue
        by_date[day].append(visit)
        accepted_places.add(place_id)
    if not accepted_places:
        conflicts.append(message("visits_missing"))
    for required in trip["must_visit"]:
        place_id = matching_activity(required)
        if not place_id or not place_sources[place_id]:
            unknowns.append(message("required_source", requested=required))
        elif place_id not in accepted_places:
            conflicts.append(message("required_missing", requested=required))

    gateway = (trip["gateway"], *GATEWAY_POINTS[trip["gateway"]])
    overnight = venue_point(lodging_id) if lodging else None
    for index, day in enumerate(dates):
        day_date = date.fromisoformat(day)
        midnight = datetime(day_date.year, day_date.month, day_date.day, tzinfo=HK)
        day_start = arrival if index == 0 else midnight + timedelta(hours=9)
        day_end = departure if index == len(dates) - 1 else midnight + timedelta(hours=22, minutes=30)
        prev_point = gateway if index == 0 else overnight
        previous_end = day_start
        rows = []
        if index == 0:
            previous_end += timedelta(minutes=45 if from_airport else 15)
            rows.append(slot(message("arrival_buffer"), "arrival", day_start, previous_end))
        if index == len(dates) - 1:
            day_end -= timedelta(minutes=120 if from_airport else 30)
        if index == len(dates) - 1 and lodging and place_facts[lodging_id].get("check_out") is not None:
            check_out = midnight + timedelta(minutes=place_facts[lodging_id]["check_out"])
            if day_start + timedelta(minutes=10) <= min(check_out, day_end):
                previous_end += timedelta(minutes=10)
                rows.append(slot(message("checkout"), "lodging", day_start, previous_end,
                                 place_sources[lodging_id]))
            else:
                unknowns.append(message("early_checkout"))

        def add_transfer(origin, destination, start, next_start):
            if origin is None or destination is None:
                return start
            info = transfer_details(origin, destination, fares, low_walking)
            if info is None:
                return start
            label, duration, parts, note = info
            end = start + timedelta(minutes=duration)
            if end > next_start:
                conflicts.append(message("transfer_buffer", day=day, minutes=duration, **label["values"]))
            citations = []
            for start_station, end_station, fare in parts:
                if fare:
                    citations.append(fare["id"])
                else:
                    unknowns.append(message("fare_pending", origin=start_station, destination=end_station))
                items.append(money_item(message("connection", origin=start_station, destination=end_station),
                                        "passenger_trip", travelers,
                                        fare["facts"]["price_cents"] if fare else None,
                                        fare["id"] if fare else None, estimated=True))
            rows.append(slot(label, "transfer", start, end, citations, note=note))
            return end

        for visit in sorted(by_date[day], key=lambda item: item["start"]):
            place_id = visit["place_id"]
            facts = place_facts[place_id]
            start = midnight + timedelta(minutes=clock_value(visit["start"]))
            end = midnight + timedelta(minutes=clock_value(visit["end"]))
            if start < previous_end or start < day_start or end > day_end:
                conflicts.append(message("slot_overlap", day=day, place=place_id))
            point = venue_point(place_id)
            add_transfer(prev_point, point, previous_end, start)
            opening = opening_for(facts, day_date, calendar)
            if opening is None:
                unknowns.append(message("hours_pending", day=day, place=place_id))
            elif not opening or minute_of_day(start) < opening[0] or minute_of_day(end) > opening[1]:
                conflicts.append(message("hours_conflict", day=day, place=place_id))
            price = place_prices.get(place_id)
            free_day = facts.get("free_weekday") == day_date.weekday()
            if not price and not free_day:
                unknowns.append(message("admission_pending", place=place_id))
            unit_price = 0 if free_day else price["facts"]["price_cents"] if price else None
            price_id = place_id + "-free-day" if free_day else price["id"] if price else None
            items.append(money_item(message("admission_charge", place=place_id), "admission", travelers,
                                    unit_price, price_id))
            ref_ids = place_sources[place_id]
            if calendar and facts.get("scope") != "outdoor park":
                ref_ids = ref_ids + ["hk-holidays"]
            rows.append(slot(message("place", place=place_id), "visit", start, end, ref_ids,
                             place_id=place_id, note=message("visit_note")))
            previous_end = max(previous_end, end)
            prev_point = point

        target = gateway if index == len(dates) - 1 else overnight
        final_end = add_transfer(prev_point, target, previous_end, day_end)
        if index == len(dates) - 1:
            rows.append(slot(message("departure_buffer"), "departure", day_end, departure))
        elif index == 0 and lodging:
            minimum = place_facts[lodging_id].get("check_in")
            if minimum is None:
                unknowns.append(message("checkin_pending"))
            else:
                check_in = max(final_end, midnight + timedelta(minutes=minimum))
                if check_in + timedelta(minutes=15) > day_end:
                    conflicts.append(message("checkin_conflict"))
                rows.append(slot(message("checkin", place=lodging_id), "lodging", check_in,
                                 check_in + timedelta(minutes=15), place_sources[lodging_id]))
        if final_end > day_end:
            conflicts.append(message("day_end", day=day))
        meals = sum(day_start <= midnight + timedelta(hours=hour) <= day_end for hour in (12, 19))
        if meals:
            items.append(money_item(message("meals", day=day), "meal", travelers * meals, 5_000, estimated=True))
        days.append({"date": day, "items": sorted(rows, key=lambda row: row["start"])})

    known = sum(item["total_cents"] for item in items if item["total_cents"] is not None and not item["estimated"])
    estimates = sum(item["total_cents"] for item in items if item["total_cents"] is not None and item["estimated"])
    if known + estimates > limit:
        conflicts.append(message("over_budget"))
    status = "CONFLICTS" if conflicts else ("INSUFFICIENT_EVIDENCE" if unknowns else "FEASIBLE_WITH_ESTIMATES")
    alternatives = [
        {**message_fields("name", message("place", place=key)), "source_ids": place_sources[key],
         **message_fields("note", message("alternatives_note"))}
        for key in draft["alternatives"]
        if key in PLACES and PLACES[key]["category"] == "activities" and key not in accepted_places and place_sources[key]
    ]
    plan = {"status": status, "days": days, "budget": {
        "currency": "HKD", "limit_cents": limit, "documented_cents": known, "estimated_cents": estimates,
        "priced_subtotal_cents": known + estimates, "nights": nights, "items": items,
    }, "alternatives": alternatives, "messages": {}}
    notes = [message("fare_assumption"), message("meal_assumption")]
    for group, specs in (("conflicts", conflicts), ("unknowns", unknowns), ("notes", notes)):
        by_text = {message_text(spec): spec for spec in specs}
        plan[group] = list(by_text)
        plan["messages"][group] = list(by_text.values())
    return plan
