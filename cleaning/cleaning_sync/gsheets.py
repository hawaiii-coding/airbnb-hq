"""Google Sheets implementation of writer.SheetClient for the `tasks` tab.

Safety, in addition to what writer.commit() enforces:
  * writes only to spreadsheet ids in `allowed_ids`; the production Cleaning sheet additionally needs
    allow_production=True (set from ALLOW_PRODUCTION_WRITES=true, same convention as the earnings worker)
  * the header row must equal writer.HEADER on every read
  * refuses to overwrite a cell that holds a formula
  * values are written RAW: text stays text, Fee VND is written as a number so payroll SUMIFS keep working
  * rollback blanks appended rows instead of deleting them (deleting rows can shrink formula ranges)
The `service` argument is a googleapiclient Sheets v4 resource (see build_service); tests use a fake.
"""
from __future__ import annotations

import json
import re
from typing import Callable, Optional

from .writer import HEADER

PRODUCTION_IDS = {"1JNJjayasaaLYtUOIe_ukbkolcfsuo1BgnLsgne21T5Y"}
NUMERIC_COLS = {"Fee VND"}
NCOLS = len(HEADER)


class WriteNotAllowed(Exception):
    pass


def col_letter(i: int) -> str:
    s, i = "", i + 1
    while i:
        i, r = divmod(i - 1, 26)
        s = chr(65 + r) + s
    return s


LAST = col_letter(NCOLS - 1)


def _s(v) -> str:
    if v is None:
        return ""
    if isinstance(v, bool):
        return str(v).upper()
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


def _to_api(col: int, value: str):
    if HEADER[col] in NUMERIC_COLS:
        return int(value) if str(value).strip() != "" else ""
    return value


def build_service():  # pragma: no cover - needs google libs + credentials
    import google.auth
    from googleapiclient.discovery import build
    creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/spreadsheets"])
    return build("sheets", "v4", credentials=creds, cache_discovery=False)


