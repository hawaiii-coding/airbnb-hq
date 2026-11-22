"""Write each unit's monthly cleaning cost into the Master Financial Tracker (owner 2026-11-22).

Target: tab 'Easy Monthly Entry', block "<YYYY-MM>  |  MONTHLY ENTRY", row "Cleaning", one column per apartment
(header row = the row whose column A is "Metric"). Value = sum of Fee VND of every non-cancelled task of that apartment
dated in that month (accrual: cleans still PLANNED later in the month are included).

Safety:
  * writes only when TRACKER_SPREADSHEET_ID is set and ALLOW_TRACKER_CLEANING_WRITE=true
  * touches only the Cleaning row of the given month; refuses if any target cell holds a formula
  * the old values are returned (and logged) as the rollback; readback must match what was written
"""
from __future__ import annotations

import datetime as dt
from typing import Optional

from .planner import parse_date
from .writer import HEADER

TAB = "Easy Monthly Entry"


def monthly_cleaning(rows, month: str) -> dict:
    """{apartment: total fee} for non-cancelled tasks dated in month 'YYYY-MM'."""
    out: dict = {}
    for r in rows:
        if not r or not r[0]:
            continue
        g = lambda n: r[HEADER.index(n)] if HEADER.index(n) < len(r) else ""
        if g("Status") == "CANCELLED" or g("Status") not in ("PLANNED", "CONFIRMED", "COMPLETED"):
            continue
        try:
            d = parse_date(g("Date"))
            fee = int(float(g("Fee VND")))
        except (ValueError, TypeError):
            continue
        if f"{d:%Y-%m}" == month:
            out[g("Apartment")] = out.get(g("Apartment"), 0) + fee
    return out


def col_letter(i: int) -> str:
    s, i = "", i + 1
    while i:
        i, r = divmod(i - 1, 26)
        s = chr(65 + r) + s
    return s


def locate(grid, month: str):
    """(row_index, {apartment: col_index}) of the Cleaning row for `month`. grid = rows of FORMULA-rendered cells."""
    head = next((r for r in grid if r and str(r[0]).strip() == "Metric"), None)
    if not head:
        raise ValueError("header row 'Metric' not found")
    cols = {str(v).strip(): i for i, v in enumerate(head) if i > 0 and str(v).strip()}
    start = next((i for i, r in enumerate(grid) if r and str(r[0]).strip().startswith(month)
                  and "MONTHLY ENTRY" in str(r[0])), None)
    if start is None:
        raise ValueError(f"block '{month} | MONTHLY ENTRY' not found")
    for i in range(start + 1, min(start + 15, len(grid))):
        r = grid[i]
        if r and str(r[0]).strip().upper().endswith("MONTHLY ENTRY"):
            break
        if r and str(r[0]).strip() == "Cleaning":
            return i, cols
    raise ValueError(f"'Cleaning' row not found under {month}")


def plan_write(grid, month: str, totals: dict):
    """Returns (row_index, cells[(col, old, new)]). Raises if a target cell is a formula."""
    ri, cols = locate(grid, month)
    row = grid[ri] + [""] * (max(cols.values()) + 1 - len(grid[ri]))
    cells = []
    for apt, ci in sorted(cols.items(), key=lambda x: x[1]):
        old = row[ci]
        if isinstance(old, str) and old.startswith("="):
            raise ValueError(f"{TAB}!{col_letter(ci)}{ri + 1} holds a formula; refusing to overwrite")
        cells.append((ci, old, totals.get(apt, 0)))
    unknown = set(totals) - set(cols)
    return ri, cells, sorted(unknown)


def write_month(svc, sid: str, month: str, totals: dict) -> dict:
    res = svc.spreadsheets().values().get(spreadsheetId=sid, range=f"'{TAB}'!A1:Z400",
                                          valueRenderOption="FORMULA").execute()
    grid = res.get("values", [])
    ri, cells, unknown = plan_write(grid, month, totals)
    first, last = min(c for c, _, _ in cells), max(c for c, _, _ in cells)
    by_col = {c: new for c, _, new in cells}
    values = [[by_col.get(c, grid[ri][c] if c < len(grid[ri]) else "") for c in range(first, last + 1)]]
    rng = f"'{TAB}'!{col_letter(first)}{ri + 1}:{col_letter(last)}{ri + 1}"
    svc.spreadsheets().values().update(spreadsheetId=sid, range=rng, valueInputOption="RAW",
                                       body={"values": values}).execute()
    back = svc.spreadsheets().values().get(spreadsheetId=sid, range=rng,
                                           valueRenderOption="UNFORMATTED_VALUE").execute().get("values", [[]])[0]
    back = (back + [""] * len(values[0]))[:len(values[0])]
    ok = all(int(float(b or 0)) == int(v or 0) for b, v in zip(back, values[0]))
    if not ok:
        old = [[c_old for _, c_old, _ in cells]]
        svc.spreadsheets().values().update(spreadsheetId=sid, range=rng, valueInputOption="RAW",
                                           body={"values": old}).execute()
        raise RuntimeError(f"readback mismatch on {rng}; old values restored")
    return {"range": rng, "month": month, "written": values[0], "previous": [o for _, o, _ in cells],
            "unknown_apartments": unknown}


def months_to_write(cutoff: dt.date) -> list:
    """Current month; also the previous one during the first 7 days (so last month gets its final figure)."""
    out = [f"{cutoff:%Y-%m}"]
    if cutoff.day <= 7:
        prev = cutoff.replace(day=1) - dt.timedelta(days=1)
        out.insert(0, f"{prev:%Y-%m}")
    return out
