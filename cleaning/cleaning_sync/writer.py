"""Guarded writer for the tasks tab: plan -> verify -> backup -> write -> readback -> rollback.

Same shape as the earnings sync. It talks to the sheet only through the SheetClient protocol below,
so it is tested against an in-memory fake. A real Google Sheets client is NOT included yet: it is built
and first exercised on a scratch copy of the sheet, never on production.

What it will and will not write
  auto_safe ops (no approval needed):
      ADD         append a new row (all columns, new row only)
      ADOPT_CODE  Source ID, Notes, Last seen on an existing row
      VERIFY      Notes (clears the unverified mark), Last seen
  needs_owner ops are applied ONLY if their id is in `approved_ids`:
      MOVE_DATE      Date (+ Source ID if a code is supplied)
      FEE_MISMATCH   Fee VND
      PROPOSE_CANCEL Status -> CANCELLED
  Anything else (Cleaner, Confirmed at, Paid at, COMPLETED/CONFIRMED status) is never written by this module.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
from typing import Iterable, Optional, Protocol

from .planner import iso

HEADER = ["Key", "Date", "Apartment", "Display", "Cleaner", "Type", "Kind", "Time", "Notes",
          "Same-day arrival", "Fee VND", "Status", "Confirmed at", "Paid at", "Last seen", "Source ID"]

# columns each op may touch on an EXISTING row
ALLOWED_COLUMNS = {
    "ADOPT_CODE": {"Source ID", "Notes", "Last seen"},
    "VERIFY": {"Notes", "Last seen"},
    "MOVE_DATE": {"Date", "Source ID", "Notes"},
    "FEE_MISMATCH": {"Fee VND", "Notes"},
    "PROPOSE_CANCEL": {"Status", "Notes"},
    "AUTO_CANCEL": {"Status", "Notes"},   # PLANNED -> CANCELLED only, when feed AND earnings ledger agree
    "TIDY_TYPE": {"Type", "Notes"},
}
AUTO_OPS = {"ADD", "ADOPT_CODE", "VERIFY", "AUTO_CANCEL", "TIDY_TYPE"}
MAX_AUTO_CANCELS = 3   # more than this in one run looks like a data problem, not real cancellations
APPROVABLE_OPS = {"MOVE_DATE", "FEE_MISMATCH", "PROPOSE_CANCEL"}
NEVER = {"Cleaner", "Confirmed at", "Paid at", "Key"}


class SheetClient(Protocol):
    def read_rows(self) -> list[list[str]]: ...                     # data rows only, in sheet order
    def write_cells(self, cells: list[tuple[int, int, str]]) -> None: ...   # (row_idx, col_idx, value), 0-based data rows
    def append_rows(self, rows: list[list[str]]) -> list[int]: ...  # returns the new row indices
    def clear_rows(self, indices: list[int]) -> None: ...           # blank rows we appended (rollback); never delete rows
    def backup(self, label: str, rows: list[list[str]]) -> str: ... # returns a backup reference
    def log(self, entry: dict) -> None: ...


class GuardError(Exception):
    pass


def op_id(op: dict) -> str:
    base = json.dumps([op["op"], op.get("key"), op["date"], op["apt"], op.get("new_date"), op.get("proposed_fee")],
                      sort_keys=True)
    return hashlib.sha1(base.encode()).hexdigest()[:10]


def rows_hash(rows: list[list[str]]) -> str:
    return hashlib.sha256(json.dumps(rows).encode()).hexdigest()


def _col(name: str) -> int:
    return HEADER.index(name)


def _find(rows, key):
    hits = [i for i, r in enumerate(rows) if r and r[0] == key]
    return hits[0] if len(hits) == 1 else (None if not hits else -1)


def _note(old: str, add: str) -> str:
    return f"{old} | {add}" if old else add


def build_mutations(ops: Iterable[dict], rows: list[list[str]], approved_ids: set, today: dt.date,
                    max_adds: int = 40):
    """Returns (mutations, skipped). Each mutation: dict(kind, ...). Validates against current rows."""
    muts, skipped, adds = [], [], 0
    seen_keys = {r[0] for r in rows if r}
    n_cancel = sum(1 for o in ops if o["op"] == "AUTO_CANCEL")
    for op in ops:
        oid = op_id(op)
        kind = op["op"]
        if kind in AUTO_OPS:
            pass
        elif kind in APPROVABLE_OPS:
            if oid not in approved_ids:
                continue  # waits for the owner; not a skip
        else:
            continue  # info or flag ops never write
        if kind == "ADD":
            r = op["row"]
            if r["fee"] is None or not r["cleaner"]:
                skipped.append((oid, "ADD without fee/cleaner"))
                continue
            if r["key"] in seen_keys:
                skipped.append((oid, "key already exists (stale plan)"))
                continue
            adds += 1
            if adds > max_adds:
                raise GuardError(f"plan wants more than {max_adds} new rows: refusing")
            note = f"SYNC {today.isoformat()}: new from feed ({r['verification']}); owner columns set from rate card"
            new = [""] * len(HEADER)
            for name, val in (("Key", r["key"]), ("Date", iso(r["date"])), ("Apartment", r["apt"]), ("Display", r["apt"]),
                              ("Cleaner", r["cleaner"]), ("Type", r["config"]), ("Kind", "CHECKOUT"),
                              ("Notes", note), ("Same-day arrival", r.get("same_day", "")),
                              ("Fee VND", str(r["fee"])), ("Status", "PLANNED"),
                              ("Last seen", today.isoformat()), ("Source ID", r["source_id"])):
                new[_col(name)] = val
            seen_keys.add(r["key"])
            muts.append({"kind": "ADD", "id": oid, "row": new})
            continue
        i = _find(rows, op["key"])
        if i is None or i == -1:
            skipped.append((oid, "row not found or key not unique"))
            continue
        row = rows[i]
        cells: dict[str, str] = {}
        if kind == "ADOPT_CODE":
            cells["Source ID"] = f"AIRBNB|{op['code']}"
            cells["Notes"] = _note(row[_col("Notes")], f"SYNC {today.isoformat()}: legacy row crosswalked to {op['code']}")
            cells["Last seen"] = today.isoformat()
        elif kind == "VERIFY":
            notes = row[_col("Notes")]
            if "QUICK-ADD unverified" not in notes:
                skipped.append((oid, "nothing to verify"))
                continue
            cells["Notes"] = notes.replace("QUICK-ADD unverified", "QUICK-ADD verified")
            cells["Last seen"] = today.isoformat()
        elif kind == "MOVE_DATE":
            if row[_col("Status")] not in ("PLANNED",):
                skipped.append((oid, f"status {row[_col('Status')]} is owner territory"))
                continue
            cells["Date"] = iso(op["new_date"])
            if op.get("code"):
                cells["Source ID"] = f"AIRBNB|{op['code']}"
            cells["Notes"] = _note(row[_col("Notes")], f"SYNC {today.isoformat()}: date {iso(row[_col('Date')])} -> {iso(op['new_date'])} (owner approved)")
        elif kind == "FEE_MISMATCH":
            if row[_col("Status")] not in ("PLANNED",):
                skipped.append((oid, f"status {row[_col('Status')]}: fee on completed rows not changed"))
                continue
            cells["Fee VND"] = str(op["proposed_fee"])
            cells["Notes"] = _note(row[_col("Notes")], f"SYNC {today.isoformat()}: fee {row[_col('Fee VND')]} -> {op['proposed_fee']} (owner approved)")
        elif kind == "AUTO_CANCEL":
            if n_cancel > MAX_AUTO_CANCELS:
                skipped.append((oid, f"too many auto-cancels in one run ({n_cancel} > {MAX_AUTO_CANCELS}): left for owner"))
                continue
            if row[_col("Status")] != "PLANNED":
                skipped.append((oid, f"status {row[_col('Status')]}: only PLANNED rows can be cancelled"))
                continue
            cells["Status"] = "CANCELLED"
            cells["Notes"] = _note(row[_col("Notes")], f"SYNC {today.isoformat()}: auto-cancelled: {op['detail']}")
        elif kind == "TIDY_TYPE":
            if row[_col("Type")] == op["new_type"]:
                skipped.append((oid, "already tidy"))
                continue
            cells["Type"] = op["new_type"]
            cells["Notes"] = _note(row[_col("Notes")], f"SYNC {today.isoformat()}: Type {row[_col('Type')]} -> {op['new_type']} (rate card)")
        elif kind == "PROPOSE_CANCEL":
            if row[_col("Status")] != "PLANNED":
                skipped.append((oid, f"status {row[_col('Status')]}: only PLANNED rows can be cancelled"))
                continue
            cells["Status"] = "CANCELLED"
            cells["Notes"] = _note(row[_col("Notes")], f"SYNC {today.isoformat()}: cancelled, booking gone from feed (owner approved)")
        bad = set(cells) - ALLOWED_COLUMNS[kind]
        if bad or set(cells) & NEVER:
            raise GuardError(f"{kind} tried to write forbidden columns {sorted(bad | (set(cells) & NEVER))}")
        muts.append({"kind": kind, "id": oid, "key": op["key"], "cells": cells, "before": {c: row[_col(c)] for c in cells}})
    return muts, skipped


def commit(client: SheetClient, plan_ops: list[dict], approved_ids: Iterable[str] = (), today: Optional[dt.date] = None,
           dry_run: bool = True, max_adds: int = 40) -> dict:
    today = today or dt.date.today()
    rows0 = client.read_rows()
    h0 = rows_hash(rows0)
    muts, skipped = build_mutations(plan_ops, rows0, set(approved_ids), today, max_adds)
    report = {"dry_run": dry_run, "mutations": [{k: v for k, v in m.items() if k != "before"} for m in muts],
              "skipped": skipped, "applied": False, "rolled_back": False, "backup": None}
    if dry_run or not muts:
        return report

    check = getattr(client, "assert_can_write", None)
    if check:
        check()                                      # permission gates fail fast, before backup or any write

    report["backup"] = client.backup(f"pre-sync-{today.isoformat()}-{h0[:8]}", rows0)

    rows1 = client.read_rows()                       # revision guard: abort if anyone edited since the plan
    if rows_hash(rows1) != h0:
        raise GuardError("tasks changed since the plan was built; re-plan")

    cells = [(_find(rows0, m["key"]), _col(c), v) for m in muts if m["kind"] != "ADD" for c, v in m["cells"].items()]
    new_rows = [m["row"] for m in muts if m["kind"] == "ADD"]
    appended: list[int] = []
    try:
        if cells:
            client.write_cells(cells)
        if new_rows:
            appended = client.append_rows(new_rows)
        _verify_readback(client, rows0, cells, new_rows, appended)
    except Exception as e:  # noqa: BLE001 - any failure triggers rollback
        report["error"] = repr(e)
        _rollback(client, rows0, muts, appended)
        report["rolled_back"] = True
        client.log({"when": dt.datetime.now().isoformat(), "result": "ROLLED_BACK", "error": repr(e)})
        return report
    report["applied"] = True
    client.log({"when": dt.datetime.now().isoformat(), "result": "OK", "backup": report["backup"],
                "n_mutations": len(muts), "ids": [m["id"] for m in muts]})
    return report


def _verify_readback(client, rows0, cells, new_rows, appended):
    after = client.read_rows()
    expected = [list(r) for r in rows0]
    for i, c, v in cells:
        expected[i][c] = v
    expected += [list(r) for r in new_rows]
    if after != expected:
        raise GuardError("readback does not match what was written")


def _rollback(client, rows0, muts, appended):
    if appended:
        client.clear_rows(appended)
    restore = []
    for m in muts:
        if m["kind"] == "ADD":
            continue
        i = _find(rows0, m["key"])
        restore += [(i, _col(c), v) for c, v in m["before"].items()]
    if restore:
        client.write_cells(restore)
    if client.read_rows() != rows0:
        raise GuardError("ROLLBACK DID NOT RESTORE THE ORIGINAL ROWS: restore from the backup immediately")
