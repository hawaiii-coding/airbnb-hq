"""Read-only planner: bookings (iCal) + current tasks -> a reviewable plan.

Design rules (owner-approved 2026-11-09, items 1/3/4):
  * The planner never writes anything. It returns operations in three tiers:
      auto_safe   - bookkeeping a writer may apply without asking (ADD new task rows,
                    code crosswalk, verification marks)
      needs_owner - anything that would change an owner-owned column or an existing
                    row's meaning (Cleaner, Fee, Status, Confirmed at, Paid at, Date)
      info        - nothing to apply, just something to look at
  * A booking missing from a feed NEVER cancels a task by itself. It is watched for
    `grace_polls` consecutive polls and then becomes a needs_owner PROPOSE_CANCEL.
  * New tasks from a feed are marked calendar_only unless the code is also in the
    earnings ledger.
  * Quick-added tasks (owner types a booking before the feed catches up) are marked
    unverified and are reconciled on the confirmation code.
"""
from __future__ import annotations

import datetime as dt
import json
import re
from dataclasses import dataclass, field
from typing import Iterable, Optional

CODE_RE = re.compile(r"\bHM[A-Z0-9]{8}\b")
ACTIVE = {"PLANNED", "CONFIRMED"}
QUICK_MARK = "QUICK-ADD unverified"
AUTO_CANCEL_POLLS = 2
OWNER_COLUMNS = ("Cleaner", "Fee VND", "Status", "Confirmed at", "Paid at")


# ----------------------------------------------------------------------------- dates
def parse_date(v) -> dt.date:
    if isinstance(v, dt.datetime):
        return v.date()
    if isinstance(v, dt.date):
        return v
    s = str(v).strip()
    if re.fullmatch(r"\d{5}(\.0+)?", s):  # Sheets serial number
        return dt.date(1899, 12, 30) + dt.timedelta(days=int(float(s)))
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        return dt.date.fromisoformat(s)
    m = re.fullmatch(r"(\d{1,2})/(\d{1,2})/(\d{4})", s)
    if m:
        return dt.date(int(m[3]), int(m[1]), int(m[2]))
    raise ValueError(f"unparseable date: {v!r}")


# ----------------------------------------------------------------------------- models
@dataclass
class Booking:
    code: Optional[str]
    apt: str
    start: Optional[dt.date]
    end: dt.date  # checkout day (iCal DTEND is exclusive = checkout)
    kind: str  # RESERVED | BLOCK
    feed: str = ""
    config: Optional[str] = None


@dataclass
class Task:
    key: str
    date: dt.date
    apt: str
    cleaner: str
    config: str
    fee: Optional[int]
    status: str
    source_id: str = ""
    notes: str = ""
    created: Optional[dt.datetime] = None
    clean_kind: str = ""  # sheet column "Kind" (CHECKOUT, STAY-OVER, MAINTENANCE, ...)
    display: str = ""

    @property
    def code(self) -> Optional[str]:
        for s in (self.key, self.source_id):
            m = CODE_RE.search(s or "")
            if m:
                return m.group(0)
        return None

    @property
    def kind(self) -> str:
        if self.key.startswith("MANUAL|"):
            return "MANUAL"
        if self.key.startswith("DIRECT|"):
            return "DIRECT"
        return "CODE" if self.code else "LEGACY"

    @property
    def unverified(self) -> bool:
        return QUICK_MARK in (self.notes or "")


