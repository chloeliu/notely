"""Simple time tracking — CSV-based, no DB dependency.

All state lives in `_timelog.csv` at workspace root. Running timers
have an empty `end` column so they survive crashes/quits.
"""

from __future__ import annotations

import csv
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .config import NotelyConfig

# CSV columns
FIELDNAMES = ["id", "folder", "description", "start", "end", "duration_minutes", "todo_id"]


def _timelog_path(config: NotelyConfig) -> Path:
    return config.base_dir / "_timelog.csv"


def _read_entries(config: NotelyConfig) -> list[dict]:
    path = _timelog_path(config)
    if not path.exists():
        return []
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def _write_entries(config: NotelyConfig, entries: list[dict]) -> None:
    path = _timelog_path(config)
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        writer.writeheader()
        writer.writerows(entries)


def get_running_timers(config: NotelyConfig) -> list[dict]:
    """Return entries where end is empty (still running)."""
    return [e for e in _read_entries(config) if not e.get("end")]


def start_timer(
    config: NotelyConfig, folder: str, description: str, todo_id: int | None = None
) -> dict:
    """Start a new timer. Appends to CSV with empty end."""
    entry = {
        "id": uuid.uuid4().hex[:8],
        "folder": folder,
        "description": description,
        "start": datetime.now(timezone.utc).isoformat(),
        "end": "",
        "duration_minutes": "",
        "todo_id": str(todo_id) if todo_id else "",
    }
    entries = _read_entries(config)
    entries.append(entry)
    _write_entries(config, entries)
    return entry


def stop_timer(
    config: NotelyConfig, timer_id: str, override_minutes: int | None = None
) -> dict | None:
    """Stop a running timer. Fills end + duration. Returns the entry or None."""
    entries = _read_entries(config)
    now = datetime.now(timezone.utc)
    for e in entries:
        if e["id"] == timer_id and not e.get("end"):
            e["end"] = now.isoformat()
            if override_minutes is not None:
                e["duration_minutes"] = str(override_minutes)
            else:
                start = datetime.fromisoformat(e["start"])
                delta = now - start
                e["duration_minutes"] = str(round(delta.total_seconds() / 60))
            _write_entries(config, entries)
            return e
    return None


def add_timer_entry(
    config: NotelyConfig, folder: str, description: str, duration_minutes: int,
    todo_id: int | None = None,
) -> dict:
    """Add a retroactive time log entry (already completed)."""
    now = datetime.now(timezone.utc)
    entry = {
        "id": uuid.uuid4().hex[:8],
        "folder": folder,
        "description": description,
        "start": now.isoformat(),
        "end": now.isoformat(),
        "duration_minutes": str(duration_minutes),
        "todo_id": str(todo_id) if todo_id else "",
    }
    entries = _read_entries(config)
    entries.append(entry)
    _write_entries(config, entries)
    return entry


def get_timer_log(
    config: NotelyConfig, folder: str | None = None, days: int = 7
) -> list[dict]:
    """Return completed entries, optionally filtered by folder and time window."""
    cutoff = datetime.now(timezone.utc).timestamp() - (days * 86400)
    results = []
    for e in _read_entries(config):
        if not e.get("end"):
            continue
        try:
            start_ts = datetime.fromisoformat(e["start"]).timestamp()
        except (ValueError, KeyError):
            continue
        if start_ts < cutoff:
            continue
        if folder:
            entry_folder = e.get("folder", "").lower()
            folder_lower = folder.lower()
            if entry_folder != folder_lower and not entry_folder.startswith(folder_lower + "/"):
                continue
        results.append(e)
    return results


def parse_duration(text: str) -> int | None:
    """Parse a duration string into minutes.

    Handles: 1h15m, 1h, 30m, 90, 1.5h, 2.5h, 0.25h
    Returns None if unparseable.
    """
    text = text.strip().lower()
    if not text:
        return None

    # "1h15m", "2h30m"
    m = re.match(r"^(\d+)h(\d+)m?$", text)
    if m:
        return int(m.group(1)) * 60 + int(m.group(2))

    # "1.5h", "0.25h"
    m = re.match(r"^(\d+\.?\d*)h$", text)
    if m:
        return round(float(m.group(1)) * 60)

    # "30m"
    m = re.match(r"^(\d+)m$", text)
    if m:
        return int(m.group(1))

    # Plain number = minutes
    m = re.match(r"^(\d+)$", text)
    if m:
        return int(m.group(1))

    return None


def format_duration(minutes: int) -> str:
    """Format minutes as human-readable: 1h15m, 30m, 2h."""
    if minutes <= 0:
        return "0m"
    h, m = divmod(minutes, 60)
    if h and m:
        return f"{h}h{m}m"
    if h:
        return f"{h}h"
    return f"{m}m"


def get_time_for_todo(config: NotelyConfig, todo_id: int) -> float:
    """Sum total duration_minutes for a given todo_id across all timer entries."""
    total = 0.0
    tid = str(todo_id)
    for e in _read_entries(config):
        if e.get("todo_id") == tid and e.get("duration_minutes"):
            try:
                total += float(e["duration_minutes"])
            except ValueError:
                pass
    return total


def get_running_timer_for_todo(config: NotelyConfig, todo_id: int) -> dict | None:
    """Return the running timer for a todo, or None."""
    tid = str(todo_id)
    for e in _read_entries(config):
        if e.get("todo_id") == tid and not e.get("end"):
            return e
    return None


def elapsed_since(start_iso: str) -> str:
    """Format elapsed time from an ISO timestamp to now."""
    start = datetime.fromisoformat(start_iso)
    delta = datetime.now(timezone.utc) - start
    minutes = round(delta.total_seconds() / 60)
    return format_duration(max(minutes, 0))
