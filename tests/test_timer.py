"""Tests for notely.timer — time tracking."""

from __future__ import annotations

import time

from notely.timer import (
    add_timer_entry,
    elapsed_since,
    format_duration,
    get_running_timers,
    get_timer_log,
    parse_duration,
    start_timer,
    stop_timer,
    _timelog_path,
)


# --- parse_duration ---


def test_parse_duration_hours_minutes():
    assert parse_duration("1h15m") == 75
    assert parse_duration("2h30m") == 150


def test_parse_duration_hours_only():
    assert parse_duration("1h") == 60
    assert parse_duration("2h") == 120


def test_parse_duration_fractional_hours():
    assert parse_duration("1.5h") == 90
    assert parse_duration("0.25h") == 15


def test_parse_duration_minutes_only():
    assert parse_duration("30m") == 30
    assert parse_duration("5m") == 5


def test_parse_duration_plain_number():
    assert parse_duration("90") == 90
    assert parse_duration("45") == 45


def test_parse_duration_invalid():
    assert parse_duration("") is None
    assert parse_duration("abc") is None
    assert parse_duration("h") is None
    assert parse_duration("1x2y") is None


def test_parse_duration_whitespace():
    assert parse_duration("  30m  ") == 30
    assert parse_duration("  1h  ") == 60


# --- format_duration ---


def test_format_duration_hours_and_minutes():
    assert format_duration(75) == "1h15m"
    assert format_duration(150) == "2h30m"


def test_format_duration_hours_only():
    assert format_duration(60) == "1h"
    assert format_duration(120) == "2h"


def test_format_duration_minutes_only():
    assert format_duration(30) == "30m"
    assert format_duration(5) == "5m"


def test_format_duration_zero():
    assert format_duration(0) == "0m"
    assert format_duration(-5) == "0m"


# --- start / stop / running ---


def test_start_timer(config):
    entry = start_timer(config, "sanity", "standup prep")
    assert entry["folder"] == "sanity"
    assert entry["description"] == "standup prep"
    assert entry["end"] == ""
    assert len(entry["id"]) == 8


def test_get_running_timers(config):
    start_timer(config, "sanity", "standup")
    start_timer(config, "vault", "api work")
    running = get_running_timers(config)
    assert len(running) == 2
    folders = {t["folder"] for t in running}
    assert folders == {"sanity", "vault"}


def test_stop_timer(config):
    entry = start_timer(config, "sanity", "standup")
    result = stop_timer(config, entry["id"])
    assert result is not None
    assert result["end"] != ""
    assert int(result["duration_minutes"]) >= 0

    # No longer running
    running = get_running_timers(config)
    assert len(running) == 0


def test_stop_timer_with_override(config):
    entry = start_timer(config, "sanity", "standup")
    result = stop_timer(config, entry["id"], override_minutes=45)
    assert result is not None
    assert result["duration_minutes"] == "45"


def test_stop_nonexistent_timer(config):
    result = stop_timer(config, "nonexistent")
    assert result is None


def test_multiple_start_stop(config):
    e1 = start_timer(config, "sanity", "standup")
    e2 = start_timer(config, "vault", "api work")
    assert len(get_running_timers(config)) == 2

    stop_timer(config, e1["id"])
    running = get_running_timers(config)
    assert len(running) == 1
    assert running[0]["id"] == e2["id"]


# --- add_timer_entry ---


def test_add_timer_entry(config):
    entry = add_timer_entry(config, "sanity", "emergency call", 30)
    assert entry["folder"] == "sanity"
    assert entry["description"] == "emergency call"
    assert entry["duration_minutes"] == "30"
    assert entry["end"] != ""

    # Should not show as running
    assert len(get_running_timers(config)) == 0


# --- get_timer_log ---


def test_get_timer_log_all(config):
    add_timer_entry(config, "sanity", "call 1", 30)
    add_timer_entry(config, "vault", "call 2", 45)
    log = get_timer_log(config)
    assert len(log) == 2


def test_get_timer_log_filtered_by_folder(config):
    add_timer_entry(config, "sanity", "call 1", 30)
    add_timer_entry(config, "vault", "call 2", 45)
    log = get_timer_log(config, folder="sanity")
    assert len(log) == 1
    assert log[0]["folder"] == "sanity"


def test_get_timer_log_excludes_running(config):
    start_timer(config, "sanity", "running task")
    add_timer_entry(config, "sanity", "done task", 30)
    log = get_timer_log(config)
    assert len(log) == 1
    assert log[0]["description"] == "done task"


def test_get_timer_log_case_insensitive_folder(config):
    add_timer_entry(config, "Sanity", "task", 30)
    log = get_timer_log(config, folder="sanity")
    assert len(log) == 1


def test_get_timer_log_prefix_match(config):
    """Filtering by parent folder includes subgroup entries."""
    add_timer_entry(config, "sanity", "parent task", 30)
    add_timer_entry(config, "sanity/onboarding", "sub task", 45)
    add_timer_entry(config, "vault", "other task", 60)
    # Parent filter includes both parent and child
    log = get_timer_log(config, folder="sanity")
    assert len(log) == 2
    folders = {e["folder"] for e in log}
    assert folders == {"sanity", "sanity/onboarding"}
    # Subgroup filter is exact — only the child
    log_sub = get_timer_log(config, folder="sanity/onboarding")
    assert len(log_sub) == 1
    assert log_sub[0]["folder"] == "sanity/onboarding"


# --- elapsed_since ---


def test_elapsed_since():
    from datetime import datetime, timezone, timedelta
    past = (datetime.now(timezone.utc) - timedelta(hours=2, minutes=15)).isoformat()
    result = elapsed_since(past)
    assert "2h" in result


# --- CSV persistence ---


def test_csv_file_created(config):
    path = _timelog_path(config)
    assert not path.exists()
    start_timer(config, "test", "task")
    assert path.exists()


def test_entries_persist_across_reads(config):
    start_timer(config, "sanity", "task1")
    add_timer_entry(config, "vault", "task2", 60)
    # Re-read from disk
    running = get_running_timers(config)
    log = get_timer_log(config)
    assert len(running) == 1
    assert len(log) == 1
