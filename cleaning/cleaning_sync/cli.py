"""python -m cleaning_sync.cli --tasks T.csv --bookings B.json [--today 2026-11-09] [--horizon 2026-11-30]
Read-only: prints a markdown plan. Writes nothing anywhere."""
from __future__ import annotations
import argparse, csv, datetime as dt, json, os
from .planner import Booking, RateCard, Task, parse_date, plan, quick_add

HERE = os.path.dirname(os.path.abspath(__file__))


def load_tasks(path):
    with open(path, newline="") as f:
        return [Task(r["Key"], parse_date(r["Date"]), r["Apartment"], r["Cleaner"], r["Type"],
                     int(r["Fee VND"]) if r["Fee VND"] else None, r["Status"], r.get("Source ID", ""),
                     r.get("Notes", "")) for r in csv.DictReader(f)]


def load_bookings(path):
    data = json.load(open(path))
    out = []
    for fd in data["feeds"]:
        for kind, code, s, e in fd["events"]:
            out.append(Booking(code, fd["apt"], dt.date.fromisoformat(s), dt.date.fromisoformat(e),
                               "RESERVED" if kind == "R" else "BLOCK", fd["feed"], fd.get("config")))
    return out


def render(result, today, horizon):
    ops = result["ops"]
    lines = [f"# Cleaning sync plan (read-only) {today} .. {horizon}", ""]
    for tier, title in (("needs_owner", "Needs your decision"), ("auto_safe", "Safe to apply"), ("info", "For information")):
        sel = [o for o in ops if o["tier"] == tier]
        lines += [f"## {title} ({len(sel)})"]
        for o in sel:
            lines.append(f"- {o['date']} | {o['apt']} | {o['op']} | {o['detail']}")
        lines.append("")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tasks", required=True); ap.add_argument("--bookings", required=True)
    ap.add_argument("--rate-card", default=os.path.join(HERE, "..", "rate_card.json"))
    ap.add_argument("--today", default=dt.date.today().isoformat()); ap.add_argument("--horizon", default=None)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    today = dt.date.fromisoformat(a.today)
    horizon = dt.date.fromisoformat(a.horizon) if a.horizon else today + dt.timedelta(days=21)
    res = plan(load_tasks(a.tasks), load_bookings(a.bookings), RateCard.load(a.rate_card), today, horizon)
    print(json.dumps(res, indent=1) if a.json else render(res, today, horizon))


if __name__ == "__main__":
    main()
