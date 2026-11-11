"""Cloud Run job: rebuild the 'Live Availability' tab from the Airbnb iCal feeds.

Usage:
  python -m jobs.live_availability --dry-run          # print, write nothing
  python -m jobs.live_availability                     # write (needs SPREADSHEET_ID + WRITE_ALLOWED_IDS)
Env: ICAL_URL_<key> per listing (from Secret Manager), SPREADSHEET_ID, WRITE_ALLOWED_IDS,
     HORIZON_DAYS (default 60), TZ_OFFSET_HOURS (default 7, HCMC).
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timedelta, timezone

from hq.availability import build
from hq.feeds import fetch_feed, load_listings

TAB = "Live Availability"


def make_rows(results, today, horizon, run_at):
    nights, listing_rows, apt_status, gaps = build(results, today, horizon)
    failed = [r.listing.key for r in results if not r.ok]
    rows = [
        ["LIVE AVAILABILITY (auto, rebuilt each run; do not edit)"],
        ["Run at (HCMC)", run_at, "Horizon", f"{nights[0]} to {nights[-1]}"],
        ["Feeds OK", f"{len(results) - len(failed)}/{len(results)}",
         "Failed (nights show UNKNOWN)", ", ".join(failed) or "none"],
        ["Note", "iCal feeds lag a few hours behind Airbnb; owner messages beat this tab. "
                 "BLOCKED = Airbnb 'Not available' (often the sibling 1BR/2BR booking, Trip.com or direct)."],
        [],
        ["GAPS (apartment level, 1BR+2BR combined)"],
        ["Apartment", "First open night", "Checkout after gap", "Nights", "Type"],
    ]
    for g in sorted(gaps, key=lambda g: (g.start, g.apartment)):
        rows.append([g.apartment, g.start.isoformat(), g.end.isoformat(), g.nights, g.label])
    rows += [[], ["NIGHTS BY LISTING (code = reserved)"],
             ["Apartment", "Listing", "Config"] + [d.strftime("%a %d %b") for d in nights]]
    for r, st in sorted(listing_rows, key=lambda x: (x[0].listing.apartment, x[0].listing.config)):
        rows.append([r.listing.apartment, r.listing.key, r.listing.config] + [st[d] for d in nights])
    rows += [[], ["NIGHTS BY APARTMENT"], ["Apartment", "", ""] + [d.strftime("%a %d %b") for d in nights]]
    for apt, st in sorted(apt_status.items()):
        rows.append([apt, "", ""] + [st[d] for d in nights])
    return rows, gaps, failed


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    tz = timezone(timedelta(hours=int(os.environ.get("TZ_OFFSET_HOURS", "7"))))
    now = datetime.now(tz)
    horizon = int(os.environ.get("HORIZON_DAYS", "60"))
    results = [fetch_feed(l) for l in load_listings()]
    rows, gaps, failed = make_rows(results, now.date(), horizon, now.strftime("%Y-%m-%d %H:%M"))

    print(f"feeds ok {len(results) - len(failed)}/{len(results)}; failed: {failed or 'none'}")
    for g in gaps:
        if g.bounded_after and g.nights <= 3:
            print(f"  {g.apartment}: {g.start} x{g.nights} ({g.label})")
    if args.dry_run:
        print(f"dry run: {len(rows)} rows not written")
        return 0
    from hq.sheets import rebuild_tab
    sid = os.environ["SPREADSHEET_ID"]
    n = rebuild_tab(sid, TAB, rows)
    print(f"wrote {n} rows to '{TAB}' and read back OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
