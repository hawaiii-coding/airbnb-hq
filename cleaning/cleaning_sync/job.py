"""Entrypoint for the Cloud Run job.  python -m cleaning_sync.job --mode plan|apply ...

plan  : read tasks, poll feeds, print the plan, store miss counts. Writes nothing to the sheet.
apply : same plan, then writer.commit(dry_run=False) for the auto-safe ops plus the op ids listed in
        --approved (a JSON list). Production needs ALLOW_PRODUCTION_WRITES=true AND the id in the allow-list.
Env: SPREADSHEET_ID, WRITE_ALLOWED_IDS (comma list), ALLOW_PRODUCTION_WRITES, FEEDS_CONFIG, ICAL_URL_* secrets,
     BACKUP_GCS_URI + STATE_GCS_URI (gs://bucket/prefix) in the cloud, or BACKUP_DIR + STATE_PATH locally.
     LEDGER_SPREADSHEET_ID (optional): Master Financial Tracker id. Read-only; adds earnings-ledger cross-checks.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os

from . import gcs, gsheets, poller, writer
from .ledger import LedgerReader
from .planner import RateCard, Task, mismatch_report, parse_date, plan

DEFAULT_HORIZON_DAYS = 45


DEFAULT_FEEDS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "feeds.example.json")


def feeds_config_path(env) -> str:
    """FEEDS_CONFIG if set, else the feeds.example.json shipped in the image (feed -> apartment mapping only;
    URLs come from the ICAL_URL_* secrets)."""
    return env.get("FEEDS_CONFIG") or DEFAULT_FEEDS


def rows_to_tasks(rows):
    out = []
    for r in rows:
        if not r or not r[0]:
            continue
        g = lambda name: r[writer.HEADER.index(name)]
        try:
            d = parse_date(g("Date"))
        except ValueError:
            continue
        fee = int(float(g("Fee VND"))) if g("Fee VND") not in ("", None) else None
        out.append(Task(g("Key"), d, g("Apartment"), g("Cleaner"), g("Type"), fee, g("Status"),
                        g("Source ID"), g("Notes"), clean_kind=g("Kind")))
    return out


class LocalBackupSink:
    def __init__(self, directory):
        self.dir = directory
        os.makedirs(directory, exist_ok=True)

    def __call__(self, label, rows):
        path = os.path.join(self.dir, f"{label}.json")
        with open(path, "w") as f:
            json.dump({"header": writer.HEADER, "rows": rows}, f)
        with open(path) as f:                       # verify the backup is readable and complete
            if json.load(f)["rows"] != rows:
                raise RuntimeError("backup verification failed")
        return path


class LocalState:
    def __init__(self, path):
        self.path = path

    def load(self):
        try:
            with open(self.path) as f:
                return json.load(f)
        except FileNotFoundError:
            return {"miss_counts": {}}

    def save(self, state):
        with open(self.path, "w") as f:
            json.dump(state, f)


def make_sinks(env, gcs_client=None):
    """STATE_GCS_URI / BACKUP_GCS_URI (gs://bucket/prefix) in the cloud; local files otherwise."""
    backup = (gcs.GcsBackupSink(env["BACKUP_GCS_URI"], gcs_client) if env.get("BACKUP_GCS_URI")
              else LocalBackupSink(env.get("BACKUP_DIR", "./backups")))
    state = (gcs.GcsState(env["STATE_GCS_URI"], gcs_client) if env.get("STATE_GCS_URI")
             else LocalState(env.get("STATE_PATH", "./state.json")))
    return backup, state


def run(args, service=None, fetch=None, env=os.environ, gcs_client=None):
    today = dt.date.fromisoformat(args.today) if args.today else dt.date.today()
    horizon = today + dt.timedelta(days=args.horizon_days)
    sid = env["SPREADSHEET_ID"]
    backup_sink, state_store = make_sinks(env, gcs_client)
    client = gsheets.GoogleSheetsClient(
        sid, service or gsheets.build_service(), allowed_ids=set(filter(None, env.get("WRITE_ALLOWED_IDS", "").split(","))),
        allow_production=env.get("ALLOW_PRODUCTION_WRITES", "").lower() == "true",
        backup_sink=backup_sink)
    rows = client.read_rows()
    tasks = rows_to_tasks(rows)
    polled = poller.poll(poller.load_feeds(feeds_config_path(env)), fetch=fetch, env=env)
    state = state_store.load()
    ledger = None
    lid = env.get("LEDGER_SPREADSHEET_ID", "")
    if lid:   # optional, read-only; absent = calendar-only behaviour as before
        ledger = LedgerReader(lid, service or gsheets.build_service()).read(now=dt.datetime.utcnow())
    res = plan(tasks, polled["bookings"], RateCard.load(args.rate_card), today, horizon,
               miss_counts=state.get("miss_counts"), covered_apts=polled["covered_apts"], grace_polls=args.grace_polls,
               ledger=ledger)
    report = {"mode": args.mode, "today": today.isoformat(), "feeds_failed": polled["failed"],
              "ledger": (None if ledger is None else
                         {"usable": ledger.usable, "reason": ledger.unusable_reason, "codes": len(ledger.entries),
                          "latest_import": str(ledger.latest_import) if ledger.latest_import else None}),
              "ops": [dict(o, id=writer.op_id(o)) for o in res["ops"]]}
    if args.mode == "apply":
        approved = set(json.load(open(args.approved))) if args.approved else set()
        approved |= set(filter(None, (args.approved_ids or "").split(",")))
        report["commit"] = writer.commit(client, res["ops"], approved, today, dry_run=False)
    applied = ({m["id"] for m in report["commit"]["mutations"]}
               if report.get("commit", {}).get("applied") else set())
    mrows = mismatch_report(res["ops"], applied, dt.datetime.now())
    report["mismatch_report"] = {"issues": len(mrows) - 2}
    if args.mode == "apply" and not getattr(args, "no_report", False):
        try:   # report tab only; a failure here never undoes or blocks the tasks write
            client.write_report(mrows)
            report["mismatch_report"]["written"] = True
        except Exception as e:  # noqa: BLE001
            report["mismatch_report"]["error"] = f"{type(e).__name__}: {str(e)[:120]}"
    state_store.save({"miss_counts": res["miss_counts"], "updated": dt.datetime.now().isoformat()})
    return report


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["plan", "apply"], default="plan")
    ap.add_argument("--today"); ap.add_argument("--horizon-days", type=int, default=DEFAULT_HORIZON_DAYS)
    ap.add_argument("--grace-polls", type=int, default=4)  # ~1-2h at a 15-30 min poll
    ap.add_argument("--approved"); ap.add_argument("--approved-ids", help="comma-separated op ids from a plan report")
    ap.add_argument("--no-report", action="store_true", help="skip writing the mismatch_report tab")
    ap.add_argument("--rate-card", default=os.path.join(os.path.dirname(__file__), "..", "rate_card.json"))
    print(json.dumps(run(ap.parse_args()), indent=1, default=str))


if __name__ == "__main__":
    main()
