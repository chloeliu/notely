"""SQLite database operations, FTS5, and search."""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path
from typing import Any

from .models import ActionItem, Note, SearchFilters, SearchResult

logger = logging.getLogger(__name__)

# Text limits used in hashing and storage
SNIPPET_LENGTH = 300       # Chars for snippet hash (duplicate detection)
BODY_PREVIEW_LENGTH = 500  # Chars stored as body_preview in DB

# Columns that update_note_metadata() is allowed to SET.
# Any key not in this set is rejected to prevent SQL column injection.
_UPDATABLE_NOTE_COLUMNS = frozenset({
    "title", "summary", "tags", "participants", "body_preview",
    "file_path", "space_metadata", "source", "refinement",
    "input_size", "date", "source_url",
})


def safe_json_loads(value: str | list | dict | None, default: Any = None) -> Any:
    """Safely parse a JSON string, passing through non-strings unchanged."""
    if value is None:
        return default if default is not None else {}
    if isinstance(value, (list, dict)):
        return value
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return default if default is not None else {}


def safe_parse_tags(value: str | list | None) -> list[str]:
    """Safely parse tags from a DB row (JSON string or list)."""
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            result = json.loads(value)
            return result if isinstance(result, list) else []
        except (json.JSONDecodeError, TypeError):
            return []
    return []

SCHEMA_SQL = """\
CREATE TABLE IF NOT EXISTS notes (
    id TEXT PRIMARY KEY,
    space TEXT NOT NULL,
    title TEXT NOT NULL,
    source TEXT NOT NULL,
    refinement TEXT NOT NULL DEFAULT 'ai-structured',
    input_size TEXT NOT NULL DEFAULT 'medium',
    raw_file TEXT,
    raw_hash TEXT,
    snippet_hash TEXT,
    date TEXT NOT NULL,
    created TEXT NOT NULL,
    updated TEXT NOT NULL,
    summary TEXT NOT NULL,
    tags TEXT NOT NULL,
    participants TEXT NOT NULL,
    file_path TEXT NOT NULL UNIQUE,
    body_preview TEXT NOT NULL,
    space_metadata TEXT NOT NULL DEFAULT '{}',
    source_url TEXT NOT NULL DEFAULT ''
);

CREATE VIRTUAL TABLE IF NOT EXISTS notes_fts USING fts5(
    title, summary, tags, participants, body_preview,
    content='notes', content_rowid='rowid',
    tokenize='porter unicode61'
);

-- Triggers to keep FTS in sync
CREATE TRIGGER IF NOT EXISTS notes_ai AFTER INSERT ON notes BEGIN
    INSERT INTO notes_fts(rowid, title, summary, tags, participants, body_preview)
    VALUES (new.rowid, new.title, new.summary, new.tags, new.participants, new.body_preview);
END;

CREATE TRIGGER IF NOT EXISTS notes_ad AFTER DELETE ON notes BEGIN
    INSERT INTO notes_fts(notes_fts, rowid, title, summary, tags, participants, body_preview)
    VALUES ('delete', old.rowid, old.title, old.summary, old.tags, old.participants, old.body_preview);
END;

CREATE TRIGGER IF NOT EXISTS notes_au AFTER UPDATE ON notes BEGIN
    INSERT INTO notes_fts(notes_fts, rowid, title, summary, tags, participants, body_preview)
    VALUES ('delete', old.rowid, old.title, old.summary, old.tags, old.participants, old.body_preview);
    INSERT INTO notes_fts(rowid, title, summary, tags, participants, body_preview)
    VALUES (new.rowid, new.title, new.summary, new.tags, new.participants, new.body_preview);
END;

CREATE TABLE IF NOT EXISTS cross_refs (
    note_id TEXT NOT NULL REFERENCES notes(id) ON DELETE CASCADE,
    target_path TEXT NOT NULL,
    PRIMARY KEY (note_id, target_path)
);

CREATE TABLE IF NOT EXISTS directories (
    id TEXT PRIMARY KEY,
    space TEXT NOT NULL,
    group_slug TEXT NOT NULL,
    subgroup_slug TEXT,
    display_name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    created TEXT NOT NULL,
    updated TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_notes_space ON notes(space);
CREATE INDEX IF NOT EXISTS idx_notes_date ON notes(date DESC);
CREATE INDEX IF NOT EXISTS idx_notes_refinement ON notes(refinement);
CREATE INDEX IF NOT EXISTS idx_cross_refs_target ON cross_refs(target_path);
CREATE INDEX IF NOT EXISTS idx_directories_space ON directories(space);
CREATE INDEX IF NOT EXISTS idx_notes_raw_hash ON notes(raw_hash);
CREATE INDEX IF NOT EXISTS idx_notes_snippet_hash ON notes(snippet_hash);

CREATE TABLE IF NOT EXISTS snippets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    space TEXT NOT NULL DEFAULT '',
    group_slug TEXT NOT NULL DEFAULT '',
    entity TEXT NOT NULL,
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    snippet_type TEXT NOT NULL DEFAULT 'fact',
    tags TEXT NOT NULL DEFAULT '[]',
    created TEXT NOT NULL,
    note_id TEXT REFERENCES notes(id) ON DELETE SET NULL,
    status TEXT NOT NULL DEFAULT '',
    flagged_date TEXT
);

CREATE VIRTUAL TABLE IF NOT EXISTS snippets_fts USING fts5(
    entity, key, value, description,
    content='snippets', content_rowid='rowid',
    tokenize='porter unicode61'
);

CREATE TRIGGER IF NOT EXISTS snippets_ai AFTER INSERT ON snippets BEGIN
    INSERT INTO snippets_fts(rowid, entity, key, value, description)
    VALUES (new.rowid, new.entity, new.key, new.value, new.description);
END;
CREATE TRIGGER IF NOT EXISTS snippets_ad AFTER DELETE ON snippets BEGIN
    INSERT INTO snippets_fts(snippets_fts, rowid, entity, key, value, description)
    VALUES ('delete', old.rowid, old.entity, old.key, old.value, old.description);
END;
CREATE TRIGGER IF NOT EXISTS snippets_au AFTER UPDATE ON snippets BEGIN
    INSERT INTO snippets_fts(snippets_fts, rowid, entity, key, value, description)
    VALUES ('delete', old.rowid, old.entity, old.key, old.value, old.description);
    INSERT INTO snippets_fts(rowid, entity, key, value, description)
    VALUES (new.rowid, new.entity, new.key, new.value, new.description);
END;

CREATE INDEX IF NOT EXISTS idx_snippets_space ON snippets(space, group_slug);
CREATE INDEX IF NOT EXISTS idx_snippets_entity ON snippets(entity);
CREATE INDEX IF NOT EXISTS idx_snippets_note ON snippets(note_id);

CREATE TABLE IF NOT EXISTS inbox (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    source_id TEXT NOT NULL DEFAULT '',
    type TEXT NOT NULL DEFAULT 'note',
    title TEXT NOT NULL DEFAULT '',
    summary TEXT NOT NULL DEFAULT '',
    body TEXT NOT NULL DEFAULT '',
    tags TEXT NOT NULL DEFAULT '[]',
    participants TEXT NOT NULL DEFAULT '[]',
    action_items TEXT NOT NULL DEFAULT '[]',
    source_url TEXT NOT NULL DEFAULT '',
    metadata TEXT NOT NULL DEFAULT '{}',
    suggested_space TEXT NOT NULL DEFAULT '',
    suggested_group TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending',
    created TEXT NOT NULL,
    reviewed_at TEXT NOT NULL DEFAULT '',
    filed_note_id TEXT NOT NULL DEFAULT '',
    processed INTEGER NOT NULL DEFAULT 1
);

CREATE INDEX IF NOT EXISTS idx_inbox_status ON inbox(status);
CREATE INDEX IF NOT EXISTS idx_inbox_source ON inbox(source);
CREATE UNIQUE INDEX IF NOT EXISTS idx_inbox_dedup ON inbox(source, source_id)
    WHERE source_id != '';
"""


