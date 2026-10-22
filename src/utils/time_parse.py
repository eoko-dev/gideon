"""Natural-language times and simple recurrence for reminders and events.

Times are parsed in the *user's* timezone (set with /me timezone), so
"tomorrow at 9am" means 9am where they are. Storage keeps the existing
convention: naive datetimes in the server-local timezone (see timeutil).

Recurrence is a small, predictable subset written as an RRULE string:
"every day", "every weekday", "every monday" (or "every mon and thu"),
"every week", "every month". The rule is applied to the first occurrence,
so its time of day carries over, and it's evaluated in the user's timezone
so the wall-clock time survives DST changes.
"""

import re
from datetime import datetime, timedelta
from typing import Optional, Tuple
from zoneinfo import ZoneInfo

import dateparser
from dateutil.rrule import rrulestr

from .timeutil import local_tz

_WEEKDAYS = {
    "monday": "MO", "mon": "MO", "tuesday": "TU", "tue": "TU", "tues": "TU",
    "wednesday": "WE", "wed": "WE", "thursday": "TH", "thu": "TH", "thur": "TH", "thurs": "TH",
    "friday": "FR", "fri": "FR", "saturday": "SA", "sat": "SA", "sunday": "SU", "sun": "SU",
}
_DAY_PATTERN = "|".join(sorted(_WEEKDAYS, key=len, reverse=True))
_RECURRENCE_RE = re.compile(
    rf"^\s*every\s+(?P<what>day|weekday|week|month|other\s+day|(?:(?:{_DAY_PATTERN})s?(?:\s*(?:,|and|&)\s*)?)+)"
    rf"(?P<rest>.*)$", re.IGNORECASE)
_MONTHS = ("jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec|january|february|march|april|june|july|"
           "august|september|october|november|december")


def normalize(text: str) -> str:
    """Rewrites phrasings dateparser misses into ones it handles."""
    text = (text or "").lower().strip()
    text = re.sub(r"\bat\b", " ", text)
    text = re.sub(r"\bon\b", " ", text)
    text = re.sub(rf"\bnext\s+({_DAY_PATTERN})\b", r"\1", text)
    text = text.replace("noon", "12:00").replace("midnight", "00:00")
    text = re.sub(r"\b(\d{1,2}(?::\d{2})?)\s*(am|pm)\b", r"\1\2", text)
    # "dec 3 9am" → "3 dec 9am" (day-first parses reliably)
    text = re.sub(rf"\b({_MONTHS})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?\b", r"\2 \1", text)
    return " ".join(text.split())


def parse_when(text: str, tz: ZoneInfo, now: Optional[datetime] = None) -> Optional[datetime]:
    """A future aware datetime for ``text`` in ``tz``, or None if it can't be parsed."""
    now = (now or datetime.now(tz)).astimezone(tz)
    cleaned = normalize(text)
    if not cleaned:
        return None
    settings = {
        "RELATIVE_BASE": now.replace(tzinfo=None),
        "PREFER_DATES_FROM": "future",
        "TIMEZONE": str(tz),
        "RETURN_AS_TIMEZONE_AWARE": True,
        "DATE_ORDER": "MDY",
    }
    parsed = dateparser.parse(cleaned, settings=settings)
    if parsed is None:
        return None
    parsed = parsed.astimezone(tz)
    has_time = bool(re.search(r"\d{1,2}(:\d{2})?(am|pm)|\d{1,2}:\d{2}|\bin\b|hour|minute|second", cleaned))
    if not has_time and parsed.hour == 0 and parsed.minute == 0:
        # A bare day ("friday") means the morning, not midnight
        parsed = parsed.replace(hour=9, minute=0)
    if parsed <= now:
        # "at 5pm" after 5pm means tomorrow
        if has_time and parsed.date() == now.date():
            parsed += timedelta(days=1)
        else:
            return None
    return parsed


