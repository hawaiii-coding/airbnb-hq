#!/usr/bin/env bash
# Add or replace ONE calendar link in the single secret `ical-urls` (JSON {feed: url}).
# Usage (Cloud Shell):  bash scripts/add_ical_url.sh <feed-id-from-config/units.json>
# The link is read hidden from the keyboard and never printed, logged or written to the repo.
# Get it from Airbnb > Calendar > Availability > Connect calendars > Export calendar.
set -euo pipefail
FEED="${1:?feed id, e.g. b341}"; PROJECT="${PROJECT:-airbnb-business-hq}"
read -r -s -p "Paste the iCal link for ${FEED}: " U; echo
case "$U" in https://www.airbnb.com/calendar/ical/*) ;; *) echo "That does not look like an Airbnb iCal link; nothing changed." >&2; exit 1;; esac
T=$(mktemp); trap 'rm -f "$T"' EXIT
gcloud secrets versions access latest --secret=ical-urls --project "$PROJECT" > "$T"
U="$U" FEED="$FEED" python3 -c "import json,os,sys; p=sys.argv[1]; d=json.load(open(p)); d[os.environ['FEED']]=os.environ['U']; json.dump(d,open(p,'w'))" "$T"
gcloud secrets versions add ical-urls --project "$PROJECT" --data-file="$T" >/dev/null
echo "Saved link for ${FEED}. Feeds in ical-urls: $(python3 -c "import json,sys;print(len(json.load(open(sys.argv[1]))))" "$T")"