class RateCard:
    def __init__(self, data: dict):
        self.apts = data["apartments"]
        self.suffix = data.get("config_suffix", {})

    @classmethod
    def load(cls, path: str) -> "RateCard":
        with open(path) as f:
            return cls(json.load(f))

    def known(self, apt: str) -> bool:
        return apt in self.apts

    def cleaner(self, apt: str) -> Optional[str]:
        return self.apts[apt]["cleaner"] if apt in self.apts else None

    def config_for(self, apt: str, config: Optional[str]) -> Optional[str]:
        """Known config, explicit alias, or the default when none is given. An unknown config label
        returns None (fee missing) instead of silently inheriting the default fee."""
        a = self.apts.get(apt)
        if not a:
            return None
        if not config:
            return a["default_config"]
        if config in a["fees"]:
            return config
        return a.get("aliases", {}).get(config)

    def fee(self, apt: str, config: Optional[str]) -> Optional[int]:
        a = self.apts.get(apt)
        cfg = self.config_for(apt, config)
        if not a or cfg is None:
            return None
        return a["fees"].get(cfg)

    def display(self, apt: str, config: Optional[str]) -> str:
        """Sheet/Zalo name, e.g. 'Q7-B1.17.09 (1PN)'. Suffix only for units with more than one bookable config."""
        a = self.apts.get(apt)
        if not a:
            return apt
        base = a.get("display", apt)
        cfg = self.config_for(apt, config)
        if len(a["fees"]) > 1 and cfg:
            return f"{base} ({self.suffix.get(cfg, cfg)})"
        return base

    def canonical_label(self, apt: str, label: Optional[str]) -> Optional[str]:
        """Rate-card label for an alias (B9.28 "1BR" -> "Studio"); None when the label is already canonical/unknown."""
        a = self.apts.get(apt)
        if not a or not label or label in a["fees"]:
            return None
        return a.get("aliases", {}).get(label)


def iso(v) -> str:
    """Any date-ish cell (ISO text, Sheets serial, m/d/yyyy) -> YYYY-MM-DD; unparseable values pass through."""
    try:
        return parse_date(v).isoformat()
    except (ValueError, TypeError):
        return str(v)


# ----------------------------------------------------------------------------- ops
def _op(op, tier, t_or_b, detail, **extra):
    d = {"op": op, "tier": tier, "detail": detail}
    d["key"] = getattr(t_or_b, "key", None)
    d["apt"] = t_or_b.apt
    d["date"] = (t_or_b.date if hasattr(t_or_b, "date") else t_or_b.end).isoformat()
    d.update(extra)
    return d


def _rate_flags(t: Task, rc: RateCard):
    ops = []
    want = rc.cleaner(t.apt)
    if want and t.cleaner != want:
        ops.append(_op("CLEANER_MISMATCH", "needs_owner", t, f"row {t.cleaner}, apartment owner is {want}"))
    fee = rc.fee(t.apt, t.config)
    if fee is not None and t.fee is not None and (t.fee <= 0 or t.fee % fee != 0):
        ops.append(_op("FEE_MISMATCH", "needs_owner", t,
                       f"row {t.fee:,} VND ({t.config}); rate card {fee:,} per shift "
                       f"({rc.config_for(t.apt, t.config)})", proposed_fee=fee))
    return ops


