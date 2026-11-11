"""Cleaning sync safety tests (rebuilt 2026-11-11; the original 48 were lost).

Key rules (BUSINESS_RULES 4-5, handoff 2026-11-11):
  * a booking missing from the feed never cancels on its own
  * a failed feed is never read as "all bookings cancelled"
  * owner columns (Cleaner, Fee VND, Status, Confirmed at, Paid at) are never overwritten by sync
  * fees and cleaners come from rate_card.json
  * auto-cancel only when feed (2 consecutive polls) AND Upcoming earnings ledger both say gone
"""
import datetime as dt
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "cleaning"))

from cleaning_sync import poller, writer  # noqa: E402
from cleaning_sync.gsheets import GoogleSheetsClient, WriteNotAllowed  # noqa: E402
from cleaning_sync.ledger import LedgerEntry, LedgerView  # noqa: E402
from cleaning_sync.planner import Booking, RateCard, Task, plan  # noqa: E402

RC = RateCard.load(os.path.join(os.path.dirname(__file__), "..", "cleaning", "rate_card.json"))
TODAY = dt.date(2026, 11, 11)
HORIZON = TODAY + dt.timedelta(days=45)
APT = "Sunrise B6.12A"
CODE = "HMAAAA1111"
H = writer.HEADER


def task(code=CODE, date=dt.date(2026, 11, 20), apt=APT, status="PLANNED", fee=80000, cleaner="Thuy",
         config="Studio", kind="CHECKOUT", key=None):
    return Task(key or f"AUTO|{apt}|{code}", date, apt, cleaner, config, fee, status, f"AIRBNB|{code}", "",
                clean_kind=kind)


def row_of(t: Task):
    r = [""] * len(H)
    for name, v in (("Key", t.key), ("Date", t.date.isoformat()), ("Apartment", t.apt), ("Cleaner", t.cleaner),
                    ("Type", t.config), ("Kind", t.clean_kind), ("Fee VND", str(t.fee)), ("Status", t.status),
                    ("Confirmed at", "2026-11-10"), ("Paid at", ""), ("Source ID", t.source_id)):
        r[H.index(name)] = v
    return r


def ledger(gone=False, live=False, code=CODE):
    e = LedgerEntry(code=code, apt=APT, live=live, gone=gone)
    return LedgerView(entries={code: e}, latest_import=dt.datetime(2026, 11, 11, 1))


def ops_of(res, name):
    return [o for o in res["ops"] if o["op"] == name]


def run_plan(tasks, bookings=(), covered=None, miss=None, led=None):
    return plan(tasks, bookings, RC, TODAY, HORIZON, miss_counts=miss or {}, grace_polls=4,
                covered_apts={APT} if covered is None else covered, ledger=led)


class FakeSheet:
    def __init__(self, rows):
        self.rows = [list(r) for r in rows]
        self.backups, self.logs = [], []

    def read_rows(self):
        return [list(r) for r in self.rows]

    def write_cells(self, cells):
        for i, c, v in cells:
            self.rows[i][c] = v

    def append_rows(self, rows):
        start = len(self.rows)
        self.rows += [list(r) for r in rows]
        return list(range(start, len(self.rows)))

    def clear_rows(self, idx):
        for i in idx:
            self.rows[i] = [""] * len(H)

    def backup(self, label, rows):
        self.backups.append(label)
        return label

    def log(self, e):
        self.logs.append(e)


# ------------------------------------------------------------------ item 5: key safety tests
def test_missing_feed_alone_never_cancels():
    t = task()
    for miss in (0, 1, 5, 50):
        res = run_plan([t], miss={t.key: miss})
        assert not ops_of(res, "AUTO_CANCEL")
        assert all(o["tier"] != "auto_safe" for o in res["ops"]
                   if o.get("key") == t.key and not o["op"].startswith("TIDY_"))
    # and the writer applies nothing for it without approval
    sheet = FakeSheet([row_of(t)])
    writer.commit(sheet, run_plan([t], miss={t.key: 50})["ops"], (), TODAY, dry_run=False)
    assert sheet.rows[0][H.index("Status")] == "PLANNED"


