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
        assert "directories" in table_names
        assert "snippets" in table_names

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
    def test_todos_stored(self, db: Database):
        note = _make_note()
        db.upsert_note(note)
        items = [
            ActionItem(owner="alice", task="Do something", status=ActionItemStatus.OPEN),
            ActionItem(owner="bob", task="Review PR", due="2026-03-01", status=ActionItemStatus.OPEN),
        ]
        ids = db.add_todos_for_note(note.id, items, space="clients", group_slug="group")
        row = db.get_todo(ids[0])
        assert row is not None
        assert row["task"] == "Do something"
        assert row["owner"] == "alice"

    def test_todos_survive_note_deletion(self, db: Database):
        """Deleting a note preserves its todos as standalone (note_id cleared)."""
        note = _make_note()
        db.upsert_note(note)
        items = [
            ActionItem(owner="alice", task="Survive deletion", status=ActionItemStatus.OPEN),
        ]
        ids = db.add_todos_for_note(note.id, items, space="clients", group_slug="group")
        # Delete the note
        db.delete_note(note.id)
        # Todo should still exist with note_id = NULL
        row = db.get_todo(ids[0])
        assert row is not None
        assert row["task"] == "Survive deletion"
        assert row["note_id"] is None

    def test_get_nonexistent_todo(self, db: Database):
        assert db.get_todo(9999) is None

    def test_get_note_todos(self, db: Database):
        note = _make_note()
        db.upsert_note(note)
        items = [
            ActionItem(owner="alice", task="Task A", status=ActionItemStatus.OPEN),
            ActionItem(owner="bob", task="Task B", due="2026-03-01", status=ActionItemStatus.OPEN),
        ]
        db.add_todos_for_note(note.id, items, space="clients", group_slug="group")
        result = db.get_note_todos(note.id)
        assert len(result) == 2
        assert result[0]["owner"] == "alice"
        assert result[1]["owner"] == "bob"

    def test_clear_all_preserves_standalone_todos(self, db: Database):
        """clear_all() only deletes note-linked todos, preserving standalone ones."""
        note = _make_note()
        db.upsert_note(note)
        # Note-linked item
        linked = [ActionItem(owner="alice", task="Linked task", status=ActionItemStatus.OPEN)]
        db.add_todos_for_note(note.id, linked, space="clients", group_slug="group")
        # Standalone item
        standalone_id = db.add_todo(owner="bob", task="Standalone task", space="clients", group_slug="group")
        db.clear_all()
        # Standalone should survive
        row = db.get_todo(standalone_id)
        assert row is not None
        assert row["task"] == "Standalone task"


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