# ----------------------------------------------------------------------------- plan
def plan(
    tasks: Iterable[Task],
    bookings: Iterable[Booking],
    rc: RateCard,
    today: dt.date,
    horizon_end: dt.date,
    ledger_codes: Iterable[str] = (),
    miss_counts: Optional[dict] = None,
    grace_polls: int = 2,
    covered_apts: Optional[set] = None,
    now: Optional[dt.datetime] = None,
    ledger=None,
):
    """`ledger` is an optional ledger.LedgerView (read-only view of the earnings staging ledger). When it is missing or
    unusable the planner behaves exactly as before (calendar only)."""
    tasks, bookings = list(tasks), list(bookings)
    ledger_codes = set(ledger_codes)
    miss_counts = dict(miss_counts or {})
    now = now or dt.datetime.combine(today, dt.time(12, 0))
    covered = covered_apts if covered_apts is not None else {b.apt for b in bookings}
    in_win = lambda d: today <= d <= horizon_end
    ops, new_miss = [], {}
    led = ledger if (ledger is not None and ledger.usable) else None
    if ledger is not None and led is None:
        ops.append({"op": "LEDGER_UNAVAILABLE", "tier": "info", "key": None, "apt": "-", "date": today.isoformat(),
                    "detail": f"earnings ledger not used this run: {ledger.unusable_reason}; calendar-only rules apply"})
    if led:
        ledger_codes |= led.codes()

    reserved = sorted((b for b in bookings if b.kind == "RESERVED"), key=lambda b: (b.end, b.apt))
    blocks = [b for b in bookings if b.kind == "BLOCK"]
    for b in reserved:
        if not b.code:
            ops.append(_op("RESERVED_NO_CODE", "info", b, "Reserved event without a confirmation code"))
    reserved = [b for b in reserved if b.code]

    active = [t for t in tasks if t.status != "CANCELLED"]
    by_code: dict[str, list[Task]] = {}
    for t in active:
        if t.code:
            by_code.setdefault(t.code, []).append(t)
    cancelled_codes = {t.code for t in tasks if t.status == "CANCELLED" and t.code}
    matched: set[str] = set()
    unmatched_bookings: list[Booking] = []

    # pass A: code matches and exact legacy matches
    for b in reserved:
        if not rc.known(b.apt):
            ops.append(_op("UNKNOWN_APARTMENT", "info", b, f"{b.code}: apartment not in rate card"))
            continue
        if b.code in by_code:
            for t in by_code[b.code]:
                matched.add(t.key)
                if t.status in ACTIVE and t.date != b.end:
                    ops.append(_op("MOVE_DATE", "needs_owner", t,
                                   f"{b.code}: task {t.date} but feed checkout is {b.end}", new_date=b.end.isoformat()))
                if t.unverified:
                    ops.append(_op("VERIFY", "auto_safe", t, f"{b.code} now present in feed; clear unverified mark"))
            continue
        legacy = [t for t in active if t.kind == "LEGACY" and t.key not in matched and t.apt == b.apt and t.date == b.end]
        if legacy:
            t = legacy[0]
            matched.add(t.key)
            ops.append(_op("ADOPT_CODE", "auto_safe", t, f"crosswalk legacy row to {b.code}", code=b.code))
            continue
        unmatched_bookings.append(b)

    # pass B: legacy row within +-2 days, only if unambiguous
    # one-to-one by smallest distance; a task with two equally near bookings is left alone (ambiguous)
    pairs = []
    for b in unmatched_bookings:
        for t in active:
            if (t.kind == "LEGACY" and t.key not in matched and t.apt == b.apt and t.status in ACTIVE
                    and abs((t.date - b.end).days) <= 2):
                pairs.append((abs((t.date - b.end).days), b.end, id(b), b, t))
    pairs.sort(key=lambda p: p[:3])
    taken_b: set[int] = set()
    for dist, _, _, b, t in pairs:
        if id(b) in taken_b or t.key in matched:
            continue
        ties = [p for p in pairs if p[4] is t and p[0] == dist and id(p[3]) not in taken_b]
        if len(ties) > 1:
            continue
        matched.add(t.key)
        taken_b.add(id(b))
        ops.append(_op("MOVE_DATE", "needs_owner", t,
                       f"{b.code}: legacy task {t.date}, feed checkout {b.end}; also crosswalk code",
                       new_date=b.end.isoformat(), code=b.code))
    still = [b for b in unmatched_bookings if id(b) not in taken_b]

    # ADD for bookings in the window that still have no task
    for b in still:
        if not in_win(b.end):
            continue
        cfg = rc.config_for(b.apt, b.config)
        fee = rc.fee(b.apt, b.config)
        verification = "calendar+ledger" if b.code in ledger_codes else "calendar_only"
        arrivals = [o.code for o in reserved if o.apt == b.apt and o.start == b.end and o.code != b.code]
        row = {"key": f"AUTO|{b.apt}|{b.code}", "date": b.end.isoformat(), "apt": b.apt,
               "cleaner": rc.cleaner(b.apt), "config": cfg, "display": rc.display(b.apt, cfg), "fee": fee, "status": "PLANNED",
               "source_id": f"AIRBNB|{b.code}", "verification": verification,
               "same_day": f"YES - {arrivals[0]} check-in" if arrivals else ""}
        ops.append({"op": "ADD", "tier": "auto_safe", "key": row["key"], "apt": b.apt, "date": row["date"],
                    "detail": f"{b.code} checkout {b.end}: no task ({verification})", "row": row})
        if fee is None:
            ops.append({"op": "FEE_MISSING", "tier": "needs_owner", "key": row["key"], "apt": b.apt,
                        "date": row["date"], "detail": f"no fee in rate card for {b.apt} / {cfg}"})
        if b.code in cancelled_codes:
            ops.append({"op": "REVIVED_CODE", "tier": "needs_owner", "key": row["key"], "apt": b.apt,
                        "date": row["date"], "detail": f"{b.code} has a CANCELLED task but is back in the feed"})

    # rate-card checks and past-planned on every active task
    for t in active:
        if t.status in ACTIVE and t.date >= today - dt.timedelta(days=0):
            ops.extend(_rate_flags(t, rc))
        if t.status == "PLANNED" and t.date < today:
            ops.append(_op("PAST_PLANNED", "needs_owner", t, "date passed, still PLANNED: confirm completed or cancel"))

    # data tidy: alias labels in Type -> rate-card label (Type is not an owner column; fee is unchanged)
    for t in active:
        if t.status in ACTIVE:
            canon = rc.canonical_label(t.apt, t.config)
            if canon and canon != t.config:
                ops.append(_op("TIDY_TYPE", "auto_safe", t, f"Type {t.config!r} -> rate-card label {canon!r}",
                               new_type=canon))

    # data tidy: sync-written rows that carry the bare apartment name in Display -> sheet/Zalo display name.
    # Only rows whose Display is empty or equals the apartment name: owner-typed displays are never touched.
    for t in active:
        if t.status in ACTIVE and t.date >= today and t.display in ("", t.apt):
            want = rc.display(t.apt, rc.canonical_label(t.apt, t.config) or t.config)
            if want and want != t.display:
                ops.append(_op("TIDY_DISPLAY", "auto_safe", t, f"Display {t.display!r} -> {want!r}", new_display=want))

    # duplicate same-date cleans (same apartment, date and kind), report only
    seen: dict = {}
    for t in active:
        if t.date >= today:
            seen.setdefault((t.apt, t.date, (t.clean_kind or "CHECKOUT").upper()), []).append(t)
    for (apt_, d_, k_), ts in seen.items():
        if len(ts) > 1:
            for t in ts:
                ops.append(_op("DUPLICATE_SAME_DATE", "info", t,
                               f"{len(ts)} active {k_} tasks on {d_}: " + ", ".join(x.key for x in ts)))

    # tasks with no booking: watch; cancel without the owner only when feed AND earnings agree (AUTO_CANCEL)
    for t in active:
        if t.key in matched or t.status not in ACTIVE or t.kind not in ("CODE", "LEGACY"):
            continue
        if not (in_win(t.date) and t.apt in covered):
            continue
        if t.unverified and t.created and now - t.created < dt.timedelta(hours=24):
            ops.append(_op("QUICK_ADD_PENDING", "info", t, "quick-add awaiting its feed entry (<24h old)"))
            continue
        e = led.get(t.code) if led else None
        if e and (e.live or e.paid):
            # Airbnb earnings still lists it: this is feed lag, not a cancellation. Miss count is not advanced.
            ops.append(_op("LEDGER_ACTIVE", "info", t,
                           f"{t.code} not in the feed but Airbnb earnings still lists it "
                           f"({'upcoming' if e.live else 'paid'}); treated as lag, no change"))
            continue
        n = miss_counts.get(t.key, 0) + 1
        new_miss[t.key] = n
        confirmed = bool(e and e.gone)
        need = min(2, grace_polls) if confirmed else grace_polls
        if confirmed and n >= AUTO_CANCEL_POLLS and t.status == "PLANNED" and t.kind == "CODE":
            # handoff item 1 / BUSINESS_RULES 4: both independent sources agree -> safe to cancel without asking
            ops.append(_op("AUTO_CANCEL", "auto_safe", t,
                           f"{t.code} missing from the Airbnb feed for {n} consecutive polls AND no longer in the "
                           f"Upcoming earnings report", polls=n))
        elif n >= need:
            how = ("calendar and Airbnb earnings agree it is gone (no longer in the latest Upcoming report)"
                   if confirmed else "absent from feed")
            ops.append(_op("PROPOSE_CANCEL", "needs_owner", t,
                           f"{how} for {n} consecutive polls; owner must confirm before cancelling",
                           ledger="confirmed_gone" if confirmed else "no_signal"))
        else:
            ops.append(_op("WATCH_MISSING", "info", t, f"not in feed (miss {n}/{grace_polls}); no change made"))

    # ledger cross-checks: information only, never a write
    if led:
        move_keys = {o["key"] for o in ops if o["op"] == "MOVE_DATE"}
        any_task_codes = {t.code for t in tasks if t.code}
        feed_codes = {b.code for b in reserved}
        for t in active:
            e = led.get(t.code)
            if (e and e.live and e.end and t.status in ACTIVE and t.date >= today and e.end != t.date
                    and t.key not in move_keys and t.kind in ("CODE", "LEGACY") and t.apt in covered):
                ops.append(_op("LEDGER_DATE_DIFF", "info", t,
                               f"{t.code}: task {t.date}, Airbnb earnings shows checkout {e.end}"))
        for t in tasks:
            e = led.get(t.code)
            if t.status == "CANCELLED" and e and e.live and t.date >= today:
                ops.append(_op("LEDGER_LIVE_BUT_CANCELLED", "info", t,
                               f"{t.code}: task is CANCELLED but Airbnb earnings still lists it as upcoming; check it"))
        for c, e in sorted(led.entries.items()):
            if (e.live and e.end and in_win(e.end) and c not in any_task_codes and c not in feed_codes
                    and rc.known(e.apt) and e.apt in covered):
                ops.append({"op": "LEDGER_ONLY_BOOKING", "tier": "info", "key": None, "apt": e.apt,
                            "date": e.end.isoformat(),
                            "detail": f"{c}: Airbnb earnings lists checkout {e.end}, no task and not in the feed "
                                      f"(calendar lag, or cancelled and not yet dropped from the report)"})

    # blocks: Trip.com / direct / closed. Never auto-create; list or verify quick-adds
    for blk in blocks:
        if not (in_win(blk.end) and blk.start and (blk.end - blk.start).days <= 30):
            continue
        nights = [blk.start + dt.timedelta(days=i) for i in range((blk.end - blk.start).days)]
        if all(any(r.apt == blk.apt and r.start and r.start <= n < r.end for r in reserved) for n in nights):
            continue  # cross-listing shadow of one or more real reservations (union of ranges)
        same_day = [t for t in active if t.apt == blk.apt and t.date == blk.end]
        if same_day:
            for t in same_day:
                if t.unverified and t.kind in ("MANUAL", "DIRECT"):
                    ops.append(_op("VERIFY", "auto_safe", t, "block ends on this date; clear unverified mark"))
            continue
        ops.append({"op": "UNCLASSIFIED_BLOCK", "tier": "info", "key": None, "apt": blk.apt,
                    "date": blk.end.isoformat(),
                    "detail": f"block {blk.start} to {blk.end} has no task: Trip.com/direct checkout? use quick-add"})

    # quick-add tasks that are older than 24h and still not in the feed are caught by the loop above.
    order = {"needs_owner": 0, "auto_safe": 1, "info": 2}
    ops.sort(key=lambda o: (order[o["tier"]], o["date"], o["op"]))
    return {"ops": ops, "miss_counts": new_miss}


