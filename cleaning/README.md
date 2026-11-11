# cleaning-sync (recovered 2026-11-11)

Recovered from the deployed Cloud Run image of `cleaning-sync-scratch` (built in an earlier Claude session whose
workspace was lost). Source files are byte-for-byte what was running. Lost and NOT recovered: the 48 unit tests,
fixtures, and the deploy/schedule scripts. Design and rules: project docs CLEANING_SYNC_PLAN / STATUS / AUDIT and BUSINESS_RULES.

Live job (2026-11-11): `cleaning-sync-scratch`, `--mode apply`, writes ONLY the scratch copy of the Cleaning sheet
(1GxwOX…), reads the Master Financial Tracker as a read-only ledger. Feed URLs come from Secret Manager (ical-url-*).

Before production: rebuild tests, pin requirements, add a Cloud Build trigger, then a separate production job gated by
ALLOW_PRODUCTION_WRITES=true with owner approval.

## 2026-11-11 upgrade (scratch only)
- Tests rebuilt: `tests/test_cleaning_sync.py` (run `python -m pytest -q` from the repo root).
- AUTO_CANCEL: PLANNED Airbnb rows only, when the booking is missing from the feed for 2 consecutive polls AND the
  Upcoming earnings ledger marks it gone. One source alone never cancels. Failed feed or unusable ledger = no cancel.
  Max 3 per run; the reason is written in Notes.
- Horizon 45 days. Mismatch report rewritten to tab `mismatch_report` of the same (scratch) sheet on every apply run.
- Data tidy: dates written as ISO text; alias Type labels (B9.28/B20.10 "1BR") changed to the rate-card label.
- Cloud Build deploys `cleaning-sync-scratch` from `main` (image `cleaning-sync`).
