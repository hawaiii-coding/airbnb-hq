"""Fetch and parse Airbnb iCal feeds.

Feed URLs carry private tokens. They are read from env vars ICAL_URL_<key>
(populated from Secret Manager on Cloud Run) and are never logged.
A feed that fails to fetch or is not an iCalendar is reported as FAILED, so a
broken poll can never look like free (or cancelled) nights.
"""
from __future__ import annotations

import json
import os
import re
import urllib.request
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

CODE_RE = re.compile(r"\b(HM[A-Z0-9]{8})\b")
CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "listings.json"


@dataclass(frozen=True)
class Listing:
    key: str
    listing_id: str
    apartment: str
    config: str


@dataclass
class Event:
    start: date          # first night
    end: date            # checkout day (iCal DTEND is exclusive)
    kind: str            # "reserved" or "blocked"
    code: str | None


@dataclass
class FeedResult:
    listing: Listing
    ok: bool
    events: list[Event] = field(default_factory=list)
    error: str | None = None


def load_listings(path: Path = CONFIG_PATH) -> list[Listing]:
    data = json.loads(path.read_text())
    return [Listing(**{k: d[k] for k in ("key", "listing_id", "apartment", "config")})
            for d in data["listings"]]


def _unfold(text: str) -> list[str]:
    lines: list[str] = []
    for raw in text.replace("\r\n", "\n").split("\n"):
        if raw.startswith((" ", "\t")) and lines:
            lines[-1] += raw[1:]
        else:
            lines.append(raw)
    return lines


def _parse_date(value: str) -> date:
    value = value.strip()[:8]
    return datetime.strptime(value, "%Y%m%d").date()


def parse_ics(text: str) -> list[Event]:
    if "BEGIN:VCALENDAR" not in text[:200]:
        raise ValueError("not an iCalendar")
    events: list[Event] = []
    cur: dict[str, str] | None = None
    for line in _unfold(text):
        if line == "BEGIN:VEVENT":
            cur = {}
        elif line == "END:VEVENT" and cur is not None:
            if "DTSTART" in cur and "DTEND" in cur:
                summary = cur.get("SUMMARY", "")
                desc = cur.get("DESCRIPTION", "").replace("\\n", "\n")
                m = CODE_RE.search(desc) or CODE_RE.search(summary)
                kind = "reserved" if summary.strip().lower().startswith("reserved") else "blocked"
                events.append(Event(_parse_date(cur["DTSTART"]), _parse_date(cur["DTEND"]),
                                    kind, m.group(1) if m else None))
            cur = None
        elif cur is not None and ":" in line:
            name, value = line.split(":", 1)
            cur[name.split(";", 1)[0].upper()] = value
    return events


def fetch_feed(listing: Listing, timeout: int = 30) -> FeedResult:
    url = os.environ.get(f"ICAL_URL_{listing.key}")
    if not url:
        return FeedResult(listing, False, error="no feed URL configured")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "airbnb-hq/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            text = resp.read().decode("utf-8", errors="replace")
        return FeedResult(listing, True, parse_ics(text))
    except Exception as exc:  # never include the URL in the message
        return FeedResult(listing, False, error=type(exc).__name__)
