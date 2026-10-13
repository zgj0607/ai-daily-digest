from __future__ import annotations

import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

MONTHS = {
    "jan": 1,
    "january": 1,
    "feb": 2,
    "february": 2,
    "mar": 3,
    "march": 3,
    "apr": 4,
    "april": 4,
    "may": 5,
    "jun": 6,
    "june": 6,
    "jul": 7,
    "july": 7,
    "aug": 8,
    "august": 8,
    "sep": 9,
    "sept": 9,
    "september": 9,
    "oct": 10,
    "october": 10,
    "nov": 11,
    "november": 11,
    "dec": 12,
    "december": 12,
}

_MONTH_DATE = re.compile(
    r"\b([A-Za-z]+)\s+(\d{1,2}),?\s+(\d{4})\b"
)
_ISO_DATE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")


def parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    text = value.strip()
    if not text:
        return None
    iso = _from_iso(text)
    if iso:
        return iso
    try:
        parsed = parsedate_to_datetime(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError, IndexError, OverflowError):
        pass
    match = _MONTH_DATE.search(text)
    if match:
        month = MONTHS.get(match.group(1).lower())
        if month:
            return datetime(int(match.group(3)), month, int(match.group(2)), tzinfo=timezone.utc)
    match = _ISO_DATE.search(text)
    if match:
        return datetime(int(match.group(1)), int(match.group(2)), int(match.group(3)), tzinfo=timezone.utc)
    return None


def _from_iso(text: str) -> datetime | None:
    candidate = text.replace("Z", "+00:00")
    if len(candidate) >= 19 and candidate[10] == " ":
        candidate = candidate[:10] + "T" + candidate[11:]
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def to_iso(value: datetime | None) -> str:
    if value is None:
        return ""
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def file_stamp(value: datetime | None) -> str:
    if value is None:
        return datetime.now(timezone.utc).strftime("%Y%m%d")
    return value.astimezone(timezone.utc).strftime("%Y%m%d")


def date_only(value: datetime | None) -> str:
    if value is None:
        return ""
    return value.astimezone(timezone.utc).date().isoformat()