class TestContacts:
    def test_get_contacts_empty(self, db: Database):
        assert db.get_contacts() == []

    def test_add_and_get_contacts(self, db: Database):
        db.add_reference(entity="Jake Chen", key="email", value="jake@acme.com", snippet_type="contact")
        db.add_reference(entity="Jake Chen", key="role", value="Product Manager", snippet_type="contact")
        db.add_reference(entity="Sarah Kim", key="email", value="sarah@co.com", snippet_type="contact")

        contacts = db.get_contacts()
        assert len(contacts) == 3

    def test_get_contacts_by_entity(self, db: Database):
        db.add_reference(entity="Jake Chen", key="email", value="jake@acme.com", snippet_type="contact")
        db.add_reference(entity="Sarah Kim", key="email", value="sarah@co.com", snippet_type="contact")

        contacts = db.get_contacts(entity="Jake Chen")
        assert len(contacts) == 1
        assert contacts[0]["entity"] == "Jake Chen"
        assert contacts[0]["value"] == "jake@acme.com"

    def test_get_contacts_by_entity_case_insensitive(self, db: Database):
        db.add_reference(entity="Jake Chen", key="email", value="jake@acme.com", snippet_type="contact")

        contacts = db.get_contacts(entity="jake chen")
        assert len(contacts) == 1

    def test_get_contacts_excludes_non_contacts(self, db: Database):
        db.add_reference(entity="labcorp", key="npi", value="123", snippet_type="identifier")
        db.add_reference(entity="Jake Chen", key="email", value="jake@acme.com", snippet_type="contact")

        contacts = db.get_contacts()
        assert len(contacts) == 1
        assert contacts[0]["snippet_type"] == "contact"

    def test_get_references_exclude_type(self, db: Database):
        db.add_reference(entity="labcorp", key="npi", value="123", snippet_type="identifier")
        db.add_reference(entity="Jake Chen", key="email", value="jake@acme.com", snippet_type="contact")

        refs = db.get_references(exclude_type="contact")
        assert len(refs) == 1
        assert refs[0]["snippet_type"] == "identifier"

    def test_get_contact_interactions(self, db: Database):
        note = _make_note(
            id="note1",
            title="Kickoff Call",
            participants=["Jake Chen", "Alice"],
            file_path="clients/acme/2026-03-05_kickoff.md",
        )
        db.upsert_note(note)

        interactions = db.get_contact_interactions("Jake", limit=5)
        assert len(interactions) == 1
        assert interactions[0]["title"] == "Kickoff Call"

    def test_get_contact_interactions_case_insensitive(self, db: Database):
        note = _make_note(
            id="note1",
            title="Sync Call",
            participants=["Jake Chen"],
            file_path="clients/acme/2026-03-05_sync.md",
        )
        db.upsert_note(note)

        interactions = db.get_contact_interactions("jake", limit=5)
        assert len(interactions) == 1

    def test_get_contact_interactions_no_match(self, db: Database):
        note = _make_note(
            id="note1",
            title="Solo Note",
            participants=["Alice"],
            file_path="clients/acme/2026-03-05_solo.md",
        )
        db.upsert_note(note)

        interactions = db.get_contact_interactions("Jake", limit=5)
        assert len(interactions) == 0

    def test_folder_context_separates_contacts(self, db: Database):
        # Add a reference and a contact
        db.add_reference(
            space="clients", group_slug="acme",
            entity="labcorp", key="npi", value="123", snippet_type="identifier",
        )
        db.add_reference(
            space="clients", group_slug="acme",
            entity="Jake", key="email", value="jake@acme.com", snippet_type="contact",
        )

        # Add a note so folder context has something
        note = _make_note(
            id="note1", space="clients",
            file_path="clients/acme/2026-03-05_test.md",
        )
        db.upsert_note(note)

        ctx = db.get_folder_context("clients", "acme")
        assert len(ctx["references"]) == 1
        assert ctx["references"][0]["snippet_type"] == "identifier"
        assert len(ctx["contacts"]) == 1
        assert ctx["contacts"][0]["snippet_type"] == "contact"
        assert ctx["contacts"][0]["entity"] == "Jake"

    def test_folder_context_has_databases_dict(self, db: Database):
        db.add_reference(
            space="clients", group_slug="acme",
            entity="labcorp", key="npi", value="123", snippet_type="identifier",
        )
        db.add_reference(
            space="clients", group_slug="acme",
            entity="Jake", key="email", value="jake@acme.com", snippet_type="contact",
        )
        db.add_reference(
            space="clients", group_slug="acme",
            entity="Acme", key="address", value="123 Main St", snippet_type="vendors",
        )

        note = _make_note(id="note1", space="clients", file_path="clients/acme/2026-03-05_test.md")
        db.upsert_note(note)

        ctx = db.get_folder_context("clients", "acme")
        assert "databases" in ctx
        assert "identifier" in ctx["databases"]
        assert "contact" in ctx["databases"]
        assert "vendors" in ctx["databases"]
        assert len(ctx["databases"]["vendors"]) == 1
        assert ctx["databases"]["vendors"][0]["entity"] == "Acme"


