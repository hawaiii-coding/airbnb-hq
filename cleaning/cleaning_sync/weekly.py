"""Sunday payroll + schedule messages for the cleaners (owner design 2026-11-12). Pure functions; no I/O.

Pay rule (BUSINESS_RULES 3, owner 2026-11-12):
  owed = opening balance + fees of every non-cancelled task dated start..cutoff
         + monthly bonuses due (last Sunday of the month, months not already settled)
         - payments recorded in the `payments` tab since start
  Tasks dated up to the cutoff (Sunday 18:00 run = Sunday) count even if still PLANNED: each is listed in the message,
  so the owner can spot an exception before paying. CANCELLED never counts.
Schedule: next Mon-Sun, cancelled and PHK left out (same as the Weekly Schedule tab).
"""
from __future__ import annotations

import datetime as dt
from typing import Optional

from .planner import parse_date
from .writer import HEADER

COUNTED = {"PLANNED", "CONFIRMED", "COMPLETED"}
DAY = ["T2", "T3", "T4", "T5", "T6", "T7", "CN"]
PAYMENTS_HEADER = ["Date", "Cleaner", "Amount VND", "Memo"]


def vnd(n: int) -> str:
    return f"{n:,}".replace(",", ".") + "đ"


def _g(r, name):
    i = HEADER.index(name)
    return r[i] if i < len(r) else ""


def _date(v) -> Optional[dt.date]:
    try:
        return parse_date(v)
    except (ValueError, TypeError):
        return None


def _fee(v) -> int:
    try:
        return int(float(v))
    except (ValueError, TypeError):
        return 0


def monday_of(d: dt.date) -> dt.date:
    return d - dt.timedelta(days=d.weekday())


def week_memo(monday: dt.date) -> str:
    """'Don nha tuan 2 T11' for the week starting Mon 9 Nov 2026 (Mon 2 Nov = tuan 1)."""
    return f"Don nha tuan {(monday.day - 1) // 7 + 1} T{monday.month}"


def last_sunday_of_month(d: dt.date) -> bool:
    return d.weekday() == 6 and (d + dt.timedelta(days=7)).month != d.month


def hhmm(v) -> str:
    """Sheets returns a time cell as a day fraction (10:00 -> 0.41666...); text times pass through."""
    s = str(v or "").strip()
    try:
        f = float(s)
    except ValueError:
        return s
    if 0 <= f < 1:
        m = round(f * 24 * 60)
        return f"{m // 60:02d}:{m % 60:02d}"
    return s


def _label(r) -> str:
    disp = _g(r, "Display") or _g(r, "Apartment")
    kind = (_g(r, "Kind") or "").upper()
    if kind == "STAYOVER":
        t = hhmm(_g(r, "Time"))
        return f"{disp} - {t} - dọn lưu" if t else f"{disp} - dọn lưu"
    if kind == "MAINTENANCE":
        return f"{disp} - dọn bảo trì"
    return disp


def _tasks(rows, cleaner, start, end, exclude_phk=False):
    out = []
    for r in rows:
        if not r or not r[0] or _g(r, "Cleaner") != cleaner or _g(r, "Status") not in COUNTED:
            continue
        if exclude_phk and _g(r, "Apartment") == "PHK":
            continue
        d = _date(_g(r, "Date"))
        if d and start <= d <= end:
            out.append((d, hhmm(_g(r, "Time")) or "99", _label(r), _fee(_g(r, "Fee VND")), r))
    return sorted(out, key=lambda x: (x[0], x[1], x[2]))


def _by_day(items, with_fee):
    lines, cur = [], None
    for d, _, label, fee, _r in items:
        txt = f"{label} {vnd(fee)}" if with_fee else label
        if d != cur:
            lines.append([d, [txt]])
            cur = d
        else:
            lines[-1][1].append(txt)
    return [f"{DAY[d.weekday()]} {d:%d/%m}: " + ", ".join(t) for d, t in lines]


