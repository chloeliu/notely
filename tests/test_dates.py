"""Tests for notely.dates — natural language date parsing."""

from datetime import date

from notely.dates import parse_due_date


def test_today():
    d = date(2026, 3, 5)
    assert parse_due_date("today", today=d) == "2026-03-05"


def test_tomorrow():
    d = date(2026, 3, 5)
    assert parse_due_date("tomorrow", today=d) == "2026-03-06"


def test_weekday_future():
    # 2026-03-05 is a Thursday
    d = date(2026, 3, 5)
    assert parse_due_date("friday", today=d) == "2026-03-06"
    assert parse_due_date("fri", today=d) == "2026-03-06"


def test_weekday_same_day_goes_next_week():
    # Thursday asking for thursday = next thursday
    d = date(2026, 3, 5)
    assert parse_due_date("thursday", today=d) == "2026-03-12"
    assert parse_due_date("thu", today=d) == "2026-03-12"


def test_weekday_monday():
    d = date(2026, 3, 5)  # Thursday
    assert parse_due_date("monday", today=d) == "2026-03-09"
    assert parse_due_date("mon", today=d) == "2026-03-09"


def test_month_day_future():
    d = date(2026, 3, 5)
    assert parse_due_date("mar 10", today=d) == "2026-03-10"
    assert parse_due_date("march 10", today=d) == "2026-03-10"


def test_month_day_past_wraps_year():
    d = date(2026, 3, 5)
    assert parse_due_date("jan 15", today=d) == "2027-01-15"


def test_slash_format():
    d = date(2026, 3, 5)
    assert parse_due_date("3/10", today=d) == "2026-03-10"
    assert parse_due_date("1/15", today=d) == "2027-01-15"


def test_iso_passthrough():
    assert parse_due_date("2026-03-10") == "2026-03-10"


def test_empty():
    assert parse_due_date("") is None
    assert parse_due_date("  ") is None


def test_invalid():
    assert parse_due_date("gibberish") is None
    assert parse_due_date("2026-13-01") is None


def test_case_insensitive():
    d = date(2026, 3, 5)
    assert parse_due_date("FRIDAY", today=d) == "2026-03-06"
    assert parse_due_date("Mar 10", today=d) == "2026-03-10"
    assert parse_due_date("TODAY", today=d) == "2026-03-05"
