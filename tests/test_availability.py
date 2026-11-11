from datetime import date

import pytest

from hq.availability import UNKNOWN, build
from hq.feeds import FeedResult, Listing, parse_ics

ICS_2BR = """BEGIN:VCALENDAR
BEGIN:VEVENT
DTSTART;VALUE=DATE:20261107
DTEND;VALUE=DATE:20261118
SUMMARY:Reserved
DESCRIPTION:Reservation URL: https://www.airbnb.com/hosting/reservations/details/HM8KS9TT8S\\nPhone
END:VEVENT
BEGIN:VEVENT
DTSTART;VALUE=DATE:20261118
DTEND;VALUE=DATE:20261119
SUMMARY:Airbnb (Not available)
END:VEVENT
BEGIN:VEVENT
DTSTART;VALUE=DATE:20261120
DTEND;VALUE=DATE:20261128
SUMMARY:Reserved
DESCRIPTION:Reservation URL: https://www.airbnb.com/hosting/reservations/details/HMSYHWNAHT
END:VEVENT
BEGIN:VEVENT
DTSTART;VALUE=DATE:20261207
DTEND;VALUE=DATE:20261229
SUMMARY:Airbnb (Not available)
END:VEVENT
END:VCALENDAR
"""

ICS_1BR = """BEGIN:VCALENDAR
BEGIN:VEVENT
DTSTART;VALUE=DATE:20261118
DTEND;VALUE=DATE:20261119
SUMMARY:Reserved
DESCRIPTION:Reservation URL: https://www.airbnb.com/hosting/reservations/details/HMZY9BASPE
END:VEVENT
BEGIN:VEVENT
DTSTART;VALUE=DATE:20261207
DTEND;VALUE=DATE:20261229
SUMMARY:Reserved
DESCRIPTION:Reservation URL: https://www.airbnb.com/hosting/reservations/details/HMJWAX2YPB
END:VEVENT
END:VCALENDAR
"""

L2 = Listing("B1805_2BR", "1", "Sunrise B18.05", "2BR")
L1 = Listing("B1805_1BR", "2", "Sunrise B18.05", "1BR")


def test_parse_codes_and_kinds():
    evs = parse_ics(ICS_2BR)
    assert [e.code for e in evs] == ["HM8KS9TT8S", None, "HMSYHWNAHT", None]
    assert evs[1].kind == "blocked"
    assert evs[0].end == date(2026, 11, 18)  # checkout day, exclusive


def test_rejects_non_ical():
    with pytest.raises(ValueError):
        parse_ics("<html>login</html>")


def test_b1805_gaps_match_reality():
    results = [FeedResult(L2, True, parse_ics(ICS_2BR)), FeedResult(L1, True, parse_ics(ICS_1BR))]
    _, _, apt, gaps = build(results, date(2026, 11, 11), 40)
    by_start = {g.start: g for g in gaps if g.apartment == "Sunrise B18.05"}
    assert by_start[date(2026, 11, 19)].nights == 1          # orphan night
    assert by_start[date(2026, 11, 28)].nights == 9          # Nov 28 - Dec 7, not 2
    assert apt["Sunrise B18.05"][date(2026, 11, 18)] == "HMZY9BASPE"


def test_failed_feed_is_unknown_not_open():
    results = [FeedResult(L2, True, parse_ics(ICS_2BR)), FeedResult(L1, False, error="URLError")]
    _, _, apt, gaps = build(results, date(2026, 11, 11), 40)
    assert apt["Sunrise B18.05"][date(2026, 11, 19)] == UNKNOWN
    assert not any(g.apartment == "Sunrise B18.05" for g in gaps)
