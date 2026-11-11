# cleaning-sync (recovered 2026-11-11)

Recovered from the deployed Cloud Run image of `cleaning-sync-scratch` (built in an earlier Claude session whose
workspace was lost). Source files are byte-for-byte what was running. Lost and NOT recovered: the 48 unit tests,
fixtures, and the deploy/schedule scripts. Design and rules: project docs CLEANING_SYNC_PLAN / STATUS / AUDIT and BUSINESS_RULES.

Live job (2026-11-11): `cleaning-sync-scratch`, `--mode apply`, writes ONLY the scratch copy of the Cleaning sheet
(1GxwOX…), reads the Master Financial Tracker as a read-only ledger. Feed URLs come from Secret Manager (ical-url-*).

Before production: rebuild tests, pin requirements, add a Cloud Build trigger, then a separate production job gated by
ALLOW_PRODUCTION_WRITES=true with owner approval.
