"""Live, bounded retrieval from the public operators used by the planner."""

import csv
import io
import json
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from workflow.trip_places import GATEWAY_POINTS, PLACES, matching_activity


MTR = "https://opendata.mtr.com.hk/data/mtr_lines_fares.csv"
AIRPORT = "https://opendata.mtr.com.hk/data/airport_express_fares.csv"
HOLIDAYS = "https://www.1823.gov.hk/common/ical/en.json"
HOSTS = {urlsplit(url).hostname for url in [MTR, AIRPORT, HOLIDAYS] + [p["url"] for p in PLACES.values()]}
MAX_SOURCE_BYTES = 4_000_000


def allowed_url(url):
    parsed = urlsplit(url)
    return (parsed.scheme == "https" and parsed.hostname in HOSTS and not parsed.username
            and not parsed.password and parsed.port in (None, 443))


class OfficialRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not allowed_url(newurl):
            raise ValueError("Redirect left the official source list")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch(url):
    if not allowed_url(url):
        raise ValueError("Not an official source URL")
    request = Request(url, headers={"User-Agent": "CSIT6000V-TripPlanner/1.0"})
    with build_opener(OfficialRedirect).open(request, timeout=9) as response:
        data = response.read(MAX_SOURCE_BYTES + 1)
        if len(data) > MAX_SOURCE_BYTES:
            raise ValueError("Official response exceeded the size bound")
        return (data.decode(response.headers.get_content_charset() or "utf-8-sig", errors="replace"),
                response.geturl(), response.headers.get("Last-Modified"), datetime.now(timezone.utc).isoformat())


class PageText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.hidden = 0
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "template", "noscript"}:
            self.hidden += 1
        elif not self.hidden and tag in {"br", "div", "p", "td", "th", "tr", "li", "h1", "h2", "h3", "dd", "dt"}:
            self.parts.append(" ")

    def handle_endtag(self, tag):
        if tag in {"script", "style", "template", "noscript"} and self.hidden:
            self.hidden -= 1
        elif not self.hidden and tag in {"div", "p", "td", "th", "tr", "li", "h1", "h2", "h3", "dd", "dt"}:
            self.parts.append(" ")

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def record(evidence_id, category, title, source, excerpt, facts=None, price_unit=None):
    _, url, updated, timestamp = source
    return {
        "id": evidence_id, "category": category, "title": title, "url": url,
        "excerpt": excerpt.strip()[:460], "fetched_at": timestamp, "source_last_modified": updated,
        "price_unit": price_unit, "facts": facts or {},
    }


def inspect_page(source):
    parser = PageText()
    parser.feed(source[0])
    return re.sub(r"\s+", " ", "".join(parser.parts)).strip()


TIME = r"(\d{1,2}(?::\d{2})?\s*(?:a\.?m\.?|p\.?m\.?)?)"


def minutes(value):
    match = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?(am|pm)?", re.sub(r"[\s.]", "", value.lower()))
    hours, mins = int(match[1]), int(match[2] or 0)
    if match[3]:
        hours = hours % 12 + (12 if match[3] == "pm" else 0)
    return hours * 60 + mins


