"""Tests for Database core operations: init, upsert, search, duplicates."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from notely.db import Database
from notely.models import Note, ActionItem, Refinement, InputSize, ActionItemStatus


def _make_note(
    id: str = "test1234",
    space: str = "clients",
    title: str = "Test Note",
    **overrides,
) -> Note:
    """Create a minimal Note for testing."""
    now = datetime.now(timezone.utc).isoformat()
    defaults = dict(
        id=id,
        space=space,
        title=title,
        source="meeting",
        refinement=Refinement.AI_STRUCTURED,
        input_size=InputSize.MEDIUM,
        date="2026-02-22",
        created=now,
        updated=now,
        summary="A test summary",
        tags=["test"],
        participants=["alice"],
        body="Body content here.",
        file_path=f"{space}/group/2026-02-22_test-note.md",
        space_metadata={"client": "acme"},
        action_items=[],
        related_contexts=[],
    )
    defaults.update(overrides)
    return Note(**defaults)


class TestDatabaseInit:
    def test_initialize_creates_tables(self, db: Database):
        tables = db.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        table_names = {r["name"] for r in tables}
        assert "notes" in table_names
        assert "action_items" in table_names
        assert "directories" in table_names

    def test_context_manager(self, tmp_workspace):
        with Database(tmp_workspace / "index.db") as db:
            db.initialize()
            assert db.conn is not None
        # After exiting, connection should be closed
        assert db._conn is None


class TestUpsertAndGet:
    def test_upsert_and_get(self, db: Database):
        note = _make_note()
        db.upsert_note(note)
        row = db.get_note("test1234")
        assert row is not None
        assert row["title"] == "Test Note"
        assert row["space"] == "clients"

    def test_upsert_updates_existing(self, db: Database):
        note = _make_note()
        db.upsert_note(note)
        note.title = "Updated Title"
        db.upsert_note(note)
        row = db.get_note("test1234")
        assert row["title"] == "Updated Title"

    def test_get_nonexistent_returns_none(self, db: Database):
        assert db.get_note("nonexistent") is None


class TestDuplicateDetection:
    def test_exact_duplicate(self, db: Database):
        note = _make_note(raw_text="Hello world meeting notes")
        db.upsert_note(note, hash_source="Hello world meeting notes")
        result = db.find_exact_duplicate("Hello world meeting notes")
        assert result is not None
        assert result["id"] == "test1234"

    def test_no_exact_duplicate(self, db: Database):
        note = _make_note(raw_text="Hello world meeting notes")
        db.upsert_note(note, hash_source="Hello world meeting notes")
        result = db.find_exact_duplicate("Completely different text")
        assert result is None

    def test_snippet_match(self, db: Database):
        text = "A" * 400  # Longer than 300 chars
        note = _make_note(raw_text=text)
        db.upsert_note(note, hash_source=text)
        # Same first 300 chars, different tail
        modified = "A" * 300 + "B" * 100
        result = db.find_snippet_match(modified)
        assert result is not None
        assert result["id"] == "test1234"


class TestActionItems:
    def test_action_items_stored(self, db: Database):
        note = _make_note(
            action_items=[
                ActionItem(owner="alice", task="Do something", status=ActionItemStatus.OPEN),
                ActionItem(owner="bob", task="Review PR", due="2026-03-01", status=ActionItemStatus.OPEN),
            ]
        )
        db.upsert_note(note)
        row = db.get_action_item(1)
        assert row is not None
        assert row["task"] == "Do something"
        assert row["owner"] == "alice"

    def test_get_nonexistent_action_item(self, db: Database):
        assert db.get_action_item(9999) is None


class TestSearch:
    def test_search_returns_notes(self, db: Database):
        db.upsert_note(_make_note(id="n1", title="Alpha meeting",
                                  file_path="clients/group/2026-02-22_alpha.md"))
        db.upsert_note(_make_note(id="n2", title="Beta review",
                                  file_path="clients/group/2026-02-22_beta.md"))
        results = db.search(limit=10)
        assert len(results) == 2

    def test_search_with_space_filter(self, db: Database):
        from notely.models import SearchFilters
        db.upsert_note(_make_note(id="n1", space="clients", title="Client note",
                                  file_path="clients/group/2026-02-22_client.md"))
        db.upsert_note(_make_note(id="n2", space="ideas", title="Idea note",
                                  file_path="ideas/cat/2026-02-22_idea.md"))
        filters = SearchFilters(space="ideas")
        results = db.search(filters=filters, limit=10)
        assert len(results) == 1
        assert results[0]["title"] == "Idea note"


class TestDeleteNote:
    def test_delete_note(self, db: Database):
        db.upsert_note(_make_note())
        assert db.get_note("test1234") is not None
        db.delete_note("test1234")
        assert db.get_note("test1234") is None