# ----------------------------------------------------------------------------- mismatch report
REPORT_CATEGORIES = {
    "ADD": "Booking without clean",
    "LEDGER_ONLY_BOOKING": "Booking without clean",
    "WATCH_MISSING": "Clean without booking",
    "PROPOSE_CANCEL": "Clean without booking",
    "AUTO_CANCEL": "Clean without booking",
    "FEE_MISMATCH": "Fee vs rate card",
    "FEE_MISSING": "Fee vs rate card",
    "DUPLICATE_SAME_DATE": "Duplicate same-date clean",
}
REPORT_HEADER = ["Date", "Category", "Apartment", "Key", "Check", "Detail"]


def mismatch_report(ops, applied_ids, generated: dt.datetime, op_id=None):
    """Rows for the mismatch_report tab. Ops the writer applied this run (applied_ids) are resolved and dropped,
    except AUTO_CANCEL, which stays listed so the owner sees what was cancelled."""
    if op_id is None:
        from .writer import op_id
    out = []
    for o in ops:
        cat = REPORT_CATEGORIES.get(o["op"])
        if not cat:
            continue
        applied = op_id(o) in applied_ids
        if applied and o["op"] != "AUTO_CANCEL":
            continue
        if o["op"] == "AUTO_CANCEL" and not applied:
            continue
        check = "CANCELLED by sync" if o["op"] == "AUTO_CANCEL" else o["op"]
        out.append([o["date"], cat, o["apt"], o.get("key") or "", check, o["detail"]])
    out.sort(key=lambda r: (r[1], r[0], r[2]))
    return [REPORT_HEADER, ["Generated", generated.strftime("%Y-%m-%d %H:%M"), f"{len(out)} issue(s)", "", "", ""]] + out


