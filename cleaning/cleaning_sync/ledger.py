"""Read-only view of the earnings staging ledger ("Airbnb CSV Staging" in the Master Financial Tracker).

Why: the Airbnb earnings Upcoming report is a second, independent witness for every reservation. The planner uses
it to (a) mark new tasks "calendar+ledger", (b) NOT treat a booking that is only missing from the calendar feed as
cancelled while Airbnb earnings still lists it, and (c) say "cancellation confirmed by both" when the calendar has
dropped a booking AND the importer flagged it as no longer in the latest Upcoming report.

Safety:
  * Only spreadsheets.values.batchGet is ever called. The reader holds no write method; the test fake exposes nothing else.
  * Header cells are checked before any data is trusted. A moved or renamed column raises LedgerUnavailable.
  * If the newest import is older than MAX_AGE_HOURS, or the read fails, the view is "unusable" and the planner falls
    back to calendar-only behaviour (the info op LEDGER_UNAVAILABLE says why). It never fails the poll.
  * Staging is never edited by hand and is never edited here.

Reading rules (from the importer's own states, column AD "Ledger state"):
  Upcoming-source row, state NOT_IN_LATEST_UPCOMING or SUPERSEDED_UPCOMING_REVISION -> not live
  Upcoming-source row, any other state (VERIFIED, REVIEW_PAID_UPCOMING_OVERLAP, ...)   -> live
  any Paid-source Reservation row with the code                                      -> paid
  gone = has Upcoming rows, ALL of them explicitly not live, and no Paid row
"""
from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from typing import Optional

from .planner import parse_date

TAB = "Airbnb CSV Staging"
MAX_AGE_HOURS = 72
NOT_LIVE_STATES = {"NOT_IN_LATEST_UPCOMING", "SUPERSEDED_UPCOMING_REVISION"}

# (column index, header must start with)  A=0 ... AD=29
EXPECTED = {0: "source", 4: "imported at", 7: "type", 8: "confirmation code", 10: "start date",
            11: "end date", 15: "physical apartment", 29: "ledger state"}
# one batchGet: header row + the columns we need, in this order
RANGES = ["A1:AO1", "A2:A", "E2:E", "H2:H", "I2:I", "K2:L", "P2:P", "AD2:AD"]


class LedgerUnavailable(Exception):
    pass


@dataclass
class LedgerEntry:
    code: str
    apt: str = ""
    live: bool = False
    paid: bool = False
    gone: bool = False
    end: Optional[dt.date] = None      # checkout of the live rows when they all agree
    start: Optional[dt.date] = None


@dataclass
class LedgerView:
    entries: dict = field(default_factory=dict)
    latest_import: Optional[dt.datetime] = None
    unusable_reason: Optional[str] = None

    @property
    def usable(self) -> bool:
        return self.unusable_reason is None

    def get(self, code: Optional[str]) -> Optional[LedgerEntry]:
        return self.entries.get(code) if code else None

    def codes(self) -> set:
        return set(self.entries)

    @classmethod
    def unavailable(cls, reason: str) -> "LedgerView":
        return cls(unusable_reason=reason)


# ----------------------------------------------------------------------------- parsing
def _s(v) -> str:
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v).strip()


def parse_ts(v) -> Optional[dt.datetime]:
    """'2026-11-09 10:25:45', '2026-10-30T17:49:40.107Z', '2026-10-30T19:46:31+07:00' or a Sheets serial number
    -> naive UTC datetime. Values with an offset are converted to UTC so mixed formats can be compared."""
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return dt.datetime(1899, 12, 30) + dt.timedelta(days=float(v))
    s = _s(v)
    if not s:
        return None
    s = re.sub(r"\.\d+", "", s.replace("T", " ")).strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    s = re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", s) if re.search(r"\d{2}:\d{2}:\d{2}[+-]\d{4}$", s) else s
    try:
        d = dt.datetime.fromisoformat(s)
    except ValueError:
        return None
    if d.tzinfo is not None:
        d = d.astimezone(dt.timezone.utc).replace(tzinfo=None)
    return d


