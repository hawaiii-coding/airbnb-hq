"""Minimal ICS reader for Airbnb feeds. Pure parsing: no network, no tokens."""
from __future__ import annotations
import datetime as dt
import re
from .planner import Booking, CODE_RE


def _unfold(text: str):
    lines = []
    for raw in text.replace("\r\n", "\n").split("\n"):
        if raw[:1] in (" ", "\t") and lines:
            lines[-1] += raw[1:]
        else:
            lines.append(raw)
    return lines


def _d(v: str) -> dt.date:
    v = v.strip()
    return dt.date(int(v[0:4]), int(v[4:6]), int(v[6:8]))


def parse_ics(text: str, apt: str, config: str | None = None, feed: str = "") -> list[Booking]:
    out, ev = [], None
    for line in _unfold(text):
        if line == "BEGIN:VEVENT":
            ev = {}
        elif line == "END:VEVENT" and ev is not None:
            summary = ev.get("SUMMARY", "")
            start, end = ev.get("DTSTART"), ev.get("DTEND")
            if start and end:
                m = CODE_RE.search(ev.get("DESCRIPTION", "").replace("\\n", " "))
                if summary.startswith("Reserved"):
                    out.append(Booking(m.group(0) if m else None, apt, _d(start), _d(end), "RESERVED", feed, config))
                elif "Not available" in summary:
                    out.append(Booking(None, apt, _d(start), _d(end), "BLOCK", feed, config))
            ev = None
        elif ev is not None and ":" in line:
            name, _, val = line.partition(":")
            ev[name.split(";")[0]] = val
    return out
