"""Tests for routing.py — context extraction, folder resolution, routing helpers."""

from __future__ import annotations

import hashlib
from typing import Any

import pytest

from notely.db import Database
from notely.routing import (
    CONTEXT_SNIPPET_LENGTH,
    DIST_DUPLICATE,
    DIST_GOOD_MATCH,
    DIST_WEAK_MATCH,
    RoutingDecision,
    _resolve_folder_text,
    _routing_from_dir,
    _routing_from_folder_default,
    _routing_from_note,
    extract_context,
)


# ---------------------------------------------------------------------------
# extract_context — pure function
# ---------------------------------------------------------------------------


class TestExtractContext:
    def test_both_context_and_raw(self):
        result = extract_context("raw paste text here", user_context="meeting notes")
        assert result == "meeting notes raw paste text here"

    def test_raw_only(self):
        result = extract_context("raw paste text")
        assert result == "raw paste text"

    def test_context_only(self):
        result = extract_context("", user_context="meeting notes")
        assert result == "meeting notes"

    def test_both_empty(self):
        assert extract_context("") == ""
        assert extract_context("", user_context=None) == ""
        assert extract_context("", user_context="") == ""

    def test_whitespace_context_ignored(self):
        result = extract_context("hello", user_context="   ")
        assert result == "hello"

    def test_raw_text_truncated(self):
        long_text = "x" * 1000
        result = extract_context(long_text)
        assert len(result) == CONTEXT_SNIPPET_LENGTH

    def test_raw_text_stripped(self):
        result = extract_context("  padded text  ")
        assert result == "padded text"


# ---------------------------------------------------------------------------
# RoutingDecision dataclass
# ---------------------------------------------------------------------------


class TestRoutingDecision:
    def test_basic_creation(self):
        rd = RoutingDecision(space="projects", group_slug="vault", group_display="Vault")
        assert rd.space == "projects"
        assert rd.group_slug == "vault"
        assert rd.group_display == "Vault"
        assert rd.group_is_new is False
        assert rd.append_to_note is None
        assert rd.subgroup_slug is None

    def test_with_subgroup(self):
        rd = RoutingDecision(
            space="clients", group_slug="acme", group_display="Acme Corp",
            subgroup_slug="onboarding", subgroup_display="Onboarding",
        )
        assert rd.subgroup_slug == "onboarding"
        assert rd.subgroup_display == "Onboarding"

    def test_new_group(self):
        rd = RoutingDecision(
            space="clients", group_slug="newclient", group_display="New Client",
            group_is_new=True,
        )
        assert rd.group_is_new is True

    def test_append_to_note(self):
        rd = RoutingDecision(
            space="projects", group_slug="vault", group_display="Vault",
            append_to_note="note-123",
        )
        assert rd.append_to_note == "note-123"


# ---------------------------------------------------------------------------
# _routing_from_note — pure function
# ---------------------------------------------------------------------------


class TestRoutingFromNote:
    def test_basic(self):
        note = {
            "space": "clients",
            "group_slug": "acme",
            "note_id": "abc123",
        }
        rd = _routing_from_note(note)
        assert rd.space == "clients"
        assert rd.group_slug == "acme"
        assert rd.append_to_note == "abc123"

    def test_with_subgroup(self):
        note = {
            "space": "clients",
            "group_slug": "acme",
            "subgroup_slug": "ideas",
            "note_id": "def456",
        }
        rd = _routing_from_note(note)
        assert rd.subgroup_slug == "ideas"
        assert rd.append_to_note == "def456"

    def test_display_from_slug(self):
        note = {
            "space": "projects",
            "group_slug": "my-project",
            "note_id": "xyz",
        }
        rd = _routing_from_note(note)
        assert rd.group_display == "My Project"  # slug → title case


# ---------------------------------------------------------------------------
# _routing_from_dir — pure function
# ---------------------------------------------------------------------------


