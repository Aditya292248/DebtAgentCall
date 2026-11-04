"""Which time zones might the debtor be in?

``zones_for(zip_code, phone_e164)`` asks two sources and returns every zone
either one gives:
  - the ZIP code, by its first 3 digits
  - the phone number, by its area code

People move and keep their phone number, so the two can disagree. When they do,
we keep both zones, and the calling-hours check has to pass in each of them.

An unknown ZIP or area code just adds no zone. If neither source gives a zone,
``zones_for`` returns ``None``, meaning "we don't know where this person is".

The tables below are small and hand-made: enough for the made-up debtors and a
few states in Eastern, Central, Mountain and Pacific time. A real data source
(a full ZIP and area-code database) comes later.
"""

from __future__ import annotations

EASTERN = "America/New_York"
CENTRAL = "America/Chicago"
MOUNTAIN = "America/Denver"
ARIZONA = "America/Phoenix"  # Mountain time, but no daylight saving
PACIFIC = "America/Los_Angeles"

# First 3 digits of a ZIP code -> time zone.
ZIP3_ZONES: dict[str, str] = {
    "021": EASTERN,  # Boston, MA
    "100": EASTERN,  # New York, NY
    "101": EASTERN,
    "102": EASTERN,
    "303": EASTERN,  # Atlanta, GA
    "331": EASTERN,  # Miami, FL
    "606": CENTRAL,  # Chicago, IL
    "750": CENTRAL,  # Dallas, TX
    "752": CENTRAL,
    "802": MOUNTAIN,  # Denver, CO
    "850": ARIZONA,  # Phoenix, AZ
    "900": PACIFIC,  # Los Angeles, CA
    "941": PACIFIC,  # San Francisco, CA
    "981": PACIFIC,  # Seattle, WA
}

# US area code -> time zone.
AREA_CODE_ZONES: dict[str, str] = {
    "212": EASTERN,  # New York, NY
    "646": EASTERN,
    "617": EASTERN,  # Boston, MA
    "404": EASTERN,  # Atlanta, GA
    "305": EASTERN,  # Miami, FL
    "312": CENTRAL,  # Chicago, IL
    "214": CENTRAL,  # Dallas, TX
    "303": MOUNTAIN,  # Denver, CO
    "720": MOUNTAIN,
    "602": ARIZONA,  # Phoenix, AZ
    "213": PACIFIC,  # Los Angeles, CA
    "310": PACIFIC,
    "415": PACIFIC,  # San Francisco, CA
    "206": PACIFIC,  # Seattle, WA
}


def _zip_zone(zip_code: str | None) -> str | None:
    digits = (zip_code or "").strip()
    if len(digits) < 5 or not digits[:5].isdigit():
        return None
    return ZIP3_ZONES.get(digits[:3])


def _area_code(phone_e164: str | None) -> str | None:
    """The area code of a US number in E.164 form, like ``+12135550101``."""
    phone = (phone_e164 or "").strip()
    if len(phone) != 12 or not phone.startswith("+1") or not phone[1:].isdigit():
        return None
    return phone[2:5]


def zones_for(zip_code: str | None, phone_e164: str | None) -> set[str] | None:
    """Every time zone the ZIP or the phone points to, or ``None`` if neither does."""
    zones = set()
    zip_zone = _zip_zone(zip_code)
    if zip_zone:
        zones.add(zip_zone)
    area = _area_code(phone_e164)
    if area and area in AREA_CODE_ZONES:
        zones.add(AREA_CODE_ZONES[area])
    return zones or None
