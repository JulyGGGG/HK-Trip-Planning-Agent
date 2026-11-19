"""Streamlit travel form and itinerary viewer backed by the controller API."""

import json
import os
import re
from datetime import datetime, time, timedelta
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import uuid4

import streamlit as st

from trip.ui_text import api_error, execution_error, phase_text, progress_text, status_text, ui_text
from workflow.trip_contract import HK, WORKFLOW
from workflow.trip_presentation import (
    GATEWAY_NAMES, LANGUAGES, PLAN_STATUS, ZH, hkd_text, itinerary_view, localized,
    plan_text, quantity_text, source_title, unit_text,
)
from workflow.trip_report import markdown_text, report


API = "http://127.0.0.1:" + os.getenv("HK_TRIP_API_PORT", "8766")
TERMINAL = {"SUCCEEDED", "FAILED", "CANCELLED", "TIMED_OUT"}
ARTIFACT_RE = re.compile(r"/api/runs/run-[0-9a-f]{24}/artifacts/[0-9]+")


def request(path, body=None, key=None, *, language=ZH):
    headers = {}
    if key:
        headers["Idempotency-Key"] = key
    encoded = None
    if body is not None:
        encoded = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    try:
        with urlopen(Request(API + path, data=encoded, headers=headers), timeout=6) as response:
            contents = response.read(4_000_001)
            if len(contents) > 4_000_000:
                raise ValueError(ui_text("response_too_large", language))
            try:
                return json.loads(contents)
            except (ValueError, UnicodeError) as exc:
                raise ValueError(ui_text("invalid_response", language)) from exc
    except HTTPError as exc:
        try:
            message = str(json.loads(exc.read(8192)).get("error") or "")
        except (ValueError, AttributeError):
            message = ""
        raise ValueError(api_error(message, exc.code, language)) from exc
    except (URLError, TimeoutError) as exc:
        raise ValueError(ui_text("api_unavailable", language)) from exc


def draft_input(widget, scope, language, name, initial, **options):
    # Streamlit 1.40 resets a widget when its label changes. Keep its value
    # separately so a language change can recreate it with the same draft input.
    draft = st.session_state.get("trip_draft")
    if not draft or draft["scope"] != scope:
        draft = {"scope": scope, "values": {}}
        st.session_state["trip_draft"] = draft
    key = f"trip-{scope}-{language}-{name}"
    if key not in st.session_state:
        st.session_state[key] = draft["values"].get(name, initial)

    def remember():
        draft["values"][name] = st.session_state[key]

    return widget(ui_text(name, language), key=key, on_change=remember, **options)


def form_fields(scope, language, base=None):
    if base:
        start, finish = (datetime.fromisoformat(base[field]) for field in ("arrival", "departure"))
    else:
        first_day = datetime.now(HK).date()
        days_to_saturday = (5 - first_day.weekday()) % 7 or 7
        first_day += timedelta(days=days_to_saturday)
        start = datetime.combine(first_day, time(9, 0))
        finish = datetime.combine(first_day + timedelta(days=1), time(20, 0))
    base = base or {}

    def field(widget, name, initial, **options):
        return draft_input(widget, scope, language, name, initial, **options)

    arrival, departure = st.columns(2)
    with arrival:
        arrival_day = field(st.date_input, "arrival_day", start.date(), value=None, format="YYYY-MM-DD")
        arrival_clock = field(st.time_input, "arrival_clock", start.time(), value=None, step=300)
    with departure:
        departure_day = field(st.date_input, "departure_day", finish.date(), value=None, format="YYYY-MM-DD")
        departure_clock = field(st.time_input, "departure_clock", finish.time(), value=None, step=300)
    gateway, size, budget = st.columns([1.4, 0.7, 1])
    with gateway:
        entry = field(st.selectbox, "gateway", base.get("gateway", "airport"), options=list(GATEWAY_NAMES), index=None,
                      format_func=lambda key: localized(GATEWAY_NAMES[key], language))
    with size:
        party = field(st.number_input, "party_size", base.get("party_size", 2),
                      min_value=1, max_value=30, value=None, step=1)
    with budget:
        allowance = field(st.number_input, "budget_hkd", float(base.get("budget_hkd", "3500")),
                          min_value=.01, max_value=1_000_000.0, value=None, step=100.0, format="%.2f")
    interests = field(st.text_input, "interests", ", ".join(base.get("interests", [])), value=None,
                      placeholder=ui_text("interests_example", language), max_chars=400)
    must_visit = field(st.text_input, "must_visit", ", ".join(base.get("must_visit", [])), value=None,
                       placeholder=ui_text("must_visit_example", language), max_chars=400)
    walking = field(st.radio, "walking", base.get("walking", "normal"), options=("normal", "low"), horizontal=True,
                    index=None, format_func=lambda key: ui_text(key, language))

    def timestamp(day, clock):
        return datetime.combine(day, clock, tzinfo=HK).isoformat(timespec="minutes") if day and clock else ""

    def entries(value):
        return [name.strip() for name in re.split(r"[,，、\n]", value or "") if name.strip()]

    return {
        "arrival": timestamp(arrival_day, arrival_clock), "departure": timestamp(departure_day, departure_clock),
        "party_size": party, "budget_hkd": f"{allowance:.2f}" if allowance is not None else "", "gateway": entry,
        "interests": entries(interests), "must_visit": entries(must_visit),
        "walking": walking,
    }


