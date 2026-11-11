"""Minimal guarded Google Sheets writer for the Live Availability tab.

Guards:
- Only tab names on WRITE_TABS can be written.
- Only spreadsheet ids on WRITE_ALLOWED_IDS (env, comma separated) can be written.
- The tab is fully rebuilt each run (clear + write), then read back and compared.
"""
from __future__ import annotations

import os

WRITE_TABS = {"Live Availability"}


class WriteRefused(Exception):
    pass


def _service():
    import google.auth
    from googleapiclient.discovery import build
    creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/spreadsheets"])
    return build("sheets", "v4", credentials=creds, cache_discovery=False)


def _check(spreadsheet_id: str, tab: str) -> None:
    allowed = {s.strip() for s in os.environ.get("WRITE_ALLOWED_IDS", "").split(",") if s.strip()}
    if spreadsheet_id not in allowed:
        raise WriteRefused("spreadsheet not on WRITE_ALLOWED_IDS")
    if tab not in WRITE_TABS:
        raise WriteRefused(f"tab '{tab}' not on the write allow-list")


def _norm(rows: list[list]) -> list[list[str]]:
    """Sheets drops trailing empty cells and rows; compare on that basis."""
    out = []
    for r in rows:
        cells = [str(c) for c in r]
        while cells and cells[-1] == "":
            cells.pop()
        out.append(cells)
    while out and not out[-1]:
        out.pop()
    return out


def rebuild_tab(spreadsheet_id: str, tab: str, rows: list[list], service=None) -> int:
    _check(spreadsheet_id, tab)
    svc = service or _service()
    meta = svc.spreadsheets().get(spreadsheetId=spreadsheet_id, fields="sheets.properties.title").execute()
    titles = {s["properties"]["title"] for s in meta.get("sheets", [])}
    if tab not in titles:
        svc.spreadsheets().batchUpdate(spreadsheetId=spreadsheet_id, body={
            "requests": [{"addSheet": {"properties": {"title": tab}}}]}).execute()
    rng = f"'{tab}'"
    svc.spreadsheets().values().clear(spreadsheetId=spreadsheet_id, range=rng, body={}).execute()
    svc.spreadsheets().values().update(spreadsheetId=spreadsheet_id, range=f"{rng}!A1",
                                       valueInputOption="RAW", body={"values": rows}).execute()
    back = svc.spreadsheets().values().get(spreadsheetId=spreadsheet_id, range=rng).execute().get("values", [])
    if _norm(back) != _norm(rows):
        raise RuntimeError("readback mismatch after writing Live Availability")
    return len(rows)