def pay_summary(rows, payments, cleaner, cutoff: dt.date, cfg: dict) -> dict:
    start = dt.date.fromisoformat(cfg["start"])
    monday = monday_of(cutoff)
    all_items = _tasks(rows, cleaner, start, cutoff)
    week_items = [x for x in all_items if x[0] >= monday]
    paid = sum(_fee(p[2]) for p in payments if len(p) >= 3 and p[1] == cleaner
               and (_date(p[0]) or dt.date.min) >= start)
    bonus_amt = cfg.get("bonus", {}).get(cleaner, 0)
    settled = set(cfg.get("bonus_settled", {}).get(cleaner, []))
    bonuses, d = [], start
    while d <= cutoff:                                     # every last-Sunday-of-month from start to cutoff
        if bonus_amt and last_sunday_of_month(d) and f"{d:%Y-%m}" not in settled:
            bonuses.append((d, bonus_amt))
        d += dt.timedelta(days=1)
    bonus_now = sum(a for b, a in bonuses if b >= monday)
    owed = cfg.get("opening", {}).get(cleaner, 0) + sum(x[3] for x in all_items) + sum(a for _, a in bonuses) - paid
    week_total = sum(x[3] for x in week_items)
    return {"cleaner": cleaner, "owed": owed, "week_total": week_total, "bonus_now": bonus_now,
            "carry": owed - week_total - bonus_now, "paid_since_start": paid, "items": week_items,
            "monday": monday, "cutoff": cutoff}


def pay_message(s: dict, cfg: dict) -> list[str]:
    name = cfg["names"][s["cleaner"]]
    m, sun = s["monday"], s["monday"] + dt.timedelta(days=6)
    lines = [f"{name} ơi, e gửi tiền dọn nhà tuần {m:%d/%m} - {sun:%d/%m} ạ:"]
    lines += _by_day(s["items"], with_fee=True) or ["(tuần này không có lịch)"]
    if s["bonus_now"]:
        lines.append(f"Thưởng tháng {sun.month}: {vnd(s['bonus_now'])}")
    if s["carry"] > 0:
        lines.append(f"Các tuần trước còn: {vnd(s['carry'])}")
    elif s["carry"] < 0:
        lines.append(f"Đã chuyển dư trước đó: -{vnd(-s['carry'])}")
    lines += [f"Tổng: {vnd(s['owed'])}", f"Nội dung CK: {week_memo(m)}"]
    return lines


def schedule_message(rows, cleaner, next_monday: dt.date, cfg: dict) -> list[str]:
    name = cfg["names"][cleaner]
    items = _tasks(rows, cleaner, next_monday, next_monday + dt.timedelta(days=6), exclude_phk=True)
    return [f"{name} ơi, e gửi lịch tuần sau ạ:", ""] + (_by_day(items, with_fee=False) or ["(tuần sau chưa có lịch)"])


def build(rows, payments, cutoff: dt.date, cfg: dict, generated: str) -> dict:
    """Returns {'rows': sheet rows for the sunday_messages tab, 'summaries': [...], 'ready_line': str}."""
    next_monday = monday_of(cutoff) + dt.timedelta(days=7)
    out = [["SUNDAY MESSAGES", f"generated {generated}", "Copy each block's column-A rows into Zalo."], []]
    sums = []
    for c in cfg["names"]:
        s = pay_summary(rows, payments, c, cutoff, cfg)
        sums.append(s)
        for title, msg in ((f"{cfg['names'][c]}: tiền tuần ({vnd(s['owed'])})", pay_message(s, cfg)),
                           (f"{cfg['names'][c]}: lịch tuần sau", schedule_message(rows, c, next_monday, cfg))):
            first = len(out) + 2
            out.append([f"▼ {title} — copy A{first}:A{first + len(msg) - 1}"])
            out += [[line] for line in msg]
            out.append([])
    out.append(["After paying, record it in the `payments` tab (Date, Cleaner, Amount VND, Memo) or tell Claude."])
    ready = "SUNDAY_MESSAGES_READY: " + ", ".join(f"{s['cleaner']} {s['owed']:,}" for s in sums)
    return {"rows": out, "summaries": sums, "ready_line": ready}