class TestRoutingFromDir:
    def test_basic(self):
        d = {
            "space": "clients",
            "group_slug": "acme",
            "display_name": "Acme Corp",
        }
        rd = _routing_from_dir(d)
        assert rd.space == "clients"
        assert rd.group_slug == "acme"
        assert rd.group_display == "Acme Corp"
        assert rd.append_to_note is None

    def test_with_subgroup(self):
        d = {
            "space": "clients",
            "group_slug": "acme",
            "display_name": "Ideas",
            "subgroup_slug": "ideas",
        }
        rd = _routing_from_dir(d)
        assert rd.subgroup_slug == "ideas"


# ---------------------------------------------------------------------------
# _routing_from_folder_default
# ---------------------------------------------------------------------------


class TestRoutingFromFolderDefault:
    def test_group_level(self, config, db):
        folder = {"space": "clients", "group_slug": "acme", "display": "Acme Corp"}
        rd = _routing_from_folder_default(config, db, folder)
        assert rd.space == "clients"
        assert rd.group_slug == "acme"
        assert rd.group_display == "Acme Corp"
        assert rd.subgroup_slug is None

    def test_space_level(self, config, db):
        folder = {"space": "personal", "group_slug": "", "display": "Personal"}
        rd = _routing_from_folder_default(config, db, folder)
        assert rd.space == "personal"
        assert rd.group_slug == ""
        assert rd.group_display == "Personal"

    def test_subgroup_level(self, config, db):
        # Insert directory so lookup works
        db.upsert_directory(
            "clients/acme/ideas", "clients", "acme",
            display_name="Ideas", subgroup_slug="ideas",
        )
        folder = {"space": "clients", "group_slug": "acme/ideas", "display": "Ideas"}
        rd = _routing_from_folder_default(config, db, folder)
        assert rd.space == "clients"
        assert rd.group_slug == "acme"
        assert rd.subgroup_slug == "ideas"


# ---------------------------------------------------------------------------
# Hash functions (in db.py, used by routing)
# ---------------------------------------------------------------------------


class TestHashFunctions:
    def test_hash_raw_basic(self):
        h = Database._hash_raw("hello world")
        expected = hashlib.sha256("hello world".encode()).hexdigest()
        assert h == expected

    def test_hash_raw_strips_whitespace(self):
        h1 = Database._hash_raw("  hello  ")
        h2 = Database._hash_raw("hello")
        assert h1 == h2

    def test_hash_raw_empty_returns_none(self):
        assert Database._hash_raw("") is None
        assert Database._hash_raw("   ") is None

    def test_hash_snippet_basic(self):
        text = "x" * 500
        h = Database._hash_snippet(text)
        expected = hashlib.sha256(("x" * 300).encode()).hexdigest()
        assert h == expected

    def test_hash_snippet_short_text(self):
        h = Database._hash_snippet("short")
        expected = hashlib.sha256("short".encode()).hexdigest()
        assert h == expected

    def test_hash_snippet_empty_returns_none(self):
        assert Database._hash_snippet("") is None


# ---------------------------------------------------------------------------
# Duplicate detection (db methods, used by routing)
# ---------------------------------------------------------------------------