def _date(v) -> Optional[dt.date]:
    try:
        return parse_date(v)
    except (ValueError, TypeError):
        return None


def build_view(header, cols, now: Optional[dt.datetime] = None, max_age_hours: int = MAX_AGE_HOURS) -> LedgerView:
    """Pure function. `cols` = dict with lists aligned by row: source, imported, type, code, start, end, apt, state."""
    for idx, want in EXPECTED.items():
        got = _s(header[idx]).lower() if idx < len(header) else ""
        if not got.startswith(want):
            raise LedgerUnavailable(f"staging header column {idx} is {got!r}, expected {want!r}: layout changed")
    n = max(len(v) for v in cols.values()) if cols else 0
    pad = lambda k: list(cols[k]) + [""] * (n - len(cols[k]))
    src, imp, typ, code, start, end, apt, state = (pad(k) for k in
                                                    ("source", "imported", "type", "code", "start", "end", "apt", "state"))
    latest, groups = None, {}
    for i in range(n):
        source = _s(src[i])
        if source not in ("Paid", "Upcoming"):
            continue
        ts = parse_ts(imp[i])
        if ts and (latest is None or ts > latest):
            latest = ts
        c = _s(code[i])
        if not c or _s(typ[i]) != "Reservation":
            continue
        groups.setdefault(c, []).append((source, _s(state[i]), _date(start[i]), _date(end[i]), _s(apt[i])))

    entries = {}
    for c, rows in groups.items():
        up = [r for r in rows if r[0] == "Upcoming"]
        live_rows = [r for r in up if r[1] not in NOT_LIVE_STATES]
        paid = any(r[0] == "Paid" for r in rows)
        ends = {r[3] for r in live_rows if r[3]}
        starts = {r[2] for r in live_rows if r[2]}
        apts = [r[4] for r in reversed(rows) if r[4]]
        entries[c] = LedgerEntry(
            code=c, apt=apts[0] if apts else "", live=bool(live_rows), paid=paid,
            gone=bool(up) and not live_rows and not paid,
            end=next(iter(ends)) if len(ends) == 1 else None,
            start=next(iter(starts)) if len(starts) == 1 else None)
    view = LedgerView(entries=entries, latest_import=latest)
    if latest is None:
        view.unusable_reason = "staging ledger has no imports"
    elif now is not None and now - latest > dt.timedelta(hours=max_age_hours):
        view.unusable_reason = f"newest earnings import is {latest:%Y-%m-%d %H:%M}, older than {max_age_hours}h"
    return view


# ----------------------------------------------------------------------------- reader
class LedgerReader:
    """Reads the staging ledger through a Sheets v4 `service`. Only batchGet is used."""

    def __init__(self, spreadsheet_id: str, service, tab: str = TAB):
        self.sid, self.svc, self.tab = spreadsheet_id, service, tab

    def read(self, now: Optional[dt.datetime] = None) -> LedgerView:
        try:
            res = self.svc.spreadsheets().values().batchGet(
                spreadsheetId=self.sid, ranges=[f"'{self.tab}'!{r}" for r in RANGES],
                valueRenderOption="UNFORMATTED_VALUE", dateTimeRenderOption="SERIAL_NUMBER").execute()
            vr = res["valueRanges"]
            header = (vr[0].get("values") or [[]])[0]
            one = lambda i: [(r[0] if r else "") for r in vr[i].get("values", [])]
            two = lambda i, j: [(r[j] if len(r) > j else "") for r in vr[i].get("values", [])]
            cols = {"source": one(1), "imported": one(2), "type": one(3), "code": one(4),
                    "start": two(5, 0), "end": two(5, 1), "apt": one(6), "state": one(7)}
            return build_view(header, cols, now=now)
        except LedgerUnavailable as e:
            return LedgerView.unavailable(str(e))
        except Exception as e:  # noqa: BLE001 - never fail the poll because the ledger could not be read
            return LedgerView.unavailable(f"ledger read failed: {type(e).__name__}: {str(e)[:100]}")
