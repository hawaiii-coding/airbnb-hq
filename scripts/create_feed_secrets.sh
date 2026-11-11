#!/usr/bin/env bash
# Prompts (hidden) for each listing's Airbnb iCal URL and stores it in Secret Manager
# as ical-url-<key>. URLs are never echoed or written to disk.
# Get each URL from Airbnb > Listing > Availability > Sync calendars > Export calendar.
set -euo pipefail
: "${PROJECT:?set PROJECT}"; : "${SA:?set SA (service account email of the worker)}"
for key in $(python3 -c "import json;print(' '.join(l['key'] for l in json.load(open('config/listings.json'))['listings']))"); do
  read -r -s -p "iCal URL for ${key} (Enter to skip): " url; echo
  [ -z "$url" ] && continue
  name="ical-url-$(echo "$key" | tr 'A-Z_' 'a-z-')"
  gcloud secrets describe "$name" --project "$PROJECT" >/dev/null 2>&1 || \
    gcloud secrets create "$name" --project "$PROJECT" --replication-policy automatic
  printf '%s' "$url" | gcloud secrets versions add "$name" --project "$PROJECT" --data-file=-
  gcloud secrets add-iam-policy-binding "$name" --project "$PROJECT" \
    --member "serviceAccount:${SA}" --role roles/secretmanager.secretAccessor >/dev/null
done