class TestDuplicateDetection:
    def _insert_note(self, db: Database, raw_text: str, note_id: str = "note-1") -> None:
        """Insert a note with hashes for duplicate detection."""
        from notely.models import Note, Refinement, InputSize
        from datetime import datetime, timezone

        note = Note(
            id=note_id,
            space="projects",
            title="Existing Note",
            date="2026-03-15",
            created=datetime.now(timezone.utc).isoformat(),
            updated=datetime.now(timezone.utc).isoformat(),
            file_path=f"projects/vault/2026-03-15_{note_id}.md",
            body="Body text",
            raw_text=raw_text,
        )
        db.upsert_note(note, hash_source=raw_text)

    def test_exact_duplicate_found(self, db):
        self._insert_note(db, "exact content here")
        dup = db.find_exact_duplicate("exact content here")
        assert dup is not None
        assert dup["id"] == "note-1"

    def test_exact_duplicate_not_found(self, db):
        self._insert_note(db, "original content")
        dup = db.find_exact_duplicate("different content")
        assert dup is None

    def test_snippet_match_found(self, db):
        # Same first 300 chars, different ending
        prefix = "a" * 300
        self._insert_note(db, prefix + " ending one")
        match = db.find_snippet_match(prefix + " ending two")
        assert match is not None

    def test_snippet_match_not_found(self, db):
        self._insert_note(db, "completely different text")
        match = db.find_snippet_match("no match at all")
        assert match is None

    def test_exact_hash_uses_full_content(self, db):
        text1 = "same start" + "a" * 500
        text2 = "same start" + "b" * 500
        self._insert_note(db, text1)
        # Different content → no exact match
        assert db.find_exact_duplicate(text2) is None
        # Same content → match
        assert db.find_exact_duplicate(text1) is not None


# ---------------------------------------------------------------------------
# Distance threshold constants
# ---------------------------------------------------------------------------


class TestDistanceThresholds:
    """Ensure routing thresholds maintain correct ordering."""

    def test_thresholds_ordered(self):
        assert DIST_DUPLICATE < DIST_GOOD_MATCH < DIST_WEAK_MATCH

    def test_duplicate_is_tight(self):
        assert DIST_DUPLICATE <= 0.3

    def test_weak_match_is_generous(self):
        assert DIST_WEAK_MATCH >= 0.5


# ---------------------------------------------------------------------------
# _resolve_folder_text — folder resolution
# ---------------------------------------------------------------------------


class TestResolveFolderText:
    def _make_dirs(self):
        return [
            {"space": "clients", "group_slug": "acme", "display_name": "Acme Corp"},
            {"space": "clients", "group_slug": "globex", "display_name": "Globex Inc"},
            {"space": "projects", "group_slug": "vault", "display_name": "Vault"},
        ]

    def test_matches_by_slug(self, config):
        dirs = self._make_dirs()
        rd = _resolve_folder_text(config, "vault", all_dirs=dirs, ask_space=False)
        assert rd is not None
        assert rd.space == "projects"
        assert rd.group_slug == "vault"
        assert rd.group_is_new is False

    def test_matches_by_display_name(self, config):
        dirs = self._make_dirs()
        rd = _resolve_folder_text(config, "Acme Corp", all_dirs=dirs, ask_space=False)
        assert rd is not None
        assert rd.group_slug == "acme"

    def test_matches_by_full_path(self, config):
        dirs = self._make_dirs()
        rd = _resolve_folder_text(config, "clients/acme", all_dirs=dirs, ask_space=False)
        assert rd is not None
        assert rd.space == "clients"
        assert rd.group_slug == "acme"

    def test_case_insensitive(self, config):
        dirs = self._make_dirs()
        rd = _resolve_folder_text(config, "VAULT", all_dirs=dirs, ask_space=False)
        assert rd is not None
        assert rd.group_slug == "vault"

    def test_create_new_false_returns_none_for_unknown(self, config):
        dirs = self._make_dirs()
        rd = _resolve_folder_text(config, "nonexistent", all_dirs=dirs, ask_space=False, create_new=False)
        assert rd is None

    def test_create_new_true_creates_folder(self, config):
        dirs = self._make_dirs()
        rd = _resolve_folder_text(config, "newclient", all_dirs=dirs, ask_space=False, create_new=True)
        assert rd is not None
        assert rd.group_is_new is True

    def test_empty_text_returns_none(self, config):
        rd = _resolve_folder_text(config, "", all_dirs=[], ask_space=False)
        assert rd is None

    def test_whitespace_text_returns_none(self, config):
        rd = _resolve_folder_text(config, "   ", all_dirs=[], ask_space=False)
        assert rd is None
