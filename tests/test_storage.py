"""Tests for storage.py — markdown I/O, CSV sync, save pipeline."""

from __future__ import annotations

import csv
from datetime import datetime, timezone
from pathlib import Path

import pytest

from notely.config import NotelyConfig
from notely.db import Database
from notely.models import ActionItem, InputSize, Note, Refinement
from notely.storage import (
    INPUT_SIZE_MEDIUM,
    INPUT_SIZE_SMALL,
    absolute_path,
    append_to_note,
    classify_input_size,
    delete_note_files,
    generate_file_path,
    raw_file_path,
    read_note,
    save_and_sync,
    sync_database_indexes,
    sync_todo_index,
    update_action_status,
    write_index_file,
    write_note,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_note(**overrides) -> Note:
    """Create a Note with sensible defaults for testing."""
    defaults = dict(
        id="test-abc123",
        space="projects",
        title="Test Meeting",
        source="paste",
        refinement=Refinement.AI_STRUCTURED,
        input_size=InputSize.MEDIUM,
        date="2026-03-15",
        created=datetime.now(timezone.utc).isoformat(),
        updated=datetime.now(timezone.utc).isoformat(),
        summary="Discussed the roadmap.",
        tags=["meeting", "roadmap"],
        participants=["Alice", "Bob"],
        file_path="projects/vault/2026-03-15_test-meeting.md",
        body="# Test Meeting\n\nWe discussed the roadmap for Q2.",
        raw_text="hey just got off call with alice and bob about the roadmap",
    )
    defaults.update(overrides)
    return Note(**defaults)


# ---------------------------------------------------------------------------
# classify_input_size — pure function
# ---------------------------------------------------------------------------


class TestClassifyInputSize:
    def test_empty_string(self):
        assert classify_input_size("") == InputSize.SMALL

    def test_short_text(self):
        assert classify_input_size("hello") == InputSize.SMALL

    def test_boundary_small(self):
        assert classify_input_size("x" * (INPUT_SIZE_SMALL - 1)) == InputSize.SMALL

    def test_boundary_medium(self):
        assert classify_input_size("x" * INPUT_SIZE_SMALL) == InputSize.MEDIUM

    def test_medium_text(self):
        assert classify_input_size("x" * 5000) == InputSize.MEDIUM

    def test_boundary_large(self):
        assert classify_input_size("x" * INPUT_SIZE_MEDIUM) == InputSize.LARGE

    def test_large_text(self):
        assert classify_input_size("x" * 50_000) == InputSize.LARGE


# ---------------------------------------------------------------------------
# generate_file_path — pure function
# ---------------------------------------------------------------------------


class TestGenerateFilePath:
    def test_basic_path(self, config):
        path = generate_file_path(config, "projects", "vault", "2026-03-15", "API Design")
        assert path == "projects/vault/2026-03-15_api-design.md"

    def test_long_title_truncated(self, config):
        long_title = "A very long title that exceeds the sixty character maximum slug length limit"
        path = generate_file_path(config, "projects", "vault", "2026-03-15", long_title)
        # Slug is capped at 60 chars
        slug = path.split("_", 1)[1].replace(".md", "")
        assert len(slug) <= 60

    def test_with_subgroup(self, config):
        path = generate_file_path(
            config, "clients", "acme", "2026-03-15", "Kickoff", subgroup="onboarding"
        )
        assert path == "clients/acme/onboarding/2026-03-15_kickoff.md"

    def test_special_chars_in_title(self, config):
        path = generate_file_path(config, "projects", "vault", "2026-03-15", "What's New? (v2.0)")
        assert ".md" in path
        # No special chars in filename
        filename = Path(path).name
        assert "?" not in filename
        assert "'" not in filename


# ---------------------------------------------------------------------------
# Path helpers — pure functions
# ---------------------------------------------------------------------------


class TestPathHelpers:
    def test_absolute_path(self, config):
        result = absolute_path(config, "projects/vault/note.md")
        assert result == config.notes_dir / "projects/vault/note.md"

    def test_raw_file_path(self, config):
        result = raw_file_path(config, "projects/vault/2026-03-15_meeting.md")
        expected = config.raw_dir / "projects/vault/2026-03-15_meeting.txt"
        assert result == expected

    def test_raw_file_path_preserves_structure(self, config):
        result = raw_file_path(config, "clients/acme/sub/2026-03-15_note.md")
        assert "clients/acme/sub" in str(result)
        assert result.suffix == ".txt"


# ---------------------------------------------------------------------------
# write_note / read_note — roundtrip I/O
# ---------------------------------------------------------------------------


class TestWriteReadNote:
    def test_write_creates_md_file(self, config):
        note = _make_note()
        path = write_note(config, note)
        assert path.exists()
        assert path.suffix == ".md"

    def test_write_creates_raw_file(self, config):
        note = _make_note()
        write_note(config, note)
        raw = raw_file_path(config, note.file_path)
        assert raw.exists()
        assert raw.read_text() == note.raw_text

    def test_roundtrip_preserves_title(self, config):
        note = _make_note(title="Important Meeting")
        write_note(config, note)
        loaded = read_note(config, note.file_path)
        assert loaded is not None
        assert loaded.title == "Important Meeting"

    def test_roundtrip_preserves_tags(self, config):
        note = _make_note(tags=["urgent", "q2", "roadmap"])
        write_note(config, note)
        loaded = read_note(config, note.file_path)
        assert loaded.tags == ["urgent", "q2", "roadmap"]

    def test_roundtrip_preserves_participants(self, config):
        note = _make_note(participants=["Alice", "Bob", "Charlie"])
        write_note(config, note)
        loaded = read_note(config, note.file_path)
        assert loaded.participants == ["Alice", "Bob", "Charlie"]

    def test_roundtrip_preserves_body(self, config):
        body = "# Meeting\n\nWe discussed:\n- Item 1\n- Item 2\n\n## Action Items\n\nNone."
        note = _make_note(body=body)
        write_note(config, note)
        loaded = read_note(config, note.file_path)
        assert loaded.body.strip() == body.strip()

    def test_roundtrip_preserves_raw_text(self, config):
        raw = "hey just got off the call, here are the notes..."
        note = _make_note(raw_text=raw)
        write_note(config, note)
        loaded = read_note(config, note.file_path)
        assert loaded.raw_text == raw

    def test_roundtrip_preserves_summary(self, config):
        note = _make_note(summary="Q2 roadmap discussion with key decisions.")
        write_note(config, note)
        loaded = read_note(config, note.file_path)
        assert loaded.summary == "Q2 roadmap discussion with key decisions."

    def test_roundtrip_preserves_source_url(self, config):
        note = _make_note(source_url="https://example.com/article")
        write_note(config, note)
        loaded = read_note(config, note.file_path)
        assert loaded.source_url == "https://example.com/article"

    def test_read_nonexistent_returns_none(self, config):
        result = read_note(config, "nonexistent/path/note.md")
        assert result is None

    def test_write_no_raw_text(self, config):
        note = _make_note(raw_text="")
        write_note(config, note)
        raw = raw_file_path(config, note.file_path)
        assert not raw.exists()

    def test_write_truncates_huge_raw_text(self, config):
        huge = "x" * 200_000
        note = _make_note(raw_text=huge)
        write_note(config, note)
        raw = raw_file_path(config, note.file_path)
        content = raw.read_text()
        assert len(content) < 200_000
        assert "Truncated" in content


# ---------------------------------------------------------------------------
# delete_note_files
# ---------------------------------------------------------------------------


class TestDeleteNoteFiles:
    def test_deletes_md_and_raw(self, config):
        note = _make_note()
        write_note(config, note)
        md_path = absolute_path(config, note.file_path)
        raw_path = raw_file_path(config, note.file_path)
        assert md_path.exists()
        assert raw_path.exists()

        delete_note_files(config, note.file_path)
        assert not md_path.exists()
        assert not raw_path.exists()

    def test_delete_nonexistent_no_error(self, config):
        # Should not raise
        delete_note_files(config, "nonexistent/path/note.md")


# ---------------------------------------------------------------------------
# append_to_note
# ---------------------------------------------------------------------------


class TestAppendToNote:
    def test_appends_body(self, config):
        note = _make_note(body="Original content")
        write_note(config, note)
        updated = append_to_note(config, note, "New content", "new raw")
        assert "Original content" in updated.body
        assert "New content" in updated.body
        assert "---" in updated.body  # separator

    def test_merges_tags(self, config):
        note = _make_note(tags=["a", "b"])
        write_note(config, note)
        updated = append_to_note(config, note, "body", "raw", new_tags=["b", "c"])
        assert updated.tags == ["a", "b", "c"]

    def test_merges_participants(self, config):
        note = _make_note(participants=["Alice"])
        write_note(config, note)
        updated = append_to_note(config, note, "body", "raw", new_participants=["Alice", "Bob"])
        assert updated.participants == ["Alice", "Bob"]


# ---------------------------------------------------------------------------
# write_index_file
# ---------------------------------------------------------------------------


class TestWriteIndexFile:
    def test_creates_csv(self, config):
        path = write_index_file(config, "test", ["Name", "Value"], [["a", "1"], ["b", "2"]])
        assert path.exists()
        assert path.name == "_test.csv"

    def test_csv_content(self, config):
        path = write_index_file(config, "items", ["Col1", "Col2"], [["hello", "world"]])
        reader = csv.reader(path.read_text().splitlines())
        rows = list(reader)
        assert rows[0] == ["Col1", "Col2"]
        assert rows[1] == ["hello", "world"]


# ---------------------------------------------------------------------------
# sync_todo_index — requires DB
# ---------------------------------------------------------------------------


class TestSyncTodoIndex:
    def test_creates_todos_csv(self, config, db):
        db.add_todo(owner="Alice", task="Deploy v2", due="2026-03-20")
        path = sync_todo_index(config, db)
        assert path.exists()
        assert path.name == "_todos.csv"
        content = path.read_text()
        assert "Deploy v2" in content
        assert "Alice" in content

    def test_empty_todos_still_creates_csv(self, config, db):
        path = sync_todo_index(config, db)
        assert path.exists()
        content = path.read_text()
        assert "Status" in content  # header row


# ---------------------------------------------------------------------------
# sync_database_indexes — requires DB
# ---------------------------------------------------------------------------


class TestSyncDatabaseIndexes:
    def test_creates_csv_per_database(self, config, db):
        db.add_reference(
            entity="Dr. Vox", key="npi", value="1234567890",
            snippet_type="contacts", space="clients", group_slug="sanity",
        )
        sync_database_indexes(config, db)
        csv_path = config.base_dir / "_contacts.csv"
        assert csv_path.exists()
        content = csv_path.read_text()
        assert "Dr. Vox" in content
        assert "1234567890" in content

    def test_no_databases_no_crash(self, config, db):
        # Should not raise even with empty DB
        sync_database_indexes(config, db)


# ---------------------------------------------------------------------------
# update_action_status — requires DB
# ---------------------------------------------------------------------------


class TestUpdateActionStatus:
    def test_marks_done(self, config, db):
        item_id = db.add_todo(owner="Alice", task="Ship it")
        result = update_action_status(config, db, item_id, "done")
        assert result is not None

        # Verify DB state
        row = db.get_todo(item_id)
        assert row["status"] == "done"

    def test_nonexistent_returns_none(self, config, db):
        result = update_action_status(config, db, 99999, "done")
        assert result is None

    def test_same_status_no_op(self, config, db):
        item_id = db.add_todo(owner="Alice", task="Ship it")
        result = update_action_status(config, db, item_id, "open")
        assert result is not None  # returns the row, no error


# ---------------------------------------------------------------------------
# save_and_sync — integration test
# ---------------------------------------------------------------------------


class TestSaveAndSync:
    def test_full_pipeline(self, config, db):
        note = _make_note()
        save_and_sync(config, db, note, hash_source="raw paste content")

        # Verify markdown written
        md_path = absolute_path(config, note.file_path)
        assert md_path.exists()

        # Verify DB has the note
        row = db.get_note(note.id)
        assert row is not None
        assert row["title"] == "Test Meeting"

    def test_with_action_items(self, config, db):
        note = _make_note()
        items = [ActionItem(task="Deploy v2", owner="Jake", due="2026-03-20")]
        save_and_sync(config, db, note, action_items=items)

        # Verify todo was inserted
        todos = db.get_open_todos()
        tasks = [t["task"] for t in todos]
        assert "Deploy v2" in tasks

    def test_hash_stored_for_dedup(self, config, db):
        note = _make_note()
        save_and_sync(config, db, note, hash_source="unique paste content")

        row = db.get_note(note.id)
        assert row["raw_hash"] is not None
        assert row["snippet_hash"] is not None

    def test_csv_synced_after_save(self, config, db):
        note = _make_note()
        items = [ActionItem(task="Write tests", owner="Chloe")]
        save_and_sync(config, db, note, action_items=items)

        csv_path = config.base_dir / "_todos.csv"
        assert csv_path.exists()
        assert "Write tests" in csv_path.read_text()