def test_ledger_alone_never_cancels():
    t = task()
    b = Booking(CODE, APT, dt.date(2026, 11, 17), t.date, "RESERVED")
    res = run_plan([t], [b], led=ledger(gone=True))
    assert not ops_of(res, "AUTO_CANCEL") and not ops_of(res, "PROPOSE_CANCEL")


def test_failed_feed_is_not_cancelled():
    t = task()

    def boom(url):
        raise ConnectionError("down")

    polled = poller.poll({"feeds": [{"feed": "a8uo", "apt": APT, "url_env": "X"}]}, fetch=boom, env={"X": "u"})
    assert polled["failed"] and APT not in polled["covered_apts"]
    res = run_plan([t], polled["bookings"], covered=polled["covered_apts"], miss={t.key: 9}, led=ledger(gone=True))
    assert not [o for o in res["ops"] if o.get("key") == t.key and o["op"] in
                ("AUTO_CANCEL", "PROPOSE_CANCEL", "WATCH_MISSING")]


def test_owner_columns_never_overwritten():
    owner = {"Cleaner", "Fee VND", "Status", "Confirmed at", "Paid at"}
    for kind, cols in writer.ALLOWED_COLUMNS.items():
        if kind == "AUTO_CANCEL":
            assert cols == {"Status", "Notes"}          # the one owner-column exception, PLANNED -> CANCELLED only
            continue
        if kind in writer.AUTO_OPS:
            assert not cols & owner, kind
    assert {"Cleaner", "Confirmed at", "Paid at"} <= writer.NEVER
    # auto-cancel refuses CONFIRMED/COMPLETED rows
    for st in ("CONFIRMED", "COMPLETED"):
        t = task(status=st)
        sheet = FakeSheet([row_of(t)])
        op = {"op": "AUTO_CANCEL", "tier": "auto_safe", "key": t.key, "apt": APT, "date": t.date.isoformat(),
              "detail": "x"}
        muts, skipped = writer.build_mutations([op], sheet.read_rows(), set(), TODAY)
        assert not muts and skipped


def test_fees_and_cleaner_from_rate_card():
    b = Booking("HMBBBB2222", "Sunrise B9.28", dt.date(2026, 11, 14), dt.date(2026, 11, 16), "RESERVED",
                config="1BR")
    res = run_plan([], [b], covered={"Sunrise B9.28"})
    row = ops_of(res, "ADD")[0]["row"]
    assert (row["fee"], row["cleaner"], row["config"]) == (80000, "Thuy", "Studio")
    b2 = Booking("HMCCCC3333", "Sunrise B18.05", dt.date(2026, 11, 14), dt.date(2026, 11, 16), "RESERVED",
                 config="2BR")
    row2 = ops_of(run_plan([], [b2], covered={"Sunrise B18.05"}), "ADD")[0]["row"]
    assert (row2["fee"], row2["cleaner"]) == (200000, "Linh")
    bad = task(fee=120000)
    assert ops_of(run_plan([bad], [Booking(CODE, APT, None, bad.date, "RESERVED")]), "FEE_MISMATCH")


# ------------------------------------------------------------------ item 1: auto-cancel when both agree
def test_auto_cancel_needs_two_polls_and_ledger():
    t = task()
    first = run_plan([t], miss={}, led=ledger(gone=True))           # first miss
    assert not ops_of(first, "AUTO_CANCEL")
    second = run_plan([t], miss=first["miss_counts"], led=ledger(gone=True))
    ac = ops_of(second, "AUTO_CANCEL")
    assert len(ac) == 1 and ac[0]["tier"] == "auto_safe"
    sheet = FakeSheet([row_of(t)])
    rep = writer.commit(sheet, second["ops"], (), TODAY, dry_run=False)
    assert rep["applied"]
    r = sheet.rows[0]
    assert r[H.index("Status")] == "CANCELLED"
    assert "feed" in r[H.index("Notes")] and "earnings" in r[H.index("Notes")]
    assert r[H.index("Cleaner")] == "Thuy" and r[H.index("Fee VND")] == "80000"