def open_run(run_id):
    st.query_params["run"] = run_id
    st.query_params.pop("submit_key", None)
    st.query_params.pop("revision_key", None)
    st.rerun()


def submit_key(name):
    key = st.query_params.get(name, "")
    if not re.fullmatch(r"[0-9a-f]{32}", key):
        key = uuid4().hex
        st.query_params[name] = key
    return key


def new_trip(language):
    st.subheader(ui_text("new_trip", language))
    st.caption(ui_text("form_hint", language))
    key = submit_key("submit_key")
    payload = form_fields(key, language)
    if st.button(ui_text("generate", language), key="generate", type="primary"):
        try:
            run = request("/api/runs", {"workflow": WORKFLOW, "trip": payload}, key, language=language)
            open_run(run["run_id"])
        except ValueError as exc:
            st.error(str(exc))


def get_artifact(items, path, language):
    url = next((item["url"] for item in items if item["path"] == path), "")
    if not ARTIFACT_RE.fullmatch(url):
        raise ValueError(ui_text("artifact_pending", language))
    return request(url, language=language)


def display_plan(run, language):
    items = request(run["status_url"] + "/artifacts", language=language)["artifacts"]
    saved_plan = get_artifact(items, "itinerary.json", language)
    evidence = get_artifact(items, "evidence.json", language)
    plan = itinerary_view(saved_plan, language)
    sources = {item["id"]: item for item in evidence["sources"]}
    cited = set()
    st.subheader(plan_text("itinerary", language))
    notice = st.success if plan["status"] == "FEASIBLE_WITH_ESTIMATES" else st.warning
    notice(localized(PLAN_STATUS[plan["status"]], language))

    days = st.tabs([day["date"] for day in plan["days"]])
    for day, container in zip(plan["days"], days):
        with container:
            for slot in day["items"]:
                times = f"{slot['start'][11:16]}–{slot['end'][11:16]}"
                st.markdown(f"**{times}** {markdown_text(slot['label'])}")
                if slot.get("note"):
                    st.caption(markdown_text(slot["note"]))
                cited.update(slot["source_ids"])

    st.subheader(plan_text("budget", language))
    budget = plan["budget"]
    metrics = st.columns(3)
    metrics[0].metric(plan_text("budget_limit", language), hkd_text(budget["limit_cents"], language))
    metrics[1].metric(plan_text("budget_subtotal", language), hkd_text(budget["priced_subtotal_cents"], language))
    nights = budget["nights"]
    metrics[2].metric(plan_text("lodging", language), quantity_text(nights, unit_text("night", nights, language)))
    rows = []
    for item in budget["items"]:
        cited.update(item["source_ids"])
        basis = "pending" if item["total_cents"] is None else "estimate" if item["estimated"] else "official_data"
        rows.append({
            plan_text("charge", language): item["label"],
            plan_text("quantity", language): quantity_text(item["quantity"], item["unit"]),
            plan_text("amount", language): hkd_text(item["total_cents"], language),
            plan_text("basis", language): plan_text(basis, language),
        })
    st.dataframe(rows, use_container_width=True, hide_index=True)

    for group in ("conflicts", "unknowns", "notes"):
        if plan[group]:
            with st.expander(plan_text(group, language), expanded=group != "notes"):
                for value in plan[group]:
                    st.markdown(markdown_text(value))
    if plan["alternatives"]:
        with st.expander(plan_text("alternatives", language)):
            for value in plan["alternatives"]:
                st.markdown(markdown_text(value["name"] + " · " + value["note"]))
                cited.update(value["source_ids"])
    with st.expander(plan_text("sources", language), expanded=True):
        for evidence_id, source in sources.items():
            if evidence_id in cited:
                st.link_button(source_title(source, language), source["url"])
                fetched = plan_text("fetched_at", language, time=source["fetched_at"][:16].replace("T", " "))
                st.caption(f"{plan_text('original_excerpt', language)} · {evidence_id} · {fetched}")
                st.markdown(markdown_text(source["excerpt"]))
        if evidence["source_errors"]:
            st.caption(plan_text("source_errors", language))
    report_contents = report(saved_plan, evidence["sources"], language, evidence["source_errors"])
    st.download_button(ui_text("download", language), report_contents.encode("utf-8"),
                       file_name=f"{run['run_id']}.{language}.md", mime="text/markdown")