class GoogleSheetsClient:
    def __init__(self, spreadsheet_id: str, service, tab: str = "tasks", allowed_ids: Optional[set] = None,
                 allow_production: bool = False, backup_sink: Optional[Callable] = None,
                 log_sink: Optional[Callable] = None):
        self.sid, self.svc, self.tab = spreadsheet_id, service, tab
        self.allowed_ids = set(allowed_ids or ())
        self.allow_production = allow_production
        self.backup_sink = backup_sink
        self.log_sink = log_sink or (lambda e: print(json.dumps(e)))

    # ------------------------------------------------------------------ guards
    def _guard(self):
        if self.sid not in self.allowed_ids:
            raise WriteNotAllowed(f"{self.sid} is not in the write allow-list")
        if self.sid in PRODUCTION_IDS and not self.allow_production:
            raise WriteNotAllowed("production Cleaning sheet: ALLOW_PRODUCTION_WRITES is not set")

    def assert_can_write(self):
        self._guard()
        if not self.backup_sink:
            raise WriteNotAllowed("no backup sink configured: refusing to write without a backup")

    # ------------------------------------------------------------------ reads
    def read_rows(self) -> list[list[str]]:
        res = self.svc.spreadsheets().values().batchGet(
            spreadsheetId=self.sid, ranges=[f"{self.tab}!A1:{LAST}1", f"{self.tab}!A2:{LAST}"],
            valueRenderOption="UNFORMATTED_VALUE", dateTimeRenderOption="SERIAL_NUMBER").execute()
        head, body = res["valueRanges"]
        header = [_s(v) for v in (head.get("values") or [[]])[0]]
        if header != HEADER:
            raise WriteNotAllowed(f"header row of {self.tab!r} changed: {header}")
        rows = [([_s(v) for v in r] + [""] * NCOLS)[:NCOLS] for r in body.get("values", [])]
        while rows and not any(rows[-1]):
            rows.pop()
        return rows

    def _assert_no_formulas(self, cells):
        ranges = [f"{self.tab}!{col_letter(c)}{i + 2}" for i, c, _ in cells]
        res = self.svc.spreadsheets().values().batchGet(
            spreadsheetId=self.sid, ranges=ranges, valueRenderOption="FORMULA").execute()
        for rng in res.get("valueRanges", []):
            for row in rng.get("values", []):
                for v in row:
                    if isinstance(v, str) and v.startswith("="):
                        raise WriteNotAllowed(f"refusing to overwrite a formula cell ({rng.get('range')})")

    # ------------------------------------------------------------------ writes
    def write_cells(self, cells):
        self._guard()
        if not cells:
            return
        self._assert_no_formulas(cells)
        data = [{"range": f"{self.tab}!{col_letter(c)}{i + 2}", "values": [[_to_api(c, v)]]} for i, c, v in cells]
        self.svc.spreadsheets().values().batchUpdate(
            spreadsheetId=self.sid, body={"valueInputOption": "RAW", "data": data}).execute()

    def append_rows(self, rows):
        """Write new rows directly under the last used row, at an exact range we compute ourselves.

        values.append is NOT used: it guesses where the "table" is and, on the real scratch sheet, wrote a row one
        column to the right (B70:Q70) and, in an earlier connector test, above the header. We read the last used row,
        write A{n}:P{m} with values.update, and require the API to report that same range. On any mismatch or error
        both the requested and the reported ranges are cleared before raising, so no stray row can be left behind.
        """
        self._guard()
        if not rows:
            return []
        first_idx = len(self.read_rows())                       # data rows in use (header excluded), trailing blanks trimmed
        first, last = first_idx + 2, first_idx + 1 + len(rows)
        want = f"{self.tab}!A{first}:{LAST}{last}"
        body = {"values": [[_to_api(c, v) for c, v in enumerate(r)] for r in rows]}
        reported = None
        try:
            res = self.svc.spreadsheets().values().update(
                spreadsheetId=self.sid, range=want, valueInputOption="RAW", body=body).execute()
            reported = res.get("updatedRange", "")
            if reported.replace("'", "") != want or res.get("updatedRows") not in (None, len(rows)):
                raise RuntimeError(f"append landed at {reported!r}, expected {want!r}")
        except Exception:
            self._clear_ranges([want] + ([reported] if reported and reported.replace("'", "") != want else []))
            raise
        return list(range(first_idx, first_idx + len(rows)))

    def _clear_ranges(self, ranges):
        try:
            self.svc.spreadsheets().values().batchClear(spreadsheetId=self.sid, body={"ranges": ranges}).execute()
        except Exception as e:  # noqa: BLE001
            self.log({"warning": "could not clear stray append range", "ranges": ranges, "error": repr(e)})

    def clear_rows(self, indices):
        self._guard()
        if indices:
            self.svc.spreadsheets().values().batchClear(
                spreadsheetId=self.sid,
                body={"ranges": [f"{self.tab}!A{i + 2}:{LAST}{i + 2}" for i in indices]}).execute()

    # ------------------------------------------------------------------ mismatch report tab
    def write_report(self, rows, tab: str = "mismatch_report"):
        """Replace the contents of the report tab (created if missing). Same allow-list/production guard as tasks.
        Never touches the tasks tab."""
        self._guard()
        if tab == self.tab:
            raise WriteNotAllowed("the report must not be written to the tasks tab")
        meta = self.svc.spreadsheets().get(spreadsheetId=self.sid, fields="sheets.properties.title").execute()
        if tab not in {s["properties"]["title"] for s in meta.get("sheets", [])}:
            self.svc.spreadsheets().batchUpdate(
                spreadsheetId=self.sid, body={"requests": [{"addSheet": {"properties": {"title": tab}}}]}).execute()
        self.svc.spreadsheets().values().clear(spreadsheetId=self.sid, range=f"{tab}!A:F", body={}).execute()
        self.svc.spreadsheets().values().update(
            spreadsheetId=self.sid, range=f"{tab}!A1", valueInputOption="RAW",
            body={"values": [[str(v) for v in r] for r in rows]}).execute()

    # ------------------------------------------------------------------ side channels
    def backup(self, label, rows):
        if not self.backup_sink:
            raise WriteNotAllowed("no backup sink configured: refusing to write without a backup")
        return self.backup_sink(label, rows)

    def log(self, entry):
        self.log_sink(entry)