def test_no_auto_cancel_when_ledger_live_unknown_or_unusable():
    t = task()
    for led in (ledger(live=True), ledger(gone=False), LedgerView.unavailable("stale"), None,
                LedgerView(entries={}, latest_import=dt.datetime(2026, 11, 11))):
        assert not ops_of(run_plan([t], miss={t.key: 3}, led=led), "AUTO_CANCEL")


def test_auto_cancel_only_planned():
    t = task(status="CONFIRMED")
    res = run_plan([t], miss={t.key: 3}, led=ledger(gone=True))
    assert not ops_of(res, "AUTO_CANCEL") and ops_of(res, "PROPOSE_CANCEL")


def test_auto_cancel_skips_manual_rows():
    t = task(key=f"MANUAL|TRIPCOM|{APT}|2026-11-20")
    assert not ops_of(run_plan([t], miss={t.key: 3}, led=ledger(gone=True)), "AUTO_CANCEL")


def test_auto_cancel_cap_per_run():
    ts = [task(code=f"HMZZZZ000{i}") for i in range(5)]
    led = LedgerView(entries={t.code: LedgerEntry(code=t.code, apt=APT, gone=True) for t in ts},
                     latest_import=dt.datetime(2026, 11, 11))
    res = run_plan(ts, miss={t.key: 3 for t in ts}, led=led)
    muts, skipped = writer.build_mutations(res["ops"], [row_of(t) for t in ts], set(), TODAY)
    assert not [m for m in muts if m["kind"] == "AUTO_CANCEL"]
    assert any("too many" in s[1] for s in skipped)


# ------------------------------------------------------------------ item 2: horizon
def test_default_horizon_45():
    from cleaning_sync import job
    import argparse
    assert job.DEFAULT_HORIZON_DAYS == 45
    b = Booking("HMDDDD4444", APT, dt.date(2026, 12, 20), dt.date(2026, 12, 24), "RESERVED")  # day 43
    assert ops_of(run_plan([], [b]), "ADD")


# ------------------------------------------------------------------ item 3: mismatch report
def test_mismatch_report_categories():
    from cleaning_sync.planner import mismatch_report
    dup1 = task(code="HMEEEE5555", date=dt.date(2026, 11, 25))
    dup2 = task(code="HMFFFF6666", date=dt.date(2026, 11, 25), key=f"DIRECT|{APT}|2026-11-25")
    orphan = task(code="HMGGGG7777", date=dt.date(2026, 11, 22))
    badfee = task(code="HMHHHH8888", date=dt.date(2026, 11, 28), fee=100000)
    bookings = [Booking("HMIIII9999", APT, dt.date(2026, 11, 26), dt.date(2026, 11, 27), "RESERVED"),
                Booking("HMEEEE5555", APT, None, dup1.date, "RESERVED"),
                Booking("HMHHHH8888", APT, None, badfee.date, "RESERVED")]
    res = run_plan([dup1, dup2, orphan, badfee], bookings)
    rows = mismatch_report(res["ops"], applied_ids=set(), generated=dt.datetime(2026, 11, 11, 8))
    cats = {r[1] for r in rows[2:]}
    assert {"Booking without clean", "Clean without booking", "Fee vs rate card", "Duplicate same-date clean"} <= cats
    assert rows[1][0] == "Generated"
    # an ADD that was applied is no longer a "booking without clean"
    add_id = writer.op_id(ops_of(res, "ADD")[0])
    rows2 = mismatch_report(res["ops"], applied_ids={add_id}, generated=dt.datetime(2026, 11, 11, 8))
    assert "Booking without clean" not in {r[1] for r in rows2[2:]}


def test_report_write_is_guarded():
    c = GoogleSheetsClient("scratch", service=None, allowed_ids={"other"})
    with pytest.raises(WriteNotAllowed):
        c.write_report([["x"]])
    c2 = GoogleSheetsClient("scratch", service=None, allowed_ids={"scratch"})
    with pytest.raises(WriteNotAllowed):
        c2.write_report([["x"]], tab="tasks")