# ----------------------------------------------------------------------------- quick-add
def quick_add(rc: RateCard, apt: str, checkout, source: str = "AIRBNB", code: Optional[str] = None,
              config: Optional[str] = None, shifts: int = 1, cleaner: Optional[str] = None,
              fee: Optional[int] = None, now: Optional[dt.datetime] = None,
              existing: Iterable[Task] = ()) -> Task:
    """Owner types a booking before any feed has it. Returns a new unverified Task."""
    if not rc.known(apt):
        raise ValueError(f"unknown apartment {apt!r}")
    checkout = parse_date(checkout)
    now = now or dt.datetime.now()
    source = source.upper()
    if source == "AIRBNB":
        if not (code and CODE_RE.fullmatch(code)):
            raise ValueError("Airbnb quick-add needs the full confirmation code (HM + 8 characters)")
        key, sid = f"AUTO|{apt}|{code}", f"AIRBNB|{code}"
    elif source == "TRIPCOM":
        key, sid = f"MANUAL|TRIPCOM|{apt}|{checkout.isoformat()}", f"TRIPCOM|OWNER-QUICK|{checkout.isoformat()}"
    elif source == "DIRECT":
        key, sid = f"DIRECT|{apt}|{checkout.isoformat()}", f"DIRECT|OWNER-QUICK|{checkout.isoformat()}"
    else:
        raise ValueError(f"unknown source {source!r}")
    if any(t.key == key for t in existing):
        raise ValueError(f"task {key} already exists")
    per = fee if fee is not None else rc.fee(apt, config)
    if per is None:
        raise ValueError(f"no fee in rate card for {apt}; pass fee=")
    return Task(
        key=key, date=checkout, apt=apt, cleaner=cleaner or rc.cleaner(apt),
        config=rc.config_for(apt, config), fee=per * shifts, status="PLANNED", source_id=sid,
        notes=f"{QUICK_MARK} {now:%Y-%m-%d %H:%M} ({source}); reconcile when calendar/earnings catch up",
        created=now,
    )
