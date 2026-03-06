"""Natural language date parsing for due dates."""

from __future__ import annotations

import re
from datetime import date, timedelta

_WEEKDAYS = {
    "mon": 0, "monday": 0,
    "tue": 1, "tuesday": 1,
    "wed": 2, "wednesday": 2,
    "thu": 3, "thursday": 3,
    "fri": 4, "friday": 4,
    "sat": 5, "saturday": 5,
    "sun": 6, "sunday": 6,
}

_MONTHS = {
    "jan": 1, "january": 1,
    "feb": 2, "february": 2,
    "mar": 3, "march": 3,
    "apr": 4, "april": 4,
    "may": 5,
    "jun": 6, "june": 6,
    "jul": 7, "july": 7,
    "aug": 8, "august": 8,
    "sep": 9, "september": 9,
    "oct": 10, "october": 10,
    "nov": 11, "november": 11,
    "dec": 12, "december": 12,
}


def parse_due_date(text: str, today: date | None = None) -> str | None:
    """Parse a natural language date string into YYYY-MM-DD.

    Handles: today, tomorrow, weekday names (next occurrence),
    month+day ("mar 10", "march 10"), m/d format ("3/10").
    Returns None for empty or unparseable input.
    """
    text = text.strip().lower()
    if not text:
        return None

    if today is None:
        today = date.today()

    # "today"
    if text == "today":
        return today.isoformat()

    # "tomorrow"
    if text == "tomorrow":
        return (today + timedelta(days=1)).isoformat()

    # Weekday names: "monday", "fri", etc.
    if text in _WEEKDAYS:
        target = _WEEKDAYS[text]
        days_ahead = (target - today.weekday()) % 7
        if days_ahead == 0:
            days_ahead = 7  # next week, not today
        return (today + timedelta(days=days_ahead)).isoformat()

    # "mar 10", "march 10"
    m = re.match(r"^([a-z]+)\s+(\d{1,2})$", text)
    if m:
        month_name, day_str = m.group(1), int(m.group(2))
        if month_name in _MONTHS:
            month = _MONTHS[month_name]
            try:
                d = date(today.year, month, day_str)
                if d < today:
                    d = date(today.year + 1, month, day_str)
                return d.isoformat()
            except ValueError:
                return None

    # "3/10" — m/d format
    m = re.match(r"^(\d{1,2})/(\d{1,2})$", text)
    if m:
        month, day = int(m.group(1)), int(m.group(2))
        try:
            d = date(today.year, month, day)
            if d < today:
                d = date(today.year + 1, month, day)
            return d.isoformat()
        except ValueError:
            return None

    # YYYY-MM-DD passthrough
    m = re.match(r"^\d{4}-\d{2}-\d{2}$", text)
    if m:
        try:
            date.fromisoformat(text)
            return text
        except ValueError:
            return None

    return None
