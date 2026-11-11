# airbnb-hq

Automation for Cong's Airbnb HCMC business: calendar feeds, live availability, cleaning sync.
Secrets (iCal URLs, service-account keys) live in Google Secret Manager, never in this repo.

## Live Availability (Phase 1)
Rebuilds the tracker tab **Live Availability** from the 13 Airbnb iCal feeds: per-listing nights for 60 days,
apartment-level nights (1BR+2BR combined) and gaps (orphan nights, short gaps). A failed feed shows UNKNOWN,
never OPEN. Writes only that tab, only on allow-listed spreadsheets, with readback.

Local test: `pip install pytest && python -m pytest -q`
Dry run (no write): `python -m jobs.live_availability --dry-run`

Deploy (gcloud logged in):
1. `PROJECT=… SA=… scripts/create_feed_secrets.sh`  (paste each iCal URL; hidden)
2. `PROJECT=… REGION=… SA=… SPREADSHEET_ID=<scratch copy> scripts/deploy_live_availability.sh`
3. Run once, check the tab, then redeploy with the production SPREADSHEET_ID.
4. `scripts/schedule_live_availability.sh`, then resume the scheduler.

The worker service account needs Editor access on the target spreadsheet.
Two listing IDs in config/listings.json are UNCONFIRMED (probably PHK); confirm or remove.