# ------------------------------------------------------------------ item 4: data tidy
def test_notes_use_iso_dates_not_serials():
    t = task()
    r = row_of(t)
    r[H.index("Date")] = "46346"   # Sheets serial for 2026-11-20
    op = {"op": "MOVE_DATE", "tier": "needs_owner", "key": t.key, "apt": APT, "date": "2026-11-20",
          "new_date": "2026-11-21", "detail": "x"}
    muts, _ = writer.build_mutations([op], [r], {writer.op_id(op)}, TODAY)
    note = muts[0]["cells"]["Notes"]
    assert "46346" not in note and "2026-11-20 -> 2026-11-21" in note


def test_alias_type_label_tidied_to_rate_card():
    t = task(apt="Sunrise B9.28", config="1BR", key="AUTO|Sunrise B9.28|HMJJJJ0000", code="HMJJJJ0000")
    b = Booking("HMJJJJ0000", "Sunrise B9.28", None, t.date, "RESERVED")
    res = run_plan([t], [b], covered={"Sunrise B9.28"})
    tidy = ops_of(res, "TIDY_TYPE")
    assert tidy and tidy[0]["tier"] == "auto_safe" and tidy[0]["new_type"] == "Studio"
    sheet = FakeSheet([row_of(t)])
    writer.commit(sheet, res["ops"], (), TODAY, dry_run=False)
    assert sheet.rows[0][H.index("Type")] == "Studio"
    assert sheet.rows[0][H.index("Fee VND")] == "80000"


def test_feeds_config_defaults_to_shipped_mapping():
    from cleaning_sync import job
    p = job.feeds_config_path({})
    feeds = poller.load_feeds(p)["feeds"]
    assert len(feeds) == 11 and all("url" not in f for f in feeds)
    assert job.feeds_config_path({"FEEDS_CONFIG": "/x.json"}) == "/x.json"


def test_report_drops_adds_another_run_already_wrote():
    from cleaning_sync import job
    from cleaning_sync.planner import mismatch_report
    b = Booking("HMKKKK1234", APT, dt.date(2026, 12, 10), dt.date(2026, 12, 14), "RESERVED")
    ops = run_plan([], [b])["ops"]
    add = ops_of({"ops": ops}, "ADD")[0]
    existing = [""] * len(H)
    existing[0] = add["key"]
    sheet = FakeSheet([existing])                       # the overlapping run already added it
    rep = writer.commit(sheet, ops, (), TODAY, dry_run=False)
    assert not rep["applied"] and rep["skipped"]
    rows = mismatch_report(ops, job.resolved_ids(rep), dt.datetime(2026, 11, 11))
    assert "Booking without clean" not in {r[1] for r in rows[2:]}


def test_cloudbuild_prod_secrets_cover_every_feed():
    import json, yaml
    root = os.path.join(os.path.dirname(__file__), "..")
    cb = yaml.safe_load(open(os.path.join(root, "cloudbuild.yaml")))
    got = set(cb["substitutions"]["_CLEAN_SECRETS"].split(","))
    feeds = json.load(open(os.path.join(root, "cleaning", "feeds.example.json")))["feeds"]
    assert got == {f"{f['url_env']}=ical-url-{f['feed']}:latest" for f in feeds}
    assert cb["substitutions"]["_CLEAN_SCRATCH_SHEET"] != cb["substitutions"]["_CLEAN_PROD_SHEET"]


def test_plan_report_rows_shape():
    from cleaning_sync import job
    rep = {"today": "2026-11-11", "mode": "plan", "feeds_failed": [{"feed": "a8uo", "apt": APT, "error": "x"}],
           "ledger": {"usable": True, "codes": 3, "latest_import": "t"},
           "ops": [{"tier": "info", "op": "WATCH_MISSING", "date": "2026-11-20", "apt": APT, "key": "k", "detail": "d"}]}
    rows = job.plan_report_rows(rep)
    assert rows[2][0] == "FEED_FAILED" and rows[3][1] == "WATCH_MISSING" and all(len(r) == 6 for r in rows)