def parse_recurrence(text: str) -> Tuple[Optional[str], str]:
    """Splits "every monday at 9am" into ("FREQ=WEEKLY;BYDAY=MO", "monday at 9am").

    Returns (None, text) when there's no recurrence. The second value is the
    time expression for the first occurrence.
    """
    match = _RECURRENCE_RE.match(text or "")
    if not match:
        return None, text
    what = match.group("what").lower().strip()
    rest = match.group("rest").strip()
    if what == "day":
        return "FREQ=DAILY", rest or "tomorrow 9am"
    if what.replace(" ", "") == "otherday":
        return "FREQ=DAILY;INTERVAL=2", rest or "tomorrow 9am"
    if what == "weekday":
        rule = "FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR"
        return rule, rest or "9am"
    if what == "week":
        return "FREQ=WEEKLY", rest or "in 7 days"
    if what == "month":
        return "FREQ=MONTHLY", rest or "in 1 month"
    days = []
    for word in re.findall(_DAY_PATTERN + r"s?", what):
        code = _WEEKDAYS.get(word.rstrip("s")) or _WEEKDAYS.get(word)
        if code and code not in days:
            days.append(code)
    if not days:
        return None, text
    # Only the time: first_occurrence() picks whichever listed day comes first
    return f"FREQ=WEEKLY;BYDAY={','.join(days)}", rest or "9am"


def first_occurrence(rule: str, candidate: datetime) -> datetime:
    """The first time ``rule`` fires at or after ``candidate``.

    ``candidate`` is the next occurrence of the time of day ("9am" → today or
    tomorrow at 9); the rule moves it to the first matching day, e.g. "every
    weekday" asked on a Saturday starts Monday.
    """
    tz = candidate.tzinfo
    local = candidate.replace(tzinfo=None)
    first = rrulestr(rule, dtstart=local).after(local - timedelta(seconds=1), inc=True)
    return (first or local).replace(tzinfo=tz)


def next_occurrence(rule: str, previous: datetime, tz: ZoneInfo, now: Optional[datetime] = None) -> Optional[datetime]:
    """The next time after ``now`` that ``rule`` fires, anchored at ``previous`` (aware)."""
    now = (now or datetime.now(tz)).astimezone(tz)
    anchor = previous.astimezone(tz).replace(tzinfo=None)
    after = rrulestr(rule, dtstart=anchor).after(now.replace(tzinfo=None))
    return after.replace(tzinfo=tz) if after else None


def describe_rule(rule: Optional[str]) -> str:
    """Short human description of a stored rule ("every weekday")."""
    if not rule:
        return ""
    names = {"MO": "Mon", "TU": "Tue", "WE": "Wed", "TH": "Thu", "FR": "Fri", "SA": "Sat", "SU": "Sun"}
    parts = dict(p.split("=", 1) for p in rule.split(";") if "=" in p)
    freq, byday, interval = parts.get("FREQ"), parts.get("BYDAY"), parts.get("INTERVAL")
    if freq == "DAILY":
        return "every other day" if interval == "2" else "every day"
    if freq == "WEEKLY" and byday:
        if byday == "MO,TU,WE,TH,FR":
            return "every weekday"
        return "every " + " & ".join(names.get(d, d) for d in byday.split(","))
    if freq == "WEEKLY":
        return "every week"
    if freq == "MONTHLY":
        return "every month"
    return "repeating"


def to_server_naive(aware: datetime) -> datetime:
    """Converts an aware datetime to the storage format (naive, server-local)."""
    return aware.astimezone(local_tz()).replace(tzinfo=None)


def from_server_naive(naive: datetime, tz: ZoneInfo) -> datetime:
    """A stored naive server-local datetime as an aware datetime in ``tz``."""
    return naive.replace(tzinfo=local_tz()).astimezone(tz)


def resolve_timezone(name: Optional[str]) -> Optional[ZoneInfo]:
    """ZoneInfo for an IANA name (case-insensitive), or None if unknown."""
    if not name:
        return None
    from zoneinfo import available_timezones
    wanted = name.strip().lower().replace(" ", "_")
    zones = available_timezones()
    # Canonical spelling first ("america/new_york" → "America/New_York"), then a city match
    for candidate in sorted(zones):
        if candidate.lower() == wanted:
            return ZoneInfo(candidate)
    for candidate in sorted(zones):
        if candidate.lower().endswith("/" + wanted):
            return ZoneInfo(candidate)
    return None