class Database:
    """SQLite database for note indexing and search."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self._conn: sqlite3.Connection | None = None

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(str(self.db_path))
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
        return self._conn

    def initialize(self) -> None:
        self._migrate()
        self.conn.executescript(SCHEMA_SQL)
        self.conn.commit()
        self._ensure_todo_database()

    def _ensure_todo_database(self) -> None:
        """Ensure the 'todo' database has _meta rows for extract_from_notes."""
        if self.get_database_meta("todo", "extract_from_notes") != "true":
            self.set_database_meta("todo", "extract_from_notes", "true")
            self.set_database_meta("todo", "description",
                                   "Action items with owner and optional due date")
            if not self.get_database_fields("todo"):
                self.set_database_fields("todo", ["owner", "due"])

    def _migrate(self) -> None:
        """Add columns that may be missing from older databases."""
        migrations = [
            ("notes", "raw_hash", "TEXT"),
            ("notes", "snippet_hash", "TEXT"),
            ("notes", "source_url", "TEXT NOT NULL DEFAULT ''"),
            ("inbox", "processed", "INTEGER NOT NULL DEFAULT 1"),
            ("action_items", "flagged_date", "TEXT"),
            ("snippets", "status", "TEXT NOT NULL DEFAULT ''"),
            ("snippets", "flagged_date", "TEXT"),
        ]
        for table, column, col_type in migrations:
            try:
                self.conn.execute(
                    f"ALTER TABLE {table} ADD COLUMN {column} {col_type}"
                )
            except sqlite3.OperationalError:
                pass  # column already exists

        # Migrate action_items FK from ON DELETE CASCADE to ON DELETE SET NULL.
        # SQLite can't ALTER constraints, so we recreate the table.
        self._migrate_action_items_fk()

    def _migrate_action_items_fk(self) -> None:
        """Migrate action_items FK from ON DELETE CASCADE to ON DELETE SET NULL.

        Runs once — detects CASCADE via PRAGMA and recreates the table if needed.
        """
        try:
            fk_info = self.conn.execute(
                "PRAGMA foreign_key_list(action_items)"
            ).fetchall()
        except sqlite3.OperationalError:
            return  # table doesn't exist yet

        if not fk_info:
            return  # no FK (fresh DB, schema not yet created)

        # Check if any FK still uses CASCADE on delete
        needs_migration = any(
            dict(row).get("on_delete", "").upper() == "CASCADE"
            for row in fk_info
        )
        if not needs_migration:
            return

        logger.info("Migrating action_items FK from CASCADE to SET NULL")
        self.conn.execute("PRAGMA foreign_keys=OFF")
        try:
            self.conn.execute("""\
                CREATE TABLE action_items_new (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    note_id TEXT REFERENCES notes(id) ON DELETE SET NULL,
                    owner TEXT NOT NULL,
                    task TEXT NOT NULL,
                    due TEXT,
                    status TEXT NOT NULL DEFAULT 'open',
                    created TEXT NOT NULL,
                    flagged_date TEXT,
                    space TEXT,
                    group_name TEXT
                )""")
            self.conn.execute("""\
                INSERT INTO action_items_new
                    (id, note_id, owner, task, due, status, created, flagged_date, space, group_name)
                SELECT id, note_id, owner, task, due, status, created, flagged_date, space, group_name
                FROM action_items""")
            self.conn.execute("DROP TABLE action_items")
            self.conn.execute("ALTER TABLE action_items_new RENAME TO action_items")
            # Recreate indexes
            self.conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_action_items_status ON action_items(status, due)")
            self.conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_action_items_note ON action_items(note_id)")
            self.conn.commit()
        finally:
            self.conn.execute("PRAGMA foreign_keys=ON")

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None

    def __enter__(self) -> "Database":
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    # --- CRUD ---

    @staticmethod
    def _hash_raw(raw_text: str) -> str | None:
        """SHA256 hash of full raw text for exact duplicate detection."""
        if not raw_text or not raw_text.strip():
            return None
        import hashlib
        return hashlib.sha256(raw_text.strip().encode()).hexdigest()

    @staticmethod
    def _hash_snippet(raw_text: str) -> str | None:
        """SHA256 hash of first 300 chars for near-duplicate detection."""
        if not raw_text or not raw_text.strip():
            return None
        import hashlib
        snippet = raw_text.strip()[:SNIPPET_LENGTH]
        return hashlib.sha256(snippet.encode()).hexdigest()

    def upsert_note(self, note: Note, hash_source: str | None = None) -> None:
        """Upsert a note into the DB.

        hash_source: if provided, use this text for raw_hash/snippet_hash
        instead of note.raw_text. Used when paste content should be hashed
        separately from typed context.
        """
        body_preview = note.body[:BODY_PREVIEW_LENGTH] if note.body else ""
        raw_file = None
        if note.raw_text:
            raw_file = note.file_path.replace(".md", ".txt")
        source_text = hash_source if hash_source is not None else note.raw_text
        raw_hash = self._hash_raw(source_text)
        snippet_hash = self._hash_snippet(source_text)

        self.conn.execute(
            """INSERT INTO notes
               (id, space, title, source, refinement, input_size, raw_file, raw_hash,
                snippet_hash, date, created, updated, summary, tags, participants,
                file_path, body_preview, space_metadata, source_url)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET
                 title=excluded.title, source=excluded.source,
                 raw_file=excluded.raw_file,
                 raw_hash=COALESCE(excluded.raw_hash, raw_hash),
                 snippet_hash=COALESCE(excluded.snippet_hash, snippet_hash),
                 date=excluded.date,
                 updated=excluded.updated, summary=excluded.summary,
                 tags=excluded.tags, participants=excluded.participants,
                 file_path=excluded.file_path, body_preview=excluded.body_preview,
                 space_metadata=excluded.space_metadata,
                 source_url=excluded.source_url""",
            (
                note.id, note.space, note.title, note.source,
                note.refinement.value, note.input_size.value, raw_file, raw_hash,
                snippet_hash,
                note.date, note.created, note.updated, note.summary,
                json.dumps(note.tags), json.dumps(note.participants),
                note.file_path, body_preview, json.dumps(note.space_metadata),
                note.source_url,
            ),
        )

        # Upsert cross-references
        self.conn.execute("DELETE FROM cross_refs WHERE note_id = ?", (note.id,))
        for ref in note.related_contexts:
            self.conn.execute(
                "INSERT INTO cross_refs (note_id, target_path) VALUES (?, ?)",
                (note.id, ref),
            )

        self.conn.commit()

    def find_exact_duplicate(self, raw_text: str) -> dict[str, Any] | None:
        """Check if exact raw text has been seen before."""
        h = self._hash_raw(raw_text)
        if not h:
            return None
        row = self.conn.execute(
            "SELECT id, title, date, file_path, space FROM notes WHERE raw_hash = ?",
            (h,),
        ).fetchone()
        return dict(row) if row else None

    def find_snippet_match(self, raw_text: str) -> dict[str, Any] | None:
        """Check if the first 300 chars match an existing note (near-duplicate)."""
        h = self._hash_snippet(raw_text)
        if not h:
            return None
        row = self.conn.execute(
            "SELECT id, title, date, file_path, space FROM notes WHERE snippet_hash = ?",
            (h,),
        ).fetchone()
        return dict(row) if row else None

    def delete_note(self, note_id: str) -> None:
        """Delete a note and its cross-refs from the DB.

        Todos linked to this note survive with note_id set to NULL
        (ON DELETE SET NULL on snippets FK), becoming standalone todos.
        """
        self.conn.execute("DELETE FROM cross_refs WHERE note_id = ?", (note_id,))
        self.conn.execute("DELETE FROM notes WHERE id = ?", (note_id,))
        self.conn.commit()

    def get_note(self, note_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM notes WHERE id = ?", (note_id,)
        ).fetchone()
        if row is None:
            return None
        return dict(row)

    def get_note_cross_refs(self, note_id: str) -> list[str]:
        rows = self.conn.execute(
            "SELECT target_path FROM cross_refs WHERE note_id = ?",
            (note_id,),
        ).fetchall()
        return [r["target_path"] for r in rows]

    # --- Search ---

    def search(
        self,
        text_query: str | None = None,
        filters: SearchFilters | None = None,
        limit: int = 20,
        sort_by: str = "recency",
    ) -> list[dict[str, Any]]:
        """Search notes with optional FTS and structured filters."""
        if text_query:
            return self._fts_search(text_query, filters, limit, sort_by)
        return self._filtered_search(filters, limit, sort_by)

    def _fts_search(
        self,
        text_query: str,
        filters: SearchFilters | None,
        limit: int,
        sort_by: str,
    ) -> list[dict[str, Any]]:
        where_clauses = []
        params: list[Any] = []

        # Build filter clauses with table alias for JOIN query
        filter_sql, filter_params = self._build_filter_clauses(filters, prefix="n.")
        where_clauses.extend(filter_sql)
        params.extend(filter_params)

        where = ""
        if where_clauses:
            where = "AND " + " AND ".join(where_clauses)

        order = "ORDER BY rank" if sort_by == "relevance" else "ORDER BY n.date DESC"

        query = f"""
            SELECT n.*, rank
            FROM notes_fts f
            JOIN notes n ON n.rowid = f.rowid
            WHERE notes_fts MATCH ?
            {where}
            {order}
            LIMIT ?
        """
        params = [text_query] + params + [limit]
        rows = self.conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]

    def _filtered_search(
        self,
        filters: SearchFilters | None,
        limit: int,
        sort_by: str,
    ) -> list[dict[str, Any]]:
        where_clauses = []
        params: list[Any] = []

        filter_sql, filter_params = self._build_filter_clauses(filters)
        where_clauses.extend(filter_sql)
        params.extend(filter_params)

        where = ""
        if where_clauses:
            where = "WHERE " + " AND ".join(where_clauses)

        order = "ORDER BY date DESC"
        if sort_by == "created":
            order = "ORDER BY created DESC"

        query = f"SELECT * FROM notes {where} {order} LIMIT ?"
        params.append(limit)
        rows = self.conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]

    def _build_filter_clauses(
        self, filters: SearchFilters | None, prefix: str = ""
    ) -> tuple[list[str], list[Any]]:
        clauses: list[str] = []
        params: list[Any] = []

        if not filters:
            return clauses, params

        if filters.space:
            clauses.append(f"{prefix}space = ?")
            params.append(filters.space)

        if filters.folder:
            # folder = group_slug; filter by file_path prefix (space/group_slug/%)
            # Requires space to be set for a meaningful path prefix
            if filters.space:
                clauses.append(f"{prefix}file_path LIKE ?")
                params.append(f"{filters.space}/{filters.folder}/%")
            else:
                clauses.append(f"{prefix}file_path LIKE ?")
                params.append(f"%/{filters.folder}/%")

        if filters.source:
            clauses.append(f"{prefix}source = ?")
            params.append(filters.source)

        if filters.date_from:
            clauses.append(f"{prefix}date >= ?")
            params.append(filters.date_from)

        if filters.date_to:
            clauses.append(f"{prefix}date <= ?")
            params.append(filters.date_to)

        if filters.tags:
            for tag in filters.tags:
                clauses.append(f"{prefix}tags LIKE ?")
                params.append(f'%"{tag}"%')

        if filters.refinement:
            placeholders = ",".join("?" for _ in filters.refinement)
            clauses.append(f"{prefix}refinement IN ({placeholders})")
            params.extend(filters.refinement)

        # Space-specific metadata filters
        if filters.client:
            clauses.append(f"json_extract({prefix}space_metadata, '$.client') = ?")
            params.append(filters.client)

        if filters.topic:
            clauses.append(f"json_extract({prefix}space_metadata, '$.topic') = ?")
            params.append(filters.topic)

        if filters.category:
            clauses.append(f"json_extract({prefix}space_metadata, '$.category') = ?")
            params.append(filters.category)

        if filters.content_status:
            clauses.append(f"json_extract({prefix}space_metadata, '$.content_status') = ?")
            params.append(filters.content_status)

        return clauses, params

    # --- Taxonomy ---

    def get_groups(self, space: str, group_field: str) -> list[dict[str, Any]]:
        """Get all groups (clients/categories) for a space with counts."""
        rows = self.conn.execute(
            """SELECT
                 json_extract(space_metadata, ?) as grp,
                 json_extract(space_metadata, ?) as grp_display,
                 COUNT(*) as note_count,
                 MAX(date) as last_note
               FROM notes
               WHERE space = ? AND grp IS NOT NULL
               GROUP BY grp
               ORDER BY last_note DESC""",
            (f"$.{group_field}", f"$.{group_field}_display", space),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_subgroups(
        self, space: str, group_field: str, group_value: str, subgroup_field: str
    ) -> list[dict[str, Any]]:
        """Get subgroups within a group."""
        rows = self.conn.execute(
            """SELECT
                 json_extract(space_metadata, ?) as subgrp,
                 json_extract(space_metadata, ?) as subgrp_display,
                 COUNT(*) as note_count,
                 MAX(date) as last_note
               FROM notes
               WHERE space = ?
                 AND json_extract(space_metadata, ?) = ?
                 AND subgrp IS NOT NULL
               GROUP BY subgrp
               ORDER BY last_note DESC""",
            (
                f"$.{subgroup_field}", f"$.{subgroup_field}_display",
                space, f"$.{group_field}", group_value,
            ),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_todays_notes(self, today: str, space: str | None = None) -> list[dict[str, Any]]:
        """Get notes created today, optionally filtered by space."""
        if space:
            rows = self.conn.execute(
                "SELECT id, title, space, space_metadata FROM notes WHERE date = ? AND space = ?",
                (today, space),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT id, title, space, space_metadata FROM notes WHERE date = ?",
                (today,),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_notes_in_space(
        self, space: str, limit: int = 15
    ) -> list[dict[str, Any]]:
        """Get recent notes in a space (lightweight — id, title, date, metadata)."""
        rows = self.conn.execute(
            "SELECT id, title, date, space_metadata FROM notes WHERE space = ? ORDER BY date DESC LIMIT ?",
            (space, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_ideas(
        self,
        space: str,
        status: str | None = None,
        category: str | None = None,
        tag: str | None = None,
    ) -> list[dict[str, Any]]:
        """Get notes from an ideas space with optional filters."""
        where = ["space = ?"]
        params: list[Any] = [space]

        if status:
            where.append("json_extract(space_metadata, '$.content_status') = ?")
            params.append(status)
        if category:
            where.append("json_extract(space_metadata, '$.category') = ?")
            params.append(category)
        if tag:
            where.append("tags LIKE ?")
            params.append(f'%"{tag}"%')

        rows = self.conn.execute(
            f"SELECT id, title, date, summary, tags, space_metadata, source FROM notes WHERE {' AND '.join(where)} ORDER BY date DESC",
            params,
        ).fetchall()
        return [dict(r) for r in rows]

    def get_recent_notes_in_group(
        self, space: str, group_field: str, group_value: str, limit: int = 5
    ) -> list[dict[str, Any]]:
        """Get recent note titles within a group for taxonomy context."""
        rows = self.conn.execute(
            """SELECT id, title, date, summary
               FROM notes
               WHERE space = ?
                 AND json_extract(space_metadata, ?) = ?
               ORDER BY date DESC
               LIMIT ?""",
            (space, f"$.{group_field}", group_value, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_space_stats(self) -> dict[str, dict[str, int]]:
        """Get note counts per space."""
        rows = self.conn.execute(
            "SELECT space, COUNT(*) as count FROM notes GROUP BY space"
        ).fetchall()
        return {r["space"]: {"count": r["count"]} for r in rows}

    def count_notes(self) -> int:
        row = self.conn.execute("SELECT COUNT(*) as c FROM notes").fetchone()
        return row["c"] if row else 0

    def clear_all(self) -> None:
        """Delete all data — used by reindex.

        Preserves standalone todos (note_id IS NULL in snippets) since they have no
        markdown source to rebuild from.
        """
        self.conn.execute(
            "DELETE FROM snippets WHERE snippet_type = 'todo' AND note_id IS NOT NULL"
        )
        self.conn.execute("DELETE FROM cross_refs")
        self.conn.execute("DELETE FROM notes")
        self.conn.commit()

    def find_notes_by_cross_ref(self, target_path: str) -> list[dict[str, Any]]:
        """Find notes that have a cross-reference to the given path."""
        rows = self.conn.execute(
            """SELECT n.* FROM notes n
               JOIN cross_refs c ON c.note_id = n.id
               WHERE c.target_path LIKE ?
               ORDER BY n.date DESC""",
            (f"{target_path}%",),
        ).fetchall()
        return [dict(r) for r in rows]

    def prune_missing(self, config: "NotelyConfig") -> int:
        """Remove DB entries for notes whose .md files no longer exist.

        Called on startup to keep the DB in sync with the filesystem.
        - .md deleted → auto-delete .raw file + remove from DB
        - .raw deleted → just clear the raw_file reference (note is fine)
        Returns the number of pruned entries.
        """
        from .storage import raw_file_path

        rows = self.conn.execute(
            "SELECT id, file_path, raw_file FROM notes"
        ).fetchall()
        pruned = 0
        for row in rows:
            md_path = config.notes_dir / row["file_path"]
            if not md_path.exists():
                # .md is gone — clean up .raw file + binary originals + vector + DB
                raw = raw_file_path(config, row["file_path"])
                raw_dir = raw.parent
                stem = Path(row["file_path"]).stem
                # Remove .txt raw
                if raw.exists():
                    raw.unlink()
                # Remove binary originals (same stem, any extension)
                if raw_dir.is_dir():
                    for f in raw_dir.glob(f"{stem}.*"):
                        f.unlink()
                try:
                    from .vectors import try_vector_delete_note
                    try_vector_delete_note(config, row["id"])
                except Exception:
                    pass
                self.delete_note(row["id"])
                pruned += 1
            elif row["raw_file"]:
                # .md exists but check if .raw is still there
                raw = raw_file_path(config, row["file_path"])
                if not raw.exists():
                    # .raw was deleted — just clear the reference
                    self.conn.execute(
                        "UPDATE notes SET raw_file = NULL WHERE id = ?",
                        (row["id"],),
                    )
                    self.conn.commit()
        return pruned

    def backfill_raw_hashes(self, config: "NotelyConfig") -> int:
        """Backfill raw_hash and snippet_hash for notes missing them.

        Called on startup to ensure duplicate detection works for notes
        saved before hashing was added.
        """
        from .storage import raw_file_path

        rows = self.conn.execute(
            "SELECT id, file_path FROM notes "
            "WHERE (raw_hash IS NULL OR snippet_hash IS NULL) AND raw_file IS NOT NULL"
        ).fetchall()
        filled = 0
        for row in rows:
            raw = raw_file_path(config, row["file_path"])
            if raw.exists():
                raw_text = raw.read_text(encoding="utf-8")
                h = self._hash_raw(raw_text)
                sh = self._hash_snippet(raw_text)
                if h or sh:
                    self.conn.execute(
                        "UPDATE notes SET raw_hash = COALESCE(raw_hash, ?), "
                        "snippet_hash = COALESCE(snippet_hash, ?) WHERE id = ?",
                        (h, sh, row["id"]),
                    )
                    filled += 1
        if filled:
            self.conn.commit()
        return filled

    def resync_from_files(self, config: "NotelyConfig") -> tuple[int, int]:
        """Re-read all .md files from disk and update the DB.

        Handles: edited files, deleted files, manually added files.
        Returns (updated_count, pruned_count).
        """
        from .storage import read_all_notes

        # First prune missing
        pruned = self.prune_missing(config)

        # Then re-read and upsert all existing files
        notes = read_all_notes(config)
        for note in notes:
            self.upsert_note(note)

        return len(notes), pruned


    def add_standalone_idea(
        self, title: str, summary: str, category: str | None = None,
        tags: list[str] | None = None,
    ) -> str:
        """Add a quick idea as a minimal note."""
        import uuid
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        note_id = uuid.uuid4().hex[:8]

        space_metadata: dict[str, Any] = {"content_status": "seed"}
        if category:
            from slugify import slugify
            space_metadata["category"] = slugify(category)
            space_metadata["category_display"] = category

        self.conn.execute(
            """INSERT INTO notes
               (id, space, title, source, refinement, input_size,
                date, created, updated, summary, tags, participants,
                file_path, body_preview, space_metadata)
               VALUES (?, ?, ?, 'thought', 'raw', 'small',
                       ?, ?, ?, ?, ?, '[]',
                       ?, ?, ?)""",
            (
                note_id, "ideas", title, today, now, now,
                summary, json.dumps(tags or []),
                f"ideas/{note_id}.md", summary[:500],
                json.dumps(space_metadata),
            ),
        )
        self.conn.commit()
        return note_id




    # --- Todos (snippets-based) ---

    def _parse_todo_row(self, row: dict[str, Any]) -> dict[str, Any]:
        """Parse a snippets row into the dict shape callers expect for todos.

        Extracts owner/due from the value JSON and exposes top-level keys:
        id, task, owner, due, status, flagged_date, note_id, space, group_slug.
        """
        value = safe_json_loads(row.get("value"), default={})
        return {
            "id": row["id"],
            "task": row["entity"],
            "owner": value.get("owner", ""),
            "due": value.get("due"),
            "status": row.get("status", "open"),
            "flagged_date": row.get("flagged_date"),
            "note_id": row.get("note_id"),
            "space": row.get("space", ""),
            "group_slug": row.get("group_slug", ""),
            "created": row.get("created", ""),
        }

    def get_todo(self, item_id: int) -> dict[str, Any] | None:
        """Get a single todo by ID from the snippets table."""
        row = self.conn.execute(
            "SELECT * FROM snippets WHERE id = ? AND snippet_type = 'todo'",
            (item_id,),
        ).fetchone()
        if not row:
            return None
        return self._parse_todo_row(dict(row))

    def get_open_todos(
        self, space: str | None = None, group_slug: str | None = None,
    ) -> list[dict[str, Any]]:
        """Get all open todos, optionally filtered by space/folder.

        Returns dicts with top-level: id, task, owner, due, status, flagged_date,
        note_id, space, group_slug, note_title, file_path.
        """
        # Linked todos (have a note_id)
        query_linked = """
            SELECT s.*, n.title as note_title, n.file_path, n.space_metadata
            FROM snippets s
            JOIN notes n ON n.id = s.note_id
            WHERE s.snippet_type = 'todo' AND s.status = 'open'
        """
        # Standalone todos (no note_id)
        query_standalone = """
            SELECT s.*, '(standalone)' as note_title,
                   '' as file_path, '{}' as space_metadata
            FROM snippets s
            WHERE s.snippet_type = 'todo' AND s.status = 'open'
              AND s.note_id IS NULL
        """
        params_linked: list[Any] = []
        params_standalone: list[Any] = []

        if space:
            query_linked += " AND COALESCE(s.space, n.space) = ?"
            params_linked.append(space)
            query_standalone += " AND s.space = ?"
            params_standalone.append(space)

        if group_slug:
            query_linked += " AND n.file_path LIKE ?"
            params_linked.append(f"{space}/{group_slug}/%")
            query_standalone += " AND s.group_slug = ?"
            params_standalone.append(group_slug)

        query = f"""
            SELECT * FROM (
                {query_linked}
                UNION ALL
                {query_standalone}
            ) ORDER BY json_extract(value, '$.due') IS NULL,
                       json_extract(value, '$.due') ASC
        """
        params = params_linked + params_standalone
        rows = self.conn.execute(query, params).fetchall()
        result = []
        for r in rows:
            d = self._parse_todo_row(dict(r))
            d["note_title"] = r["note_title"]
            d["file_path"] = r["file_path"]
            d["space_metadata"] = r["space_metadata"]
            result.append(d)
        return result

    def get_todos_filtered(
        self,
        space: str | None = None,
        client: str | None = None,
        owner: str | None = None,
        show_all: bool = False,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        """Get todos with optional filters, joined with note metadata.

        status overrides show_all — e.g. status='done' returns only done items.
        """
        clauses = []
        params: list[Any] = []

        if status:
            clauses.append("AND s.status = ?")
            params.append(status)
        elif not show_all:
            clauses.append("AND s.status = 'open'")

        if space:
            clauses.append("AND n.space = ?")
            params.append(space)
        if client:
            clauses.append("AND json_extract(n.space_metadata, '$.client') = ?")
            params.append(client)
        if owner:
            clauses.append("AND LOWER(s.value) LIKE ?")
            params.append(f'%"owner"%{owner.lower()}%')

        extra = " ".join(clauses)
        rows = self.conn.execute(
            f"""SELECT s.*, n.id as _note_id, n.title as note_title,
                       n.space as _note_space, n.space_metadata, n.date as note_date
                FROM snippets s
                JOIN notes n ON n.id = s.note_id
                WHERE s.snippet_type = 'todo' {extra}
                ORDER BY
                    CASE s.status WHEN 'open' THEN 0 ELSE 1 END,
                    json_extract(s.value, '$.due') IS NULL,
                    json_extract(s.value, '$.due') ASC, n.date DESC""",
            params,
        ).fetchall()
        result = []
        for r in rows:
            d = self._parse_todo_row(dict(r))
            d["note_title"] = r["note_title"]
            d["note_date"] = r["note_date"]
            d["space_metadata"] = r["space_metadata"]
            result.append(d)
        return result

    def get_note_todos(self, note_id: str) -> list[dict[str, Any]]:
        """Get all todos linked to a specific note."""
        rows = self.conn.execute(
            "SELECT * FROM snippets WHERE note_id = ? AND snippet_type = 'todo' ORDER BY id",
            (note_id,),
        ).fetchall()
        return [self._parse_todo_row(dict(r)) for r in rows]

    def get_note_records(self, note_id: str) -> list[dict[str, Any]]:
        """Get all records (todos + other types) linked to a specific note."""
        rows = self.conn.execute(
            """SELECT * FROM snippets
               WHERE note_id = ? AND entity != '_meta'
               ORDER BY id""",
            (note_id,),
        ).fetchall()
        results = []
        for r in rows:
            row = dict(r)
            if row.get("snippet_type") == "todo":
                results.append(self._parse_todo_row(row))
            else:
                results.append(row)
        return results

    def add_todo(
        self, owner: str, task: str, due: str | None = None,
        space: str | None = None, group_slug: str | None = None,
        note_id: str | None = None,
    ) -> int:
        """Add a todo to the snippets table. Returns the new row ID."""
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        value_dict: dict[str, Any] = {}
        if owner:
            value_dict["owner"] = owner
        if due:
            value_dict["due"] = due
        value_json = json.dumps(value_dict) if value_dict else "{}"

        cursor = self.conn.execute(
            """INSERT INTO snippets
               (space, group_slug, entity, key, value, description,
                snippet_type, tags, created, note_id, status, flagged_date)
               VALUES (?, ?, ?, 'record', ?, '', 'todo', '[]', ?, ?, 'open', NULL)""",
            (space or "", group_slug or "", task, value_json, now, note_id),
        )
        self.conn.commit()
        return cursor.lastrowid

    def add_todos_for_note(
        self,
        note_id: str,
        items: list,
        space: str = "",
        group_slug: str = "",
    ) -> list[int]:
        """Insert todos linked to a source note. Returns list of inserted IDs."""
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        ids = []
        for item in items:
            status_val = item.status.value if hasattr(item.status, "value") else item.status
            value_dict: dict[str, Any] = {}
            if item.owner:
                value_dict["owner"] = item.owner
            if item.due:
                value_dict["due"] = item.due
            value_json = json.dumps(value_dict) if value_dict else "{}"

            cursor = self.conn.execute(
                """INSERT INTO snippets
                   (space, group_slug, entity, key, value, description,
                    snippet_type, tags, created, note_id, status, flagged_date)
                   VALUES (?, ?, ?, 'record', ?, '', 'todo', '[]', ?, ?, ?, NULL)""",
                (space, group_slug, item.task, value_json, now,
                 note_id, status_val),
            )
            ids.append(cursor.lastrowid)
        self.conn.commit()
        return ids

    def update_todo_status(self, item_id: int, status: str) -> None:
        """Update a todo's status in the snippets table."""
        self.conn.execute(
            "UPDATE snippets SET status = ? WHERE id = ? AND snippet_type = 'todo'",
            (status, item_id),
        )
        self.conn.commit()

    def update_todo_folder(
        self, item_id: int, space: str | None, group_slug: str | None,
    ) -> None:
        """Move a standalone todo to a different folder."""
        self.conn.execute(
            "UPDATE snippets SET space = ?, group_slug = ? WHERE id = ? AND snippet_type = 'todo'",
            (space or "", group_slug or "", item_id),
        )
        self.conn.commit()

    def flag_todo_today(self, item_id: int, date_str: str) -> None:
        """Flag a todo for a specific date (YYYY-MM-DD)."""
        self.conn.execute(
            "UPDATE snippets SET flagged_date = ? WHERE id = ? AND snippet_type = 'todo'",
            (date_str, item_id),
        )
        self.conn.commit()

    def unflag_todo_today(self, item_id: int) -> None:
        """Remove the flagged_date from a todo."""
        self.conn.execute(
            "UPDATE snippets SET flagged_date = NULL WHERE id = ? AND snippet_type = 'todo'",
            (item_id,),
        )
        self.conn.commit()

    def update_todo_owner(self, item_id: int, new_owner: str) -> None:
        """Update the owner in a todo's value JSON."""
        row = self.conn.execute(
            "SELECT value FROM snippets WHERE id = ? AND snippet_type = 'todo'",
            (item_id,),
        ).fetchone()
        if not row:
            return
        value = safe_json_loads(row[0], default={})
        value["owner"] = new_owner
        self.conn.execute(
            "UPDATE snippets SET value = ? WHERE id = ? AND snippet_type = 'todo'",
            (json.dumps(value), item_id),
        )
        self.conn.commit()

    def get_folder_for_todo(self, item_id: int) -> tuple[str, str, str] | None:
        """Get (space, group_slug, display) for the folder containing a todo.

        Derives from note's file_path for note-linked items, or from
        standalone space/group_slug fields.
        """
        row = self.conn.execute(
            """SELECT s.note_id, s.space as s_space, s.group_slug,
                      n.file_path, n.space
               FROM snippets s
               LEFT JOIN notes n ON n.id = s.note_id
               WHERE s.id = ? AND s.snippet_type = 'todo'""",
            (item_id,),
        ).fetchone()
        if not row:
            return None

        if row["file_path"]:
            parts = row["file_path"].split("/")
            space = parts[0] if parts else ""
            group_slug = parts[1] if len(parts) > 2 else ""
            display = group_slug.replace("-", " ").title() if group_slug else space
            return (space, group_slug, display)
        elif row["s_space"]:
            space = row["s_space"]
            group = row["group_slug"] or ""
            display = group.replace("-", " ").title() if group else space
            return (space, group, display)
        return None

    # --- Directories ---

    def upsert_directory(
        self,
        dir_id: str,
        space: str,
        group_slug: str,
        display_name: str,
        description: str = "",
        subgroup_slug: str | None = None,
    ) -> None:
        """Insert or update a directory entry."""
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        self.conn.execute(
            """INSERT INTO directories (id, space, group_slug, subgroup_slug,
                   display_name, description, created, updated)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET
                 display_name=excluded.display_name,
                 description=excluded.description,
                 updated=excluded.updated""",
            (dir_id, space, group_slug, subgroup_slug,
             display_name, description, now, now),
        )
        self.conn.commit()

    def get_directory(self, dir_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM directories WHERE id = ?", (dir_id,)
        ).fetchone()
        return dict(row) if row else None

    def get_all_directories(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM directories ORDER BY space, group_slug, subgroup_slug"
        ).fetchall()
        return [dict(r) for r in rows]

    def delete_directory(self, dir_id: str) -> None:
        self.conn.execute("DELETE FROM directories WHERE id = ?", (dir_id,))
        self.conn.commit()

    def get_folder_context(
        self,
        space: str,
        group_slug: str,
    ) -> dict[str, Any]:
        """Get full context for a specific group folder.

        Used by /chat to build the AI system prompt with all folder context.
        Returns notes (with summaries, tags, counts), open todos, and subfolder structure.
        Group is determined by file_path prefix (e.g. 'projects/decipherhealth/%').
        When group_slug is empty, returns all notes in the space (e.g. 'inbox/%').
        """
        if group_slug:
            path_prefix = f"{space}/{group_slug}/%"
        else:
            path_prefix = f"{space}/%"

        # Notes in this group — use file_path LIKE only (space may be empty after reindex)
        rows = self.conn.execute(
            """SELECT n.id, n.title, n.date, n.summary, n.tags, n.participants,
                      n.file_path, n.space_metadata,
                      (SELECT COUNT(*) FROM snippets s WHERE s.note_id = n.id AND s.snippet_type = 'todo') as action_items_count
               FROM notes n
               WHERE n.file_path LIKE ?
               ORDER BY n.date DESC""",
            (path_prefix,),
        ).fetchall()

        notes = []
        for r in rows:
            notes.append({
                "id": r["id"],
                "title": r["title"],
                "date": r["date"],
                "summary": r["summary"],
                "tags": safe_parse_tags(r["tags"]),
                "participants": json.loads(r["participants"]) if isinstance(r["participants"], str) else r["participants"],
                "action_items_count": r["action_items_count"],
                "file_path": r["file_path"],
                "space_metadata": safe_json_loads(r["space_metadata"]),
            })

        # Open todos linked to notes in this group
        todo_rows = self.conn.execute(
            """SELECT s.id, s.entity, s.value, s.status, s.note_id,
                      s.flagged_date, s.space, s.group_slug,
                      n.title as note_title
               FROM snippets s
               JOIN notes n ON n.id = s.note_id
               WHERE s.snippet_type = 'todo' AND s.status = 'open'
                 AND n.file_path LIKE ?
               ORDER BY json_extract(s.value, '$.due') IS NULL,
                        json_extract(s.value, '$.due') ASC""",
            (path_prefix,),
        ).fetchall()

        open_todos = []
        for r in todo_rows:
            d = self._parse_todo_row(dict(r))
            d["note_title"] = r["note_title"]
            open_todos.append(d)

        # Subfolders: subgroups within this group, or groups within this space
        if group_slug:
            sub_rows = self.conn.execute(
                """SELECT id, display_name, description
                   FROM directories
                   WHERE space = ? AND group_slug = ? AND subgroup_slug IS NOT NULL
                   ORDER BY display_name""",
                (space, group_slug),
            ).fetchall()
        else:
            # Space-level: show group-level dirs as subfolders
            sub_rows = self.conn.execute(
                """SELECT id, display_name, description
                   FROM directories
                   WHERE space = ? AND group_slug != '' AND subgroup_slug IS NULL
                   ORDER BY display_name""",
                (space,),
            ).fetchall()
        subfolders = [dict(r) for r in sub_rows]

        # Snippets: folder-scoped + global (unscoped), grouped by database name
        databases: dict[str, list[dict[str, Any]]] = {}
        references: list[dict[str, Any]] = []
        contacts: list[dict[str, Any]] = []
        try:
            ref_rows = self.conn.execute(
                """SELECT * FROM snippets
                   WHERE entity != '_meta'
                     AND ((space = ? AND group_slug = ?)
                          OR (space = '' AND group_slug = ''))
                   ORDER BY entity, key""",
                (space, group_slug),
            ).fetchall()
            for r in ref_rows:
                row = dict(r)
                db_name = row.get("snippet_type", "fact")
                databases.setdefault(db_name, []).append(row)
                # Backward compat: populate legacy keys
                # "contact" (old singular) and "contacts" (new plural) both go to contacts
                if db_name in ("contact", "contacts"):
                    contacts.append(row)
                else:
                    references.append(row)
        except sqlite3.OperationalError:
            pass  # table may not exist yet

        return {
            "notes": notes,
            "open_todos": open_todos,
            "subfolders": subfolders,
            "references": references,
            "contacts": contacts,
            "databases": databases,
        }

    def search_notes_in_group(
        self,
        text_query: str,
        space: str,
        group_slug: str,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """FTS search scoped to a specific group within a space.

        Used by /chat to let the AI search within the current folder.
        Delegates to search() with folder filter.
        """
        filters = SearchFilters(space=space, folder=group_slug)
        return self.search(text_query, filters=filters, limit=limit, sort_by="relevance")

    def update_note_metadata(self, note_id: str, **kwargs: Any) -> None:
        """Update specific fields on a note.

        Only columns listed in _UPDATABLE_NOTE_COLUMNS are accepted.
        Raises ValueError for unknown column names to prevent SQL injection.
        """
        bad_keys = set(kwargs) - _UPDATABLE_NOTE_COLUMNS
        if bad_keys:
            raise ValueError(f"Cannot update unknown column(s): {', '.join(sorted(bad_keys))}")
        set_clauses = []
        params = []
        for key, value in kwargs.items():
            set_clauses.append(f"{key} = ?")
            params.append(value)
        if not set_clauses:
            return
        params.append(note_id)
        self.conn.execute(
            f"UPDATE notes SET {', '.join(set_clauses)} WHERE id = ?",
            params,
        )
        self.conn.commit()

    # --- References (snippets) ---

    def find_similar_entities(
        self, name: str, snippet_type: str, threshold: float = 0.7,
    ) -> list[str]:
        """Find existing entities in a database that look similar to `name`.

        Uses case-insensitive prefix/substring matching and SequenceMatcher
        for fuzzy matching. Returns entity names sorted by similarity.
        """
        from difflib import SequenceMatcher

        try:
            rows = self.conn.execute(
                "SELECT DISTINCT entity FROM snippets "
                "WHERE snippet_type = ? AND entity != '_meta'",
                (snippet_type,),
            ).fetchall()
        except sqlite3.OperationalError:
            return []

        name_lower = name.lower()
        matches: list[tuple[str, float]] = []
        for r in rows:
            existing = r[0]
            existing_lower = existing.lower()
            if existing_lower == name_lower:
                continue  # Exact match, skip
            # Check similarity
            ratio = SequenceMatcher(None, name_lower, existing_lower).ratio()
            # Boost if one contains the other
            if name_lower in existing_lower or existing_lower in name_lower:
                ratio = max(ratio, 0.85)
            if ratio >= threshold:
                matches.append((existing, ratio))

        matches.sort(key=lambda x: -x[1])
        return [m[0] for m in matches]

    def add_reference(
        self,
        space: str = "",
        group_slug: str = "",
        entity: str = "",
        key: str = "",
        value: str = "",
        description: str = "",
        snippet_type: str = "fact",
        tags: list[str] | None = None,
        note_id: str | None = None,
    ) -> int:
        """Add a reference/snippet to the DB. Returns the new row ID."""
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        cursor = self.conn.execute(
            """INSERT INTO snippets (space, group_slug, entity, key, value,
                   description, snippet_type, tags, created, note_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (space, group_slug, entity, key, value,
             description, snippet_type, json.dumps(tags or []), now, note_id),
        )
        self.conn.commit()
        return cursor.lastrowid

    def find_existing_snippet(
        self, entity: str, key: str, snippet_type: str,
    ) -> dict[str, Any] | None:
        """Check if an entity+key combo already exists in a database.

        Returns the existing row as a dict, or None if not found.
        Case-insensitive match on entity and key.
        """
        try:
            row = self.conn.execute(
                "SELECT * FROM snippets "
                "WHERE LOWER(entity) = ? AND LOWER(key) = ? AND snippet_type = ? "
                "LIMIT 1",
                (entity.lower(), key.lower(), snippet_type),
            ).fetchone()
        except sqlite3.OperationalError:
            return None
        return dict(row) if row else None

    def update_snippet(self, snippet_id: int, value: str, description: str = "") -> None:
        """Update an existing snippet's value (and optionally description)."""
        self.conn.execute(
            "UPDATE snippets SET value = ?, description = ? WHERE id = ?",
            (value, description, snippet_id),
        )
        self.conn.commit()

    def get_references(
        self,
        space: str | None = None,
        group_slug: str | None = None,
        entity: str | None = None,
        exclude_type: str | None = None,
    ) -> list[dict[str, Any]]:
        """Get references with optional filters.

        Args:
            exclude_type: exclude snippets of this type (e.g. "contact")
        """
        clauses = ["entity != '_meta'"]
        params: list[Any] = []
        if space is not None:
            clauses.append("space = ?")
            params.append(space)
        if group_slug is not None:
            clauses.append("group_slug = ?")
            params.append(group_slug)
        if entity is not None:
            clauses.append("LOWER(entity) = ?")
            params.append(entity.lower())
        if exclude_type is not None:
            clauses.append("snippet_type != ?")
            params.append(exclude_type)
        where = f"WHERE {' AND '.join(clauses)}"
        rows = self.conn.execute(
            f"SELECT * FROM snippets {where} ORDER BY entity, key",
            params,
        ).fetchall()
        return [dict(r) for r in rows]

    def search_references(
        self,
        query: str,
        space: str | None = None,
        group_slug: str | None = None,
    ) -> list[dict[str, Any]]:
        """FTS search across references."""
        clauses = []
        params: list[Any] = [query]
        if space is not None:
            clauses.append("r.space = ?")
            params.append(space)
        if group_slug is not None:
            clauses.append("r.group_slug = ?")
            params.append(group_slug)
        extra = ""
        if clauses:
            extra = "AND " + " AND ".join(clauses)
        rows = self.conn.execute(
            f"""SELECT r.* FROM snippets_fts f
                JOIN snippets r ON r.rowid = f.rowid
                WHERE snippets_fts MATCH ? {extra}
                ORDER BY rank LIMIT 20""",
            params,
        ).fetchall()
        return [dict(r) for r in rows]

    def delete_reference(self, ref_id: int) -> bool:
        """Delete a reference by ID. Returns True if found."""
        cursor = self.conn.execute(
            "DELETE FROM snippets WHERE id = ?", (ref_id,),
        )
        self.conn.commit()
        return cursor.rowcount > 0

    def get_folder_references(
        self, space: str, group_slug: str,
    ) -> list[dict[str, Any]]:
        """Get all references in a folder — for /chat context."""
        return self.get_references(space=space, group_slug=group_slug)

    def count_references(self) -> int:
        """Count total references in DB."""
        try:
            row = self.conn.execute("SELECT COUNT(*) as c FROM snippets").fetchone()
            return row["c"] if row else 0
        except sqlite3.OperationalError:
            return 0

    # --- User-Defined Databases (generalized snippets) ---

    def database_exists(self, name: str) -> bool:
        """Check if a database (snippet_type) has any records or is a default."""
        if name in self.DEFAULT_DATABASES:
            return True
        try:
            row = self.conn.execute(
                "SELECT 1 FROM snippets WHERE snippet_type = ? LIMIT 1", (name,)
            ).fetchone()
            return row is not None
        except sqlite3.OperationalError:
            return False

    def get_database_keys(self, name: str) -> list[str]:
        """Return distinct key names used in a database, ordered by frequency."""
        try:
            rows = self.conn.execute(
                "SELECT key, COUNT(*) as cnt FROM snippets "
                "WHERE snippet_type = ? AND entity != '_meta' "
                "GROUP BY key ORDER BY cnt DESC",
                (name,),
            ).fetchall()
            return [r[0] for r in rows]
        except sqlite3.OperationalError:
            return []

    def set_database_description(self, name: str, description: str) -> None:
        """Set a human-readable description for a database."""
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        # Use a _meta entity to store database-level metadata
        self.conn.execute(
            "DELETE FROM snippets WHERE snippet_type = ? AND entity = '_meta' AND key = 'description'",
            (name,),
        )
        self.conn.execute(
            "INSERT INTO snippets (snippet_type, entity, key, value, space, group_slug, created) "
            "VALUES (?, '_meta', 'description', ?, '', '', ?)",
            (name, description, now),
        )
        self.conn.commit()

    def get_database_description(self, name: str) -> str | None:
        """Get the human-readable description for a database."""
        try:
            row = self.conn.execute(
                "SELECT value FROM snippets "
                "WHERE snippet_type = ? AND entity = '_meta' AND key = 'description'",
                (name,),
            ).fetchone()
            return row[0] if row else None
        except sqlite3.OperationalError:
            return None

    def set_database_fields(self, name: str, fields: list[str]) -> None:
        """Set expected field names for a database (stored as _meta)."""
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        self.conn.execute(
            "DELETE FROM snippets WHERE snippet_type = ? AND entity = '_meta' AND key = 'fields'",
            (name,),
        )
        self.conn.execute(
            "INSERT INTO snippets (snippet_type, entity, key, value, space, group_slug, created) "
            "VALUES (?, '_meta', 'fields', ?, '', '', ?)",
            (name, ",".join(fields), now),
        )
        self.conn.commit()

    def get_database_fields(self, name: str) -> list[str]:
        """Get expected field names for a database. Falls back to discovered keys."""
        try:
            row = self.conn.execute(
                "SELECT value FROM snippets "
                "WHERE snippet_type = ? AND entity = '_meta' AND key = 'fields'",
                (name,),
            ).fetchone()
            if row and row[0]:
                return [f.strip() for f in row[0].split(",") if f.strip()]
        except sqlite3.OperationalError:
            pass
        # Fall back to keys discovered from actual records
        return self.get_database_keys(name)

    def set_database_meta(self, name: str, key: str, value: str) -> None:
        """Set a _meta key/value for a database."""
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        self.conn.execute(
            "DELETE FROM snippets WHERE snippet_type = ? AND entity = '_meta' AND key = ?",
            (name, key),
        )
        self.conn.execute(
            "INSERT INTO snippets (snippet_type, entity, key, value, space, group_slug, created) "
            "VALUES (?, '_meta', ?, ?, '', '', ?)",
            (name, key, value, now),
        )
        self.conn.commit()

    def get_database_meta(self, name: str, key: str) -> str | None:
        """Get a _meta value for a database."""
        try:
            row = self.conn.execute(
                "SELECT value FROM snippets "
                "WHERE snippet_type = ? AND entity = '_meta' AND key = ?",
                (name, key),
            ).fetchone()
            return row[0] if row else None
        except sqlite3.OperationalError:
            return None

    def delete_database(self, name: str) -> int:
        """Delete ALL records for a database (snippet_type). Returns count deleted."""
        cursor = self.conn.execute(
            "DELETE FROM snippets WHERE snippet_type = ?", (name,)
        )
        self.conn.commit()
        return cursor.rowcount

    # Default databases that always appear even with no records.
    DEFAULT_DATABASES = {"fact", "todo"}

    def get_database_names(self) -> list[str]:
        """Return all distinct snippet_type values (= database names).

        Always includes default databases (fact, todo) even if they have
        no records yet.
        """
        names = set(self.DEFAULT_DATABASES)
        try:
            rows = self.conn.execute(
                "SELECT DISTINCT snippet_type FROM snippets ORDER BY snippet_type"
            ).fetchall()
            names |= {r[0] for r in rows}
        except sqlite3.OperationalError:
            pass
        return sorted(names)

    def get_database_records(
        self,
        db_name: str,
        space: str | None = None,
        group_slug: str | None = None,
        entity: str | None = None,
    ) -> list[dict[str, Any]]:
        """Get all records in a user-defined database (by snippet_type)."""
        clauses = ["snippet_type = ?", "entity != '_meta'"]
        params: list[Any] = [db_name]
        if space is not None:
            clauses.append("space = ?")
            params.append(space)
        if group_slug is not None:
            clauses.append("group_slug = ?")
            params.append(group_slug)
        if entity is not None:
            clauses.append("LOWER(entity) = ?")
            params.append(entity.lower())
        where = f"WHERE {' AND '.join(clauses)}"
        rows = self.conn.execute(
            f"SELECT * FROM snippets {where} ORDER BY entity, key",
            params,
        ).fetchall()
        return [dict(r) for r in rows]

    # --- Contacts (snippet_type = 'contact') ---

    def get_contacts(
        self,
        space: str | None = None,
        group_slug: str | None = None,
        entity: str | None = None,
    ) -> list[dict[str, Any]]:
        """Get contact snippets with optional filters."""
        clauses = ["snippet_type IN ('contact', 'contacts')"]
        params: list[Any] = []
        if space is not None:
            clauses.append("space = ?")
            params.append(space)
        if group_slug is not None:
            clauses.append("group_slug = ?")
            params.append(group_slug)
        if entity is not None:
            clauses.append("LOWER(entity) = ?")
            params.append(entity.lower())
        where = f"WHERE {' AND '.join(clauses)}"
        rows = self.conn.execute(
            f"SELECT * FROM snippets {where} ORDER BY entity, key",
            params,
        ).fetchall()
        return [dict(r) for r in rows]

    def get_contact_interactions(
        self,
        entity: str,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        """Get recent notes where a person appears as a participant.

        Uses json_each() to search the participants JSON array.
        """
        rows = self.conn.execute(
            """SELECT n.id, n.title, n.date, n.file_path
               FROM notes n, json_each(n.participants) p
               WHERE LOWER(p.value) LIKE ?
               ORDER BY n.date DESC
               LIMIT ?""",
            (f"%{entity.lower()}%", limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def migrate_references_toml(self, config: "NotelyConfig") -> int:
        """Migrate references from references.toml to DB.

        Runs once on startup if references.toml exists and DB has 0 references.
        After migration, renames to references.toml.bak.
        Returns number of migrated entries.
        """
        refs_path = config.references_path
        if not refs_path.exists():
            return 0

        # Only migrate if DB has no references yet
        if self.count_references() > 0:
            return 0

        try:
            from .secrets import SecretsStore
            store = SecretsStore(refs_path)
            all_refs = store.get_all()
        except Exception:
            return 0

        if not all_refs:
            return 0

        count = 0
        for entity, kvs in all_refs.items():
            for key, value in kvs.items():
                self.add_reference(
                    entity=entity, key=key, value=str(value),
                )
                count += 1

        # Rename to .bak so it doesn't re-migrate
        if count > 0:
            bak = refs_path.with_suffix(".toml.bak")
            refs_path.rename(bak)
            logger.info("Migrated %d references from TOML to DB (backup: %s)", count, bak)

        return count

    def migrate_action_items_to_snippets(self) -> int:
        """Migrate action_items rows into the snippets table as snippet_type='todo'.

        Idempotent — skips if action_items table doesn't exist or if snippets
        already has todo rows. After migration, renames action_items → action_items_bak.
        Returns number of migrated entries.
        """
        # Check if action_items table exists
        table_check = self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='action_items'"
        ).fetchone()
        if not table_check:
            return 0  # already migrated or fresh DB

        # Skip if snippets already has todo rows (already migrated)
        try:
            existing = self.conn.execute(
                "SELECT 1 FROM snippets WHERE snippet_type = 'todo' LIMIT 1"
            ).fetchone()
            if existing:
                return 0
        except sqlite3.OperationalError:
            pass  # snippets table may not exist yet

        # Read all action_items
        try:
            rows = self.conn.execute("SELECT * FROM action_items").fetchall()
        except sqlite3.OperationalError:
            return 0

        if not rows:
            # Empty table — just rename
            self.conn.execute("ALTER TABLE action_items RENAME TO action_items_bak")
            self.conn.commit()
            logger.info("No action items to migrate, renamed table to action_items_bak")
            return 0

        count = 0
        for row in rows:
            row = dict(row)
            # Derive space/group_slug from note's file_path or standalone fields
            space = row.get("space") or ""
            group_slug = row.get("group_name") or ""

            # If note-linked, try to derive better space/group from note's file_path
            if row.get("note_id"):
                note_row = self.conn.execute(
                    "SELECT file_path, title FROM notes WHERE id = ?",
                    (row["note_id"],),
                ).fetchone()
                if note_row:
                    parts = note_row["file_path"].split("/")
                    space = parts[0] if parts else space
                    group_slug = parts[1] if len(parts) > 2 else group_slug

            # Build JSON value with todo fields
            value_dict: dict[str, Any] = {}
            if row.get("owner"):
                value_dict["owner"] = row["owner"]
            if row.get("due"):
                value_dict["due"] = row["due"]
            value_json = json.dumps(value_dict) if value_dict else "{}"

            self.conn.execute(
                """INSERT INTO snippets
                   (space, group_slug, entity, key, value, description,
                    snippet_type, tags, created, note_id, status, flagged_date)
                   VALUES (?, ?, ?, 'record', ?, '', 'todo', '[]', ?, ?, ?, ?)""",
                (
                    space,
                    group_slug,
                    row["task"],  # entity = task text
                    value_json,
                    row.get("created") or "",
                    row.get("note_id"),
                    row.get("status", "open"),
                    row.get("flagged_date"),
                ),
            )
            count += 1

        # Rename old table
        self.conn.execute("ALTER TABLE action_items RENAME TO action_items_bak")
        self.conn.commit()
        logger.info("Migrated %d action items to snippets table (backup: action_items_bak)", count)

        return count

    # --- Inbox ---

    def upsert_inbox_item(self, item: "InboxItem") -> None:
        """Insert or update an inbox item. Dedup by (source, source_id) when source_id is set.

        If source_id is non-empty and a row with the same (source, source_id)
        already exists, updates that row instead of inserting a new one.
        """
        # If source_id is set, check for existing row with same source+source_id
        # and reuse its id to avoid UNIQUE constraint violation
        actual_id = item.id
        if item.source_id:
            existing = self.conn.execute(
                "SELECT id FROM inbox WHERE source = ? AND source_id = ?",
                (item.source, item.source_id),
            ).fetchone()
            if existing:
                actual_id = existing["id"]

        self.conn.execute(
            """INSERT INTO inbox
               (id, source, source_id, type, title, summary, body, tags,
                participants, action_items, source_url, metadata,
                suggested_space, suggested_group, status, created,
                reviewed_at, filed_note_id, processed)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET
                 title=excluded.title, summary=excluded.summary, body=excluded.body,
                 tags=excluded.tags, participants=excluded.participants,
                 action_items=excluded.action_items, source_url=excluded.source_url,
                 metadata=excluded.metadata, suggested_space=excluded.suggested_space,
                 suggested_group=excluded.suggested_group,
                 processed=excluded.processed""",
            (
                actual_id, item.source, item.source_id, item.type,
                item.title, item.summary, item.body,
                json.dumps(item.tags), json.dumps(item.participants),
                json.dumps([a.model_dump() for a in item.action_items]),
                item.source_url, json.dumps(item.metadata),
                item.suggested_space, item.suggested_group,
                item.status.value, item.created,
                item.reviewed_at, item.filed_note_id,
                int(item.processed),
            ),
        )
        self.conn.commit()

    def get_inbox_items(self, status: str | None = None) -> list[dict[str, Any]]:
        """List inbox items, optionally filtered by status."""
        if status:
            rows = self.conn.execute(
                "SELECT * FROM inbox WHERE status = ? ORDER BY created DESC",
                (status,),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM inbox ORDER BY created DESC",
            ).fetchall()
        return [dict(r) for r in rows]

    def get_inbox_item(self, item_id: str) -> dict[str, Any] | None:
        """Get a single inbox item by ID."""
        row = self.conn.execute(
            "SELECT * FROM inbox WHERE id = ?", (item_id,),
        ).fetchone()
        return dict(row) if row else None

    def update_inbox_status(
        self, item_id: str, status: str, filed_note_id: str = "",
    ) -> None:
        """Update an inbox item's status and optional filed_note_id."""
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        self.conn.execute(
            """UPDATE inbox SET status = ?, reviewed_at = ?, filed_note_id = ?
               WHERE id = ?""",
            (status, now, filed_note_id, item_id),
        )
        self.conn.commit()

    def delete_inbox_item(self, item_id: str) -> None:
        """Hard delete an inbox item."""
        self.conn.execute("DELETE FROM inbox WHERE id = ?", (item_id,))
        self.conn.commit()

    def count_inbox(self, status: str = "pending") -> int:
        """Count inbox items by status."""
        try:
            row = self.conn.execute(
                "SELECT COUNT(*) as c FROM inbox WHERE status = ?", (status,),
            ).fetchone()
            return row["c"] if row else 0
        except sqlite3.OperationalError:
            return 0

    def cleanup_inbox(self, days: int = 30) -> int:
        """Delete filed/skipped inbox items older than N days. Returns count deleted."""
        from datetime import datetime, timezone, timedelta
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        cursor = self.conn.execute(
            """DELETE FROM inbox
               WHERE status IN ('filed', 'skipped') AND reviewed_at != '' AND reviewed_at < ?""",
            (cutoff,),
        )
        self.conn.commit()
        return cursor.rowcount