def test_display_names_and_1pn_2pn():
    assert RC.display("Lavida B1.1709", "1BR") == "Lavida B1.1709 (1PN)"
    assert RC.display("Lavida B1.1709", "2BR") == "Lavida B1.1709 (2PN)"
    assert RC.display("Sunrise B18.05", "2BR") == "Q7-B18.05 (2PN)"
    assert RC.display("Lavida B1.1319", None) == "Lavida B1.1319"
    assert RC.display("Sunrise B9.28", "1BR") == "Q7-B9.28"          # alias -> Studio, single config: no suffix
    b = Booking("HMLLLL0000", "Lavida B1.1709", dt.date(2026, 11, 11), dt.date(2026, 11, 13), "RESERVED", config="1BR")
    row = ops_of(run_plan([], [b], covered={"Lavida B1.1709"}), "ADD")[0]["row"]
    assert row["display"] == "Lavida B1.1709 (1PN)"
    muts, _ = writer.build_mutations(run_plan([], [b], covered={"Lavida B1.1709"})["ops"], [], set(), TODAY)
    assert muts[0]["row"][H.index("Display")] == "Lavida B1.1709 (1PN)"


def test_tidy_display_only_bare_names():
    apt = "Lavida B1.1709"
    t = task(apt=apt, config="1BR", fee=120000, key=f"AUTO|{apt}|HMMMMM0000", code="HMMMMM0000",
             date=dt.date(2026, 11, 13))
    t.display = apt
    b = Booking("HMMMMM0000", apt, None, t.date, "RESERVED", config="1BR")
    res = run_plan([t], [b], covered={apt})
    td = ops_of(res, "TIDY_DISPLAY")
    assert td and td[0]["new_display"] == "Lavida B1.1709 (1PN)"
    r = row_of(t)
    r[H.index("Display")] = apt
    sheet = FakeSheet([r])
    writer.commit(sheet, res["ops"], (), TODAY, dry_run=False)
    assert sheet.rows[0][H.index("Display")] == "Lavida B1.1709 (1PN)"
    assert sheet.rows[0][H.index("Status")] == "PLANNED" and sheet.rows[0][H.index("Fee VND")] == "120000"
    t.display = "my own name"                                       # owner-typed display is never touched
    assert not ops_of(run_plan([t], [b], covered={apt}), "TIDY_DISPLAY")


def test_alerts_flag_failures_only():
    from cleaning_sync import job
    ok = {"feeds_failed": [], "ledger": {"usable": True}, "commit": {"applied": True}, "mismatch_report": {"written": True}}
    assert job.alerts(ok) == []
    bad = {"feeds_failed": [{"feed": "a8uo", "apt": APT, "error": "boom"}], "ledger": {"usable": False, "reason": "stale"},
           "commit": {"rolled_back": True, "error": "x"}, "mismatch_report": {"error": "y"}}
    assert len(job.alerts(bad)) == 4


def test_hcmc_time_is_utc_plus_7():
    from cleaning_sync import job
    diff = job.now_hcmc() - dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    assert abs(diff - dt.timedelta(hours=7)) < dt.timedelta(minutes=1)



# ------------------------------------------------------------------ Sunday payroll + schedule
def _wrow(key, date, apt, disp, cleaner, fee, status="COMPLETED", kind="CHECKOUT", time=""):
    r = [""] * len(H)
    for n, v in (("Key", key), ("Date", date), ("Apartment", apt), ("Display", disp), ("Cleaner", cleaner),
                 ("Fee VND", str(fee)), ("Status", status), ("Kind", kind), ("Time", time)):
        r[H.index(n)] = v
    return r


WCFG = {"start": "2026-11-09", "opening": {"Thuy": 80000, "Linh": 0}, "bonus": {"Thuy": 500000},
        "bonus_settled": {"Thuy": ["2026-11"]}, "names": {"Thuy": "Chị Thuý", "Linh": "Chị Linh"}}
