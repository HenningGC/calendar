#!/usr/bin/env python3
"""Fetch one or more ICS feeds, merge them, expand recurring events and write a
static site into ./site:

    site/events.json   occurrences for the web view (UTC timestamps)
    site/calendar.ics  merged, expanded iCalendar feed others can subscribe to
    site/meta.json     generation time, sources, counts
    site/*             everything from ./web (the FullCalendar page)

Configuration is taken from the environment:

    ICS_URLS     required. One feed per line or comma separated. A feed may be
                 given as "Name=URL"; otherwise feeds are called "Calendar 1",
                 "Calendar 2", ... so the tenant / company behind a feed is
                 never exposed on the public site.
    PRIVACY      busy   -> only "Busy" / "Tentative" / "Out of office" blocks
                 titles -> event titles only (default)
                 full   -> titles, locations and descriptions
    PAST_DAYS    how far back to include occurrences (default 90)
    FUTURE_DAYS  how far ahead to include occurrences (default 365)
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import shutil
import sys
from pathlib import Path

import recurring_ical_events
import requests
from icalendar import Calendar, Event

ROOT = Path(__file__).resolve().parent.parent
WEB_DIR = ROOT / "web"
OUT_DIR = ROOT / "site"

PRIVACY = (os.environ.get("PRIVACY") or "titles").strip().lower()
PAST_DAYS = int(os.environ.get("PAST_DAYS") or 90)
FUTURE_DAYS = int(os.environ.get("FUTURE_DAYS") or 365)
HTTP_TIMEOUT = 60
MAX_DESCRIPTION = 2000

BUSY_TITLES = {
    "busy": "Busy",
    "tentative": "Tentative",
    "oof": "Out of office",
    "free": "Free",
}

# Feeds published with "availability only" permission carry the (localized) busy
# state as the event title. Normalize those to one English label per status.
AVAILABILITY_TITLES = {
    "busy", "tentative", "free", "away", "out of office", "working elsewhere",
    "ocupado", "provisional", "libre", "fuera de la oficina", "ausente",
    "beschäftigt", "mit vorbehalt", "frei", "abwesend", "außer haus",
    "zajęty", "wstępnie zaakceptowane", "wolny", "poza biurem",
}


def parse_sources(raw: str) -> list[tuple[str, str]]:
    sources: list[tuple[str, str]] = []
    for chunk in re.split(r"[\n,]+", raw):
        chunk = chunk.strip()
        if not chunk:
            continue
        if chunk.lower().startswith(("http://", "https://", "webcal://")):
            name, url = "", chunk
        else:
            name, _, url = chunk.partition("=")
            name, url = name.strip(), url.strip()
        if url.lower().startswith("webcal://"):
            url = "https://" + url[len("webcal://"):]
        sources.append((name or f"Calendar {len(sources) + 1}", url))
    if not sources:
        sys.exit("ICS_URLS is empty - nothing to merge")
    return sources


def fetch(url: str) -> Calendar:
    response = requests.get(
        url, timeout=HTTP_TIMEOUT, headers={"User-Agent": "calendar-merge/1.0"}
    )
    response.raise_for_status()
    return Calendar.from_ical(response.content)


def is_date_only(value) -> bool:
    return isinstance(value, dt.date) and not isinstance(value, dt.datetime)


def to_utc(value: dt.datetime) -> dt.datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.timezone.utc)
    return value.astimezone(dt.timezone.utc)


def busy_status(event: Event) -> str:
    raw = str(event.get("X-MICROSOFT-CDO-BUSYSTATUS", "")).strip().upper()
    if raw in ("FREE", "TENTATIVE", "OOF"):
        return raw.lower()
    if raw in ("BUSY", "WORKINGELSEWHERE"):
        return "busy"
    if str(event.get("TRANSP", "")).strip().upper() == "TRANSPARENT":
        return "free"
    return "busy"


def text(event: Event, key: str) -> str:
    value = event.get(key)
    return str(value).strip() if value is not None else ""


def occurrence_records(name: str, cal: Calendar, window_start, window_end) -> list[dict]:
    records = []
    for event in recurring_ical_events.of(cal).between(window_start, window_end):
        if text(event, "STATUS").upper() == "CANCELLED":
            continue
        start = event.decoded("DTSTART")
        if "DTEND" in event:
            end = event.decoded("DTEND")
        elif "DURATION" in event:
            end = start + event.decoded("DURATION")
        else:
            end = start + dt.timedelta(days=1) if is_date_only(start) else start

        all_day = is_date_only(start)
        if all_day:
            if not is_date_only(end):
                end = end.date()
            start_iso, end_iso = start.isoformat(), end.isoformat()
        else:
            if is_date_only(end):
                end = dt.datetime.combine(end, dt.time.min, tzinfo=start.tzinfo)
            start_utc, end_utc = to_utc(start), to_utc(end)
            if end_utc <= start_utc:
                end_utc = start_utc + dt.timedelta(minutes=30)
            start_iso = start_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
            end_iso = end_utc.strftime("%Y-%m-%dT%H:%M:%SZ")

        status = busy_status(event)
        title = text(event, "SUMMARY") or "(no title)"
        if title.casefold() in AVAILABILITY_TITLES:
            title = BUSY_TITLES[status]

        records.append(
            {
                "uid": text(event, "UID"),
                "title": title,
                "start": start_iso,
                "end": end_iso,
                "allDay": all_day,
                "status": status,
                "location": text(event, "LOCATION"),
                "description": text(event, "DESCRIPTION")[:MAX_DESCRIPTION],
                "sources": [name],
            }
        )
    return records


def dedupe(records: list[dict]) -> list[dict]:
    """Collapse the same meeting invited to several mailboxes into one entry."""
    merged: dict[tuple, dict] = {}
    for record in records:
        key = (record["title"].casefold(), record["start"], record["end"], record["allDay"])
        if key in merged:
            existing = merged[key]
            for source in record["sources"]:
                if source not in existing["sources"]:
                    existing["sources"].append(source)
            if not existing["location"]:
                existing["location"] = record["location"]
            if not existing["description"]:
                existing["description"] = record["description"]
        else:
            merged[key] = record
    return list(merged.values())


def apply_privacy(record: dict) -> dict:
    if PRIVACY == "busy":
        record["title"] = BUSY_TITLES[record["status"]]
        record["location"] = record["description"] = ""
    elif PRIVACY == "titles":
        record["location"] = record["description"] = ""
    elif PRIVACY != "full":
        sys.exit(f"Unknown PRIVACY value {PRIVACY!r} (use busy, titles or full)")
    return record


def finalize(record: dict) -> dict:
    digest = hashlib.sha1(
        f"{record['sources'][0]}|{record['uid']}|{record['start']}".encode()
    ).hexdigest()[:16]
    out = {
        "id": digest,
        "title": record["title"],
        "start": record["start"],
        "end": record["end"],
        "allDay": record["allDay"],
        "status": record["status"],
        "source": record["sources"][0],
        "sources": record["sources"],
    }
    if record["location"]:
        out["location"] = record["location"]
    if record["description"]:
        out["description"] = record["description"]
    return out


def write_ics(events: list[dict], generated_at: dt.datetime, path: Path) -> None:
    cal = Calendar()
    cal.add("prodid", "-//HenningGC//calendar-merge//EN")
    cal.add("version", "2.0")
    cal.add("calscale", "GREGORIAN")
    cal.add("method", "PUBLISH")
    cal.add("x-wr-calname", "Henning Gruhl - merged calendar")
    cal.add("x-published-ttl", "PT1H")
    for item in events:
        ev = Event()
        ev.add("uid", f"{item['id']}@henninggc.github.io")
        ev.add("dtstamp", generated_at)
        ev.add("summary", item["title"])
        if item["allDay"]:
            ev.add("dtstart", dt.date.fromisoformat(item["start"]))
            ev.add("dtend", dt.date.fromisoformat(item["end"]))
        else:
            ev.add("dtstart", dt.datetime.fromisoformat(item["start"]))
            ev.add("dtend", dt.datetime.fromisoformat(item["end"]))
        ev.add("categories", item["sources"])
        ev.add("transp", "TRANSPARENT" if item["status"] == "free" else "OPAQUE")
        ev.add("x-microsoft-cdo-busystatus", item["status"].upper())
        if item.get("location"):
            ev.add("location", item["location"])
        if item.get("description"):
            ev.add("description", item["description"])
        cal.add_component(ev)
    path.write_bytes(cal.to_ical())


def main() -> None:
    raw_urls = os.environ.get("ICS_URLS", "")
    if not raw_urls.strip():
        sys.exit("Set ICS_URLS (one feed per line, optionally Name=URL)")
    sources = parse_sources(raw_urls)

    generated_at = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    window_start = (generated_at - dt.timedelta(days=PAST_DAYS)).date()
    window_end = (generated_at + dt.timedelta(days=FUTURE_DAYS)).date()

    records: list[dict] = []
    per_source: list[dict] = []
    for name, url in sources:
        try:
            cal = fetch(url)
        except Exception as exc:  # fail the build so the previous deploy stays live
            sys.exit(f"Feed '{name}' could not be fetched: {type(exc).__name__}: {exc}")
        found = occurrence_records(name, cal, window_start, window_end)
        per_source.append({"name": name, "events": len(found)})
        records.extend(found)
        print(f"{name}: {len(found)} occurrences", file=sys.stderr)

    events = [finalize(apply_privacy(r)) for r in dedupe(records)]
    events.sort(key=lambda e: (e["start"], e["title"]))

    if OUT_DIR.exists():
        shutil.rmtree(OUT_DIR)
    shutil.copytree(WEB_DIR, OUT_DIR)
    (OUT_DIR / ".nojekyll").touch()
    (OUT_DIR / "events.json").write_text(json.dumps(events, ensure_ascii=False))
    (OUT_DIR / "meta.json").write_text(
        json.dumps(
            {
                "generatedAt": generated_at.isoformat().replace("+00:00", "Z"),
                "privacy": PRIVACY,
                "rangeStart": window_start.isoformat(),
                "rangeEnd": window_end.isoformat(),
                "sources": per_source,
                "events": len(events),
            },
            indent=2,
        )
    )
    write_ics(events, generated_at, OUT_DIR / "calendar.ics")
    print(f"wrote {len(events)} events to {OUT_DIR}", file=sys.stderr)


if __name__ == "__main__":
    main()