class TestUserDefinedDatabases:
    def test_get_database_names_empty(self, db: Database):
        names = db.get_database_names()
        # Default databases always present
        assert "fact" in names
        assert "todo" in names
        assert len(names) == 2

    def test_get_database_names_returns_distinct_types(self, db: Database):
        db.add_reference(entity="a", key="k", value="v", snippet_type="contacts")
        db.add_reference(entity="b", key="k", value="v", snippet_type="fact")
        db.add_reference(entity="c", key="k", value="v", snippet_type="vendors")

        names = db.get_database_names()
        assert "contacts" in names
        assert "fact" in names
        assert "vendors" in names
        assert "todo" in names
        assert len(names) == 4

    def test_get_database_records_filters_by_type(self, db: Database):
        db.add_reference(entity="a", key="npi", value="123", snippet_type="fact")
        db.add_reference(entity="b", key="email", value="b@co.com", snippet_type="contacts")
        db.add_reference(entity="c", key="addr", value="123 Main", snippet_type="vendors")

        refs = db.get_database_records("fact")
        assert len(refs) == 1
        assert refs[0]["entity"] == "a"

        vendors = db.get_database_records("vendors")
        assert len(vendors) == 1
        assert vendors[0]["entity"] == "c"

    def test_get_database_records_with_entity_filter(self, db: Database):
        db.add_reference(entity="Acme", key="phone", value="555-1234", snippet_type="vendors")
        db.add_reference(entity="Globex", key="phone", value="555-5678", snippet_type="vendors")

        recs = db.get_database_records("vendors", entity="Acme")
        assert len(recs) == 1
        assert recs[0]["entity"] == "Acme"

    def test_get_database_records_with_space_filter(self, db: Database):
        db.add_reference(entity="a", key="k", value="v", snippet_type="vendors", space="clients", group_slug="acme")
        db.add_reference(entity="b", key="k", value="v", snippet_type="vendors", space="", group_slug="")

        recs = db.get_database_records("vendors", space="clients", group_slug="acme")
        assert len(recs) == 1
        assert recs[0]["entity"] == "a"

    def test_custom_database_auto_created(self, db: Database):
        """Adding a record with a new snippet_type auto-creates the database."""
        db.add_reference(entity="test", key="k", value="v", snippet_type="my-custom-db")
        names = db.get_database_names()
        assert "my-custom-db" in names

        recs = db.get_database_records("my-custom-db")
        assert len(recs) == 1

    def test_contacts_via_get_database_records(self, db: Database):
        """Contacts work through generic get_database_records too."""
        db.add_reference(entity="Jake", key="email", value="jake@co.com", snippet_type="contact")
        recs = db.get_database_records("contact")
        assert len(recs) == 1
        assert recs[0]["entity"] == "Jake"

    def test_database_exists_empty(self, db: Database):
        assert db.database_exists("contacts") is False
        assert db.database_exists("references") is False

    def test_database_exists_custom(self, db: Database):
        assert db.database_exists("vendors") is False
        db.add_reference(entity="a", key="k", value="v", snippet_type="vendors")
        assert db.database_exists("vendors") is True

    def test_delete_database(self, db: Database):
        db.add_reference(entity="a", key="k1", value="v1", snippet_type="vendors")
        db.add_reference(entity="b", key="k2", value="v2", snippet_type="vendors")
        db.add_reference(entity="c", key="k3", value="v3", snippet_type="contacts")

        count = db.delete_database("vendors")
        assert count == 2
        assert db.database_exists("vendors") is False
        # contacts untouched
        assert len(db.get_database_records("contacts")) == 1

    def test_delete_database_empty(self, db: Database):
        count = db.delete_database("nonexistent")
        assert count == 0

    def test_get_database_keys(self, db: Database):
        db.add_reference(entity="Jake", key="email", value="jake@co.com", snippet_type="contacts")
        db.add_reference(entity="Jake", key="phone", value="555-1234", snippet_type="contacts")
        db.add_reference(entity="Sarah", key="email", value="sarah@co.com", snippet_type="contacts")
        db.add_reference(entity="Sarah", key="role", value="PM", snippet_type="contacts")

        keys = db.get_database_keys("contacts")
        assert "email" in keys
        assert "phone" in keys
        assert "role" in keys
        # email appears twice, should be first (ordered by frequency)
        assert keys[0] == "email"

    def test_get_database_keys_empty(self, db: Database):
        assert db.get_database_keys("nonexistent") == []

    def test_get_database_keys_excludes_meta(self, db: Database):
        db.add_reference(entity="Jake", key="email", value="jake@co.com", snippet_type="contacts")
        db.set_database_description("contacts", "People and their info")
        keys = db.get_database_keys("contacts")
        assert "email" in keys
        assert "description" not in keys  # _meta record excluded

    def test_set_and_get_database_description(self, db: Database):
        assert db.get_database_description("vendors") is None
        db.set_database_description("vendors", "External vendor companies")
        assert db.get_database_description("vendors") == "External vendor companies"
        # Update
        db.set_database_description("vendors", "Updated description")
        assert db.get_database_description("vendors") == "Updated description"

    def test_find_similar_entities(self, db: Database):
        db.add_reference(entity="Jose Mason", key="email", value="jose@co.com", snippet_type="contacts")
        db.add_reference(entity="Jake Chen", key="email", value="jake@co.com", snippet_type="contacts")

        # "Jose" should match "Jose Mason" (substring)
        similar = db.find_similar_entities("Jose", "contacts")
        assert "Jose Mason" in similar

        # "jose mason" should not return itself
        similar = db.find_similar_entities("Jose Mason", "contacts")
        assert "Jose Mason" not in similar

        # No match
        similar = db.find_similar_entities("Bob Smith", "contacts")
        assert len(similar) == 0

        # Different database — no cross-contamination
        similar = db.find_similar_entities("Jose", "vendors")
        assert len(similar) == 0

    def test_database_records_excludes_meta(self, db: Database):
        db.add_reference(entity="Acme", key="npi", value="123", snippet_type="vendors")
        db.set_database_description("vendors", "Vendor companies")
        recs = db.get_database_records("vendors")
        assert len(recs) == 1
        assert recs[0]["entity"] == "Acme"  # _meta not included
