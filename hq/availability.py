"""Turn feed results into per-listing night status and per-apartment gaps.

Night status per listing: "<CODE>" (reserved), "BLOCKED" (Airbnb 'Not available',
often the sibling listing's booking), "OPEN", or "UNKNOWN" (feed failed).
An apartment night is OPEN only if every listing of that apartment is OPEN.
If any listing of the apartment is UNKNOWN, the apartment night is UNKNOWN.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta

from .feeds import FeedResult

OPEN, BLOCKED, UNKNOWN = "OPEN", "BLOCKED", "UNKNOWN"


def night_status(result: FeedResult, nights: list[date]) -> dict[date, str]:
    if not result.ok:
        return {d: UNKNOWN for d in nights}
    status = {d: OPEN for d in nights}
    # reserved wins over blocked when both cover a night
    for ev in sorted(result.events, key=lambda e: e.kind != "blocked"):
        d = ev.start
        while d < ev.end:
            if d in status:
                status[d] = (ev.code or "RESERVED") if ev.kind == "reserved" else BLOCKED
            d += timedelta(days=1)
    return status


def apartment_status(per_listing: list[dict[date, str]], nights: list[date]) -> dict[date, str]:
    out: dict[date, str] = {}
    for d in nights:
        vals = [s[d] for s in per_listing]
        if any(v == UNKNOWN for v in vals):
            out[d] = UNKNOWN
        elif all(v == OPEN for v in vals):
            out[d] = OPEN
        else:
            codes = [v for v in vals if v not in (OPEN, BLOCKED)]
            out[d] = codes[0] if codes else BLOCKED
    return out


@dataclass
class Gap:
    apartment: str
    start: date          # first open night
    end: date            # day after last open night
    nights: int
    bounded_after: bool  # a booking/block follows inside the horizon
    label: str


def find_gaps(apartment: str, status: dict[date, str], nights: list[date]) -> list[Gap]:
    gaps: list[Gap] = []
    i = 0
    while i < len(nights):
        if status[nights[i]] != OPEN:
            i += 1
            continue
        j = i
        while j < len(nights) and status[nights[j]] == OPEN:
            j += 1
        n = j - i
        bounded = j < len(nights) and status[nights[j]] != UNKNOWN
        if not bounded:
            label = f"open to end of horizon ({n}+ nights)"
        elif n == 1:
            label = "ORPHAN 1 night"
        elif n <= 3:
            label = f"short gap {n} nights"
        else:
            label = f"{n} nights"
        gaps.append(Gap(apartment, nights[i], nights[i] + timedelta(days=n), n, bounded, label))
        i = j
    return gaps


def build(results: list[FeedResult], today: date, horizon_days: int = 60):
    nights = [today + timedelta(days=k) for k in range(horizon_days)]
    listing_rows = []
    by_apartment: dict[str, list[dict[date, str]]] = defaultdict(list)
    for r in results:
        st = night_status(r, nights)
        listing_rows.append((r, st))
        if r.listing.config != "unknown":
            by_apartment[r.listing.apartment].append(st)
    apt_status = {a: apartment_status(s, nights) for a, s in by_apartment.items()}
    gaps = [g for a, st in apt_status.items() for g in find_gaps(a, st, nights)]
    return nights, listing_rows, apt_status, gaps
