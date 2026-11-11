"""Item 1: fetch the Airbnb iCal feeds directly (no Google Calendar import in the middle).

feeds config (JSON, kept OUTSIDE the repo; urls come from env vars / Secret Manager, never inline):
  {"feeds": [{"feed": "keqo", "apt": "Rivergate B9.20", "config": "1BR", "url_env": "ICAL_URL_KEQO"}, ...]}
A feed that fails to download or does not look like an iCalendar is reported in `failed`, and every
apartment it belongs to is dropped from `covered_apts`, so a failed poll can never look like
"all bookings vanished".
"""
from __future__ import annotations

import json
import os
from typing import Callable, Optional

from .ics import parse_ics


def default_fetch(url: str) -> str:  # pragma: no cover - network
    import requests
    r = requests.get(url, timeout=30, headers={"User-Agent": "airbnb-hq-cleaning-sync"})
    r.raise_for_status()
    return r.text


def poll(feeds_config: dict, fetch: Optional[Callable[[str], str]] = None, env=os.environ):
    fetch = fetch or default_fetch
    bookings, failed, apts, bad_apts = [], [], set(), set()
    for fd in feeds_config["feeds"]:
        apts.add(fd["apt"])
        try:
            url = env.get(fd["url_env"], "")
            if not url:
                raise RuntimeError(f"env {fd['url_env']} not set")
            text = fetch(url)
            if "BEGIN:VCALENDAR" not in text:
                raise RuntimeError("response is not an iCalendar")
            bookings += parse_ics(text, fd["apt"], fd.get("config"), fd["feed"])
        except Exception as e:  # noqa: BLE001 - reason is logged, URL (token) is not
            failed.append({"feed": fd["feed"], "apt": fd["apt"], "error": type(e).__name__ + ": " + str(e)[:120]})
            bad_apts.add(fd["apt"])
    return {"bookings": bookings, "failed": failed, "covered_apts": apts - bad_apts}


def load_feeds(path: str) -> dict:
    with open(path) as f:
        return json.load(f)
