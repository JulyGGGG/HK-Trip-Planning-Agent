"""Markdown export for the same itinerary shown by the web application."""

import re

if __package__:
    from .trip_presentation import (
        PLAN_STATUS, ZH, hkd_text, itinerary_view, localized, plan_text, quantity_text, source_title, unit_text,
    )
else:
    from trip_presentation import (
        PLAN_STATUS, ZH, hkd_text, itinerary_view, localized, plan_text, quantity_text, source_title, unit_text,
    )


def markdown_text(value):
    return re.sub(r"([\\`*_\[\]<>])", r"\\\1", value)


def report(plan, evidence, language=ZH, source_errors=()):
    view = itinerary_view(plan, language)
    lines = [f"# {plan_text('report_title', language)}", "", localized(PLAN_STATUS[view["status"]], language), ""]
    used = set()

    def citations(refs):
        used.update(refs)
        return "".join(f" [{ref}]" for ref in refs)

    for day in view["days"]:
        lines.extend(["## " + day["date"], ""])
        for entry in day["items"]:
            label = markdown_text(entry["label"])
            note = " · " + markdown_text(entry["note"]) if entry.get("note") else ""
            lines.append(f"- {entry['start'][11:16]}–{entry['end'][11:16]} "
                         f"{label}{note}{citations(entry['source_ids'])}")
        lines.append("")

    budget = view["budget"]
    nights = budget["nights"]
    stay = quantity_text(nights, unit_text("night", nights, language))
    lines.extend([
        f"## {plan_text('budget', language)} (HKD)", "",
        f"{plan_text('budget_limit', language)} {hkd_text(budget['limit_cents'], language)} · "
        f"{plan_text('budget_subtotal', language)} {hkd_text(budget['priced_subtotal_cents'], language)} · "
        f"{plan_text('lodging', language)} {stay}", "",
    ])
    for item in budget["items"]:
        value = hkd_text(item["total_cents"], language)
        quantity = quantity_text(item["quantity"], item["unit"])
        lines.append(f"- {markdown_text(item['label'])} · {quantity} · {value}{citations(item['source_ids'])}")

    for group in ("unknowns", "conflicts", "notes"):
        if view[group]:
            lines.extend(["", f"## {plan_text(group, language)}", ""])
            lines.extend("- " + markdown_text(item) for item in view[group])
    if view["alternatives"]:
        lines.extend(["", f"## {plan_text('alternatives', language)}", ""])
        for option in view["alternatives"]:
            lines.append(f"- {markdown_text(option['name'])} · {markdown_text(option['note'])}"
                         f"{citations(option['source_ids'])}")

    lines.extend(["", f"## {plan_text('sources', language)}", ""])
    if source_errors:
        lines.extend([plan_text("source_errors", language), ""])
    for item in evidence:
        if item["id"] in used:
            title = markdown_text(source_title(item, language))
            lines.append(f"- [{item['id']} · {title}]({item['url']}) · {item['fetched_at']}")
            lines.append(f"  > {plan_text('original_excerpt', language)}")
            lines.append("  >")
            lines.extend(f"  > {line}" for line in item["excerpt"].splitlines())
    return "\n".join(lines) + "\n"