def show_status(run, language):
    scheduler = run["scheduler"]
    service = run["model_service"]
    workflow = run["workflow"]
    phase = phase_text(workflow.get("phase"), language)
    number = scheduler.get("workflow_job_id") or service.get("service_job_id")
    step = scheduler.get("workflow_step_id")
    if number and step:
        number = f"{number}.{step}"
    st.subheader(status_text(run["status"], language))
    if phase:
        st.text(phase)
    message = progress_text(workflow.get("message"), language)
    if message and run["status"] not in TERMINAL:
        st.caption(message)
    if run.get("error"):
        error = str(run["error"])
        warning, detail = execution_error(error, language)
        st.warning(warning)
        if detail:
            with st.expander(ui_text("execution_details", language)):
                st.text(error)
    version = ui_text("version", language, version=run["request"]["version"])
    st.caption(f"{run['run_id']} · {version} · {run['attempt_id']}"
               + (f" · Slurm {number}" if number else ""))


@st.fragment(run_every="4s")
def track(path, language):
    try:
        run = request(path, language=language)
        if run["status"] in TERMINAL:
            st.rerun()
        show_status(run, language)
        if st.button(ui_text("cancel", language), key="cancel", disabled=run.get("cancel_requested", False)):
            request(path + "/cancel", {}, language=language)
            st.rerun()
    except ValueError as exc:
        st.error(str(exc))


def revise(run, language):
    with st.expander(ui_text("revise", language)):
        key = submit_key("revision_key")
        base = run["request"]["trip"]
        changed = form_fields(key, language, base)
        feedback = draft_input(st.text_area, key, language, "feedback", "", max_chars=500, value=None,
                               placeholder=ui_text("feedback_example", language))
        if st.button(ui_text("submit_revision", language), key="revise"):
            try:
                changes = {name: value for name, value in changed.items() if base[name] != value}
                child = request(run["status_url"] + "/revisions", {"changes": changes, "feedback": feedback or ""},
                                key, language=language)
                open_run(child["run_id"])
            except ValueError as exc:
                st.error(str(exc))


def set_language():
    st.query_params["lang"] = st.session_state["app_language"]


def main():
    selected_language = st.query_params.get("lang", ZH)
    language = selected_language if selected_language in LANGUAGES else ZH
    title = ui_text("title", language)
    st.set_page_config(page_title=title, page_icon="🗺️", layout="centered")
    st.title(title)
    if st.session_state.get("app_language") != language:
        st.session_state["app_language"] = language
    st.sidebar.radio("语言 / Language", list(LANGUAGES), format_func=LANGUAGES.get, key="app_language",
                     on_change=set_language, horizontal=True)
    try:
        runs = request("/api/runs", language=language)["runs"]
    except ValueError as exc:
        st.error(str(exc))
        return
    indexed = {run["run_id"]: run for run in runs}
    selected = indexed.get(st.query_params.get("run"))
    with st.sidebar:
        st.header(ui_text("history", language))
        if st.button(ui_text("new_trip", language), key="new", use_container_width=True):
            for name in ("run", "submit_key", "revision_key"):
                st.query_params.pop(name, None)
            st.rerun()
        if runs:
            labels = {run["run_id"]: " · ".join((run["request"]["trip"]["arrival"][:10],
                      ui_text("version", language, version=run["request"]["version"]),
                      status_text(run["status"], language))) for run in runs}
            wanted = st.selectbox(ui_text("find_run", language), list(indexed), index=None, format_func=labels.get,
                                  placeholder=ui_text("pick_run", language))
            if st.button(ui_text("open_run", language), key="open", disabled=not wanted, use_container_width=True):
                open_run(wanted)
    if not selected:
        new_trip(language)
        return
    if selected["status"] not in TERMINAL:
        track(selected["status_url"], language)
        return
    show_status(selected, language)
    if selected["status"] == "SUCCEEDED":
        try:
            display_plan(selected, language)
        except ValueError as exc:
            st.error(str(exc))
    elif st.button(ui_text("retry", language), key="retry"):
        try:
            request(selected["status_url"] + "/retry", {}, language=language)
            st.rerun()
        except ValueError as exc:
            st.error(str(exc))
    if not any(run["request"].get("parent_run_id") == selected["run_id"] for run in runs):
        revise(selected, language)


if __name__ == "__main__":
    main()