def hours_evidence(place_id, place, source, page):
    if place_id == "hong-kong-park":
        span = re.search(r"Park\s*\(outdoor facilities\)\s*" + TIME + r"\s*[-–]\s*" + TIME, page, re.I)
        if span:
            interval = [minutes(span[1]), minutes(span[2])]
            yield record(place_id + "-hours", "activities", place["name"], source, span[0],
                         {"place_id": place_id, "hours": [interval] * 7, "holiday_hours": interval,
                          "scope": "outdoor park"})
        return
    regular_labels = {
        "art-museum": r"Mondays? to Wednesdays?,?\s*(?:and\s*)?Fridays?",
        "history-museum": r"Mondays?,?\s*Wednesdays?\s+to\s+Fridays?",
        "science-museum": r"Mondays?,\s*Tuesdays?,\s*Wednesdays?\s+and\s+Fridays?",
    }
    match_regular = re.search(regular_labels[place_id] + r"\s*[:：]?\s*" + TIME + r"\s*[-–]\s*" + TIME, page, re.I)
    match_weekend = re.search(r"Saturdays?,?\s*Sundays?(?:,?\s*and)?\s+public holidays?\s*[:：]?\s*" + TIME
                              + r"\s*[-–]\s*" + TIME, page, re.I)
    closed_day = 1 if place_id == "history-museum" else 3
    closed_name = "Tuesdays?" if closed_day == 1 else "Thursdays?"
    closure = re.search(r"(?:Closed on\s+" + closed_name + r"|" + closed_name + r".{0,45}closed)", page, re.I)
    if not (match_regular and match_weekend and closure):
        return
    regular = [minutes(match_regular[1]), minutes(match_regular[2])]
    weekend = [minutes(match_weekend[1]), minutes(match_weekend[2])]
    weekly = [regular] * 5 + [weekend] * 2
    weekly[closed_day] = []
    special = re.search(r"(?:Christmas Eve|Lunar New Year.s Eve|Chinese New Year.s Eve|Lunar New Year).{0,95}", page, re.I)
    yield record(place_id + "-hours", "activities", place["name"], source, match_regular[0],
                 {"place_id": place_id, "hours": weekly, "holiday_hours": weekend,
                  "special_hours_note": special[0] if special else None})
    yield record(place_id + "-weekend", "activities", place["name"], source, match_weekend[0],
                 {"place_id": place_id})
    yield record(place_id + "-closure", "activities", place["name"], source, closure[0],
                 {"place_id": place_id})


def place_records(place_id, source):
    place = PLACES[place_id]
    page = inspect_page(source)
    if place["category"] == "lodging":
        room = re.search(r"(?:Twin\s*(?:/|or)\s*Double Room|Twin Room|Double Room)", page, re.I)
        check_in = re.search(r"Check.in(?:\s+time)?\s*[:：]?\s*(?:After\s*)?" + TIME, page, re.I)
        check_out = re.search(r"Check.out(?:\s+time)?\s*[:：]?\s*(?:Before\s*)?" + TIME, page, re.I)
        if room:
            yield record(place_id + "-room", "lodging", place["name"], source,
                         page[max(0, room.start() - 18):room.end() + 145],
                         {"place_id": place_id, "room_capacity": 2, "price_cents": None},
                         "room_night")
        for name, match in (("check_in", check_in), ("check_out", check_out)):
            if match:
                yield record(place_id + "-" + name, "lodging", place["name"], source, match[0],
                             {"place_id": place_id, name: minutes(match[1])})
        return
    yield from hours_evidence(place_id, place, source, page)
    price_patterns = {
        "art-museum": r"Free admission\s*\(except special exhibitions?\)",
        "history-museum": r"['‘’]?\s*Hong Kong Story\s*['‘’]?\s*Permanent Exhibition\s*[:：]\s*Free",
        "hong-kong-park": r"Admission Fee\s*Free\s*(?:Except Sports Centre and Squash Centre)?",
    }
    pattern = price_patterns.get(place_id)
    price = re.search(pattern, page, re.I) if pattern else None
    if price:
        yield record(place_id + "-admission", "activities", place["name"], source, price[0],
                     {"place_id": place_id, "price_cents": 0, "scope": "general admission"}, "person_visit")
    if place_id == "science-museum":
        standard = re.search(r"Permanent Exhibition\s*Standard\s*\$(\d+(?:\.\d{1,2})?)", page, re.I)
        free_day = re.search(r"Free admission to (?:the )?Permanent Exhibition on Wednesdays", page, re.I)
        if standard:
            yield record(place_id + "-admission", "activities", place["name"], source, standard[0],
                         {"place_id": place_id, "price_cents": int(Decimal(standard[1]) * 100)}, "person_visit")
        if free_day:
            yield record(place_id + "-free-day", "activities", place["name"], source, free_day[0],
                         {"place_id": place_id, "free_weekday": 2})