WROWS = [
    _wrow("old", "2026-11-07", APT, "Q7-B6.12A", "Thuy", 240000),                         # before start: settled
    _wrow("a", "2026-11-09", "Lavida B1.1709", "Lavida B1.1709 (2PN)", "Thuy", 180000),
    _wrow("b", "46335", "Sunrise B20.10", "Q7-B20.10", "Thuy", 80000),                      # serial date = Nov 9
    _wrow("c", "2026-11-10", "Sunrise B14.20", "Q7-B14.20", "Thuy", 80000, kind="STAYOVER", time="10:00"),
    _wrow("d", "2026-11-14", "PHK", "Q2-2PN", "Thuy", 180000, status="CANCELLED"),
    _wrow("e", "2026-11-15", APT, "Q7-B6.12A", "Thuy", 80000, status="PLANNED"),            # Sunday, still PLANNED
    _wrow("f", "2026-11-09", "Rivergate B9.20", "B9.20", "Linh", 135000),
    _wrow("g", "2026-11-17", "Rivergate B9.20", "B9.20", "Linh", 135000, status="PLANNED"),  # next week
    _wrow("h", "2026-11-18", "Sunrise B14.20", "Q7-B14.20", "Thuy", 80000, status="PLANNED"),
    _wrow("i", "2026-11-19", "PHK", "Q2-1PN", "Thuy", 120000, status="PLANNED"),             # PHK left out of schedule
]


def test_weekly_pay_totals_and_carry():
    from cleaning_sync import weekly
    s = weekly.pay_summary(WROWS, [], "Thuy", dt.date(2026, 11, 15), WCFG)
    assert s["week_total"] == 180000 + 80000 + 80000 + 80000      # cancelled PHK out, PLANNED Sunday in
    assert s["owed"] == 80000 + 420000 and s["carry"] == 80000
    s2 = weekly.pay_summary(WROWS, [["2026-11-16", "Thuy", "500000", "Don nha tuan 2 T11"]], "Thuy",
                            dt.date(2026, 11, 22), WCFG)
    assert s2["owed"] == 80000 + 420000 + 80000 + 120000 - 500000
    assert weekly.pay_summary(WROWS, [], "Linh", dt.date(2026, 11, 15), WCFG)["owed"] == 135000


def test_weekly_bonus_last_sunday_only_unsettled_months():
    from cleaning_sync import weekly
    assert weekly.last_sunday_of_month(dt.date(2026, 11, 29)) and not weekly.last_sunday_of_month(dt.date(2026, 11, 22))
    assert weekly.pay_summary(WROWS, [], "Thuy", dt.date(2026, 11, 29), WCFG)["bonus_now"] == 0   # Nov settled
    dec = weekly.pay_summary(WROWS, [], "Thuy", dt.date(2026, 12, 27), WCFG)
    assert dec["bonus_now"] == 500000
    assert any("Thưởng tháng 12: 500.000đ" in l for l in weekly.pay_message(dec, WCFG))


def test_weekly_messages_format():
    from cleaning_sync import weekly
    s = weekly.pay_summary(WROWS, [], "Thuy", dt.date(2026, 11, 15), WCFG)
    msg = weekly.pay_message(s, WCFG)
    assert msg[0] == "Chị Thuý ơi, e gửi tiền dọn nhà tuần 09/11 - 15/11 ạ:"
    assert "T2 09/11: Lavida B1.1709 (2PN) 180.000đ, Q7-B20.10 80.000đ" in msg
    assert "T3 10/11: Q7-B14.20 - 10:00 - dọn lưu 80.000đ" in msg
    assert "Các tuần trước còn: 80.000đ" in msg and "Tổng: 500.000đ" in msg
    assert msg[-1] == "Nội dung CK: Don nha tuan 2 T11"
    sched = weekly.schedule_message(WROWS, "Thuy", dt.date(2026, 11, 16), WCFG)
    assert sched[2:] == ["T4 18/11: Q7-B14.20"]                      # PHK excluded
    assert weekly.week_memo(dt.date(2026, 11, 2)) == "Don nha tuan 1 T11"


def test_weekly_build_copy_ranges_match():
    from cleaning_sync import weekly
    res = weekly.build(WROWS, [], dt.date(2026, 11, 15), WCFG, "t")
    rows = res["rows"]
    for i, r in enumerate(rows):
        if r and r[0].startswith("▼"):
            rng = r[0].split("copy ")[1]
            a, b = (int(x[1:]) for x in rng.split(":"))
            assert a == i + 2 and rows[a - 1][0] and rows[b - 1][0] and (b == len(rows) or not rows[b])
    assert res["ready_line"] == "SUNDAY_MESSAGES_READY: Thuy 500,000, Linh 135,000"