def route_records(source, stations, airport=False):
    fields = ("ST_FROM", "ST_TO") if airport else ("SRC_STATION_NAME", "DEST_STATION_NAME")
    rows = csv.DictReader(io.StringIO(source[0].lstrip("\ufeff")))
    if not set((*fields, "OCT_ADT_FARE")).issubset(rows.fieldnames or []):
        raise ValueError("MTR fare fields are unavailable")
    for row in rows:
        raw_from, raw_to = row[fields[0]], row[fields[1]]
        origin, destination = [name.replace("HongKong", "Hong Kong").replace("TsingYi", "Tsing Yi").strip()
                               for name in (raw_from, raw_to)]
        if origin == destination or origin not in stations or destination not in stations:
            continue
        if airport and "Airport" not in {origin, destination}:
            continue
        try:
            fare = Decimal(row["OCT_ADT_FARE"])
            if not fare.is_finite() or fare < 0 or fare > 500:
                continue
        except InvalidOperation:
            continue
        key = re.sub(r" +", "-", (origin + "-" + destination).lower())
        title = "港鐵機場快綫八達通成人車費" if airport else "港鐵八達通成人車費"
        yield record("mtr-" + key, "transport", title, source,
                     f"{fields[0]}={raw_from}, {fields[1]}={raw_to}, OCT_ADT_FARE={row['OCT_ADT_FARE']}",
                     {"origin": origin, "destination": destination, "price_cents": int(fare * 100),
                      "airport_express": airport},
                     "person_trip")


def calendar_record(source, dates):
    events = json.loads(source[0].lstrip("\ufeff"))["vcalendar"][0]["vevent"]
    years = {event["dtstart"][0][:4] for event in events}
    if not {date[:4] for date in dates}.issubset(years):
        raise ValueError("Holiday calendar does not cover the requested dates")
    holidays = {event["dtstart"][0]: event["summary"] for event in events}
    applicable = {date: holidays[date.replace("-", "")] for date in dates if date.replace("-", "") in holidays}
    return record("hk-holidays", "calendar", "香港公眾假期（1823）", source,
                  "; ".join(f"{date} {name}" for date, name in applicable.items()) or
                  f"Public holiday calendar checked for {dates[0]} to {dates[-1]}",
                  {"dates_checked": dates, "holidays": applicable})


def search(queries, trip, dates, fetch_source=fetch):
    """Fetch official sources for a trip in parallel, then extract typed evidence."""
    must = {matching_activity(name) for name in trip["must_visit"]}
    ranked = []
    for place_id, place in PLACES.items():
        terms = (queries.get(place["category"], "") + " " + " ".join(trip["interests"])).casefold()
        hits = sum(keyword in terms for keyword in place["terms"])
        ranked.append((place_id, hits))
    ranked.sort(key=lambda item: (item[0] not in must, -item[1]))
    selected = [place_id for place_id, _ in ranked]
    urls = {place_id: PLACES[place_id]["url"] for place_id in selected}
    urls.update({"mtr": MTR, "calendar": HOLIDAYS})
    if trip["gateway"] == "airport":
        urls["airport"] = AIRPORT
    stations = {PLACES[place_id]["station"] for place_id in selected}
    stations.update({GATEWAY_POINTS[trip["gateway"]][0], "Tsing Yi", "Hong Kong", "Kowloon"})
    evidence, errors = [], []
    with ThreadPoolExecutor(max_workers=8) as pool:
        tasks = {name: pool.submit(fetch_source, url) for name, url in urls.items()}
        for name, task in tasks.items():
            try:
                source = task.result()
                if name == "calendar":
                    evidence.append(calendar_record(source, dates))
                elif name in {"mtr", "airport"}:
                    evidence.extend(route_records(source, stations, airport=name == "airport"))
                else:
                    found = list(place_records(name, source))
                    if not found:
                        raise ValueError("No current facts could be read from this page")
                    evidence.extend(found)
            except (ValueError, OSError, KeyError, IndexError, TypeError) as exc:
                errors.append({"source": name, "url": urls[name], "error": type(exc).__name__,
                               "message": str(exc)[:160]})
    return evidence, errors
