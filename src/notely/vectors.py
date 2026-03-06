"""LanceDB vector store for semantic routing."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Protocol

import pyarrow as pa

logger = logging.getLogger(__name__)


# Sampling counts for directory description enrichment
SAMPLE_RECENT = 5   # Most recent notes to include
SAMPLE_OLDER = 5    # Random older notes to include
RAW_SNIPPET_LENGTH = 300  # Chars of raw text embedded alongside title+summary


def _escape_where_value(value: str) -> str:
    """Escape a string value for use in a LanceDB WHERE clause."""
    return value.replace("\\", "\\\\").replace('"', '\\"')


class EmbeddingProvider(Protocol):
    """Protocol for embedding providers."""

    def embed(self, texts: list[str]) -> list[list[float]]: ...


class FastEmbedProvider:
    """Local embeddings via fastembed (BAAI/bge-small-en-v1.5, ~33MB)."""

    def __init__(self, model_name: str = "BAAI/bge-small-en-v1.5") -> None:
        self._model_name = model_name
        self._model: Any = None

    def _get_model(self) -> Any:
        if self._model is None:
            from fastembed import TextEmbedding

            self._model = TextEmbedding(model_name=self._model_name)
        return self._model

    def embed(self, texts: list[str]) -> list[list[float]]:
        model = self._get_model()
        embeddings = list(model.embed(texts))
        return [e.tolist() for e in embeddings]


# LanceDB table schemas
_DIR_SCHEMA = pa.schema([
    pa.field("id", pa.utf8()),
    pa.field("space", pa.utf8()),
    pa.field("group_slug", pa.utf8()),
    pa.field("subgroup_slug", pa.utf8()),
    pa.field("display_name", pa.utf8()),
    pa.field("description", pa.utf8()),
    pa.field("note_count", pa.int32()),
    pa.field("last_note_date", pa.utf8()),
    pa.field("vector", pa.list_(pa.float32(), 384)),
])

_NOTE_SCHEMA = pa.schema([
    pa.field("note_id", pa.utf8()),
    pa.field("space", pa.utf8()),
    pa.field("group_slug", pa.utf8()),
    pa.field("subgroup_slug", pa.utf8()),
    pa.field("title", pa.utf8()),
    pa.field("summary", pa.utf8()),
    pa.field("date", pa.utf8()),
    pa.field("tags", pa.utf8()),
    pa.field("vector", pa.list_(pa.float32(), 384)),
])


def sample_note_summaries(notes: list[dict[str, Any]]) -> str:
    """Sample note summaries: 5 most recent + up to 5 random older ones.

    Returns a semicolon-joined string of "title: summary" pairs for embedding.
    Shared by vectors.py (directory descriptions) and routing.py (refresh).
    """
    import random

    if not notes:
        return ""

    recent = notes[:SAMPLE_RECENT]
    older = notes[SAMPLE_RECENT:]
    sampled_older = random.sample(older, min(SAMPLE_OLDER, len(older))) if older else []
    sampled = recent + sampled_older

    parts = []
    for n in sampled:
        title = n.get("title", "")
        summary = n.get("summary", "")
        if title and summary:
            parts.append(f"{title}: {summary}")
        elif title:
            parts.append(title)
    return "; ".join(parts)


def _build_rich_description(
    display_name: str,
    db: Any,
    space: str,
    group_field: str,
    group_slug: str,
) -> str:
    """Build a rich description from base name + sampled note summaries."""
    all_notes = db.get_recent_notes_in_group(space, group_field, group_slug, limit=50)
    summary_text = sample_note_summaries(all_notes)
    if not summary_text:
        return display_name
    return f"{display_name} -- {summary_text}"


class VectorStore:
    """LanceDB-backed vector store for directory and note summary search."""

    def __init__(
        self,
        vectors_dir: Path,
        provider: EmbeddingProvider | None = None,
    ) -> None:
        self._vectors_dir = vectors_dir
        self._provider = provider or FastEmbedProvider()
        self._db: Any = None

    def _get_db(self) -> Any:
        if self._db is None:
            import lancedb

            self._vectors_dir.mkdir(parents=True, exist_ok=True)
            self._db = lancedb.connect(str(self._vectors_dir))
        return self._db

    def _get_or_create_table(self, name: str, schema: pa.Schema) -> Any:
        db = self._get_db()
        if name in db.table_names():
            return db.open_table(name)
        return db.create_table(name, schema=schema)

    # --- Directory operations ---

    def upsert_directory(
        self,
        dir_id: str,
        space: str,
        group_slug: str,
        subgroup_slug: str | None,
        display_name: str,
        description: str,
        note_count: int = 0,
        last_note_date: str = "",
    ) -> None:
        """Insert or update a directory embedding."""
        table = self._get_or_create_table("directories", _DIR_SCHEMA)
        text = f"{display_name} -- {description}" if description else display_name
        vector = self._provider.embed([text])[0]

        row = {
            "id": dir_id,
            "space": space,
            "group_slug": group_slug,
            "subgroup_slug": subgroup_slug or "",
            "display_name": display_name,
            "description": description,
            "note_count": note_count,
            "last_note_date": last_note_date,
            "vector": vector,
        }

        # Delete existing then add (LanceDB upsert)
        try:
            table.delete(f'id = "{_escape_where_value(dir_id)}"')
        except Exception:
            pass
        table.add([row])

    def search_directories(
        self,
        query: str,
        limit: int = 5,
        space: str | None = None,
        group_slug: str | None = None,
    ) -> list[dict[str, Any]]:
        """Search directories by semantic similarity.

        Directory embeddings are built from the display name combined with
        sampled note summaries (see _build_rich_description), so searches
        match against both folder names and the content they contain.

        Args:
            query: Text to match against directory descriptions.
            limit: Max results to return.
            space: Filter to a specific space (e.g. "clients").
            group_slug: Filter to a specific group (search within a folder).

        Returns:
            List of dicts, each containing:
                id, space, group_slug, subgroup_slug, display_name,
                description, note_count, last_note_date, and _distance (float).
            The _distance field indicates semantic distance: lower values
            mean more similar (0.0 = exact match). Typical thresholds used
            by the routing pipeline: <0.4 good match, >=0.4 weak match.
            Results are sorted by _distance ascending (most similar first).
        """
        db = self._get_db()
        if "directories" not in db.table_names():
            return []

        table = db.open_table("directories")
        if table.count_rows() == 0:
            return []

        vector = self._provider.embed([query])[0]
        search = table.search(vector).limit(limit)

        where_parts = []
        if space:
            where_parts.append(f'space = "{_escape_where_value(space)}"')
        if group_slug:
            where_parts.append(f'group_slug = "{_escape_where_value(group_slug)}"')
        if where_parts:
            search = search.where(" AND ".join(where_parts))

        results = search.to_list()
        return [
            {
                "id": r["id"],
                "space": r["space"],
                "group_slug": r["group_slug"],
                "subgroup_slug": r["subgroup_slug"] or None,
                "display_name": r["display_name"],
                "description": r["description"],
                "note_count": r["note_count"],
                "last_note_date": r["last_note_date"],
                "_distance": r.get("_distance", 0),
            }
            for r in results
        ]

    def delete_directory(self, dir_id: str) -> None:
        """Remove a directory from the vector store."""
        db = self._get_db()
        if "directories" not in db.table_names():
            return
        table = db.open_table("directories")
        try:
            table.delete(f'id = "{_escape_where_value(dir_id)}"')
        except Exception:
            pass

    # --- Note summary operations ---

    def upsert_note_summary(
        self,
        note_id: str,
        space: str,
        group_slug: str,
        subgroup_slug: str | None,
        title: str,
        summary: str,
        date: str,
        tags: list[str] | None = None,
        raw_snippet: str | None = None,
    ) -> None:
        """Insert or update a note summary embedding.

        raw_snippet: first ~300 chars of raw text. Included in the embedding
        so vector search can match raw pastes against existing notes.
        """
        table = self._get_or_create_table("note_summaries", _NOTE_SCHEMA)
        parts = [title]
        if summary:
            parts.append(summary)
        if raw_snippet:
            parts.append(raw_snippet)
        text = ". ".join(parts)
        vector = self._provider.embed([text])[0]

        import json
        row = {
            "note_id": note_id,
            "space": space,
            "group_slug": group_slug,
            "subgroup_slug": subgroup_slug or "",
            "title": title,
            "summary": summary or "",
            "date": date,
            "tags": json.dumps(tags or []),
            "vector": vector,
        }

        try:
            table.delete(f'note_id = "{_escape_where_value(note_id)}"')
        except Exception:
            pass
        table.add([row])

    def search_notes(
        self,
        query: str,
        limit: int = 5,
        space: str | None = None,
        group_slug: str | None = None,
    ) -> list[dict[str, Any]]:
        """Search note summaries by semantic similarity.

        Embeds the query text and finds the closest note summary vectors
        in LanceDB. Note embeddings include title, summary, and raw_snippet
        (first ~300 chars of original input) so both structured and raw
        text queries can match.

        Args:
            query: Text to match against note summaries.
            limit: Max results to return.
            space: Filter to a specific space (e.g. "clients").
            group_slug: Filter to a specific group within a space.

        Returns:
            List of dicts, each containing:
                note_id, space, group_slug, subgroup_slug, title,
                summary, date, tags (as list), and _distance (float).
            The _distance field indicates semantic distance: lower values
            mean more similar (0.0 = exact match). Typical thresholds:
            <0.2 likely duplicate, <0.4 closely related, >0.6 weak match.
            Results are sorted by _distance ascending (most similar first).
        """
        import json

        db = self._get_db()
        if "note_summaries" not in db.table_names():
            return []

        table = db.open_table("note_summaries")
        if table.count_rows() == 0:
            return []

        vector = self._provider.embed([query])[0]
        search = table.search(vector).limit(limit)

        where_parts = []
        if space:
            where_parts.append(f'space = "{_escape_where_value(space)}"')
        if group_slug:
            where_parts.append(f'group_slug = "{_escape_where_value(group_slug)}"')
        if where_parts:
            search = search.where(" AND ".join(where_parts))

        results = search.to_list()
        return [
            {
                "note_id": r["note_id"],
                "space": r["space"],
                "group_slug": r["group_slug"],
                "subgroup_slug": r["subgroup_slug"] or None,
                "title": r["title"],
                "summary": r["summary"],
                "date": r["date"],
                "tags": json.loads(r["tags"]) if r["tags"] else [],
                "_distance": r.get("_distance", 0),
            }
            for r in results
        ]

    def delete_note_summary(self, note_id: str) -> None:
        """Remove a note from the vector store."""
        db = self._get_db()
        if "note_summaries" not in db.table_names():
            return
        table = db.open_table("note_summaries")
        try:
            table.delete(f'note_id = "{_escape_where_value(note_id)}"')
            table.compact_files()
            table.cleanup_old_versions()
        except Exception as e:
            logger.debug("Failed to delete note %s from vectors: %s", note_id, e)

    # --- Bulk operations ---

    def rebuild_from_db(
        self, config: Any, db: Any
    ) -> tuple[int, int]:
        """Clear and rebuild both LanceDB tables (directories + note_summaries)
        from the SQLite database.

        This is the nuclear rebuild option: drops existing vector tables entirely,
        then re-indexes everything. The process:
        1. Index space-level directories from config.spaces
        2. Rebuild group/subgroup directories from DB + filesystem scan,
           enriching each with sampled note summaries via _build_rich_description
        3. Rebuild note summary embeddings, including raw_snippet (first 300 chars
           of original input from .raw/ files) for better raw-paste matching

        Called by `notely reindex` and `/sync`. Since LanceDB is derived data,
        this is always safe -- the source of truth remains in .md files and SQLite.

        Args:
            config: NotelyConfig instance for space definitions and paths.
            db: Database instance for reading notes and directories from SQLite.

        Returns:
            Tuple of (dir_count, note_count) -- the number of directories and
            note summaries that were indexed.
        """
        import json

        lance_db = self._get_db()

        # Drop and recreate tables
        for name in ("directories", "note_summaries"):
            if name in lance_db.table_names():
                lance_db.drop_table(name)

        # Index spaces themselves (so vector search works at space level)
        dir_count = 0
        for space_name, space_cfg in config.spaces.items():
            self.upsert_directory(
                dir_id=space_name,
                space=space_name,
                group_slug="",
                subgroup_slug=None,
                display_name=space_cfg.display_name,
                description=space_cfg.description,
            )
            # Also register in DB if not already there
            if not db.get_directory(space_name):
                db.upsert_directory(
                    dir_id=space_name,
                    space=space_name,
                    group_slug="",
                    display_name=space_cfg.display_name,
                    description=space_cfg.description,
                )
            dir_count += 1

        # Rebuild group/subgroup directories from DB + filesystem scan
        dirs = db.get_all_directories()
        for d in dirs:
            # Skip space-level entries (already indexed above)
            if not d["group_slug"]:
                continue

            note_count = 0
            space_cfg = config.get_space(d["space"])
            group_field = space_cfg.group_by if space_cfg else "client"

            if space_cfg:
                note_count_row = db.conn.execute(
                    """SELECT COUNT(*) as c FROM notes
                       WHERE space = ? AND json_extract(space_metadata, ?) = ?""",
                    (d["space"], f"$.{group_field}", d["group_slug"]),
                ).fetchone()
                note_count = note_count_row["c"] if note_count_row else 0

            # Build rich description from recent note titles
            description = _build_rich_description(
                d["display_name"], db, d["space"], group_field, d["group_slug"],
            )
            # Update the DB entry with the enriched description
            db.upsert_directory(
                dir_id=d["id"],
                space=d["space"],
                group_slug=d["group_slug"],
                display_name=d["display_name"],
                description=description,
                subgroup_slug=d.get("subgroup_slug"),
            )

            self.upsert_directory(
                dir_id=d["id"],
                space=d["space"],
                group_slug=d["group_slug"],
                subgroup_slug=d.get("subgroup_slug"),
                display_name=d["display_name"],
                description=description,
                note_count=note_count,
                last_note_date=d.get("updated", ""),
            )
            dir_count += 1

        # Also scan for directories that exist on disk but not in the DB
        dir_count += self._scan_filesystem_directories(config, db)

        # Rebuild note summaries
        note_count = 0
        rows = db.conn.execute(
            "SELECT id, space, title, summary, date, tags, space_metadata, raw_file FROM notes"
        ).fetchall()

        for r in rows:
            sm = json.loads(r["space_metadata"]) if isinstance(r["space_metadata"], str) else r["space_metadata"]
            space_cfg = config.get_space(r["space"])
            group_slug = ""
            subgroup_slug = None
            if space_cfg:
                group_slug = sm.get(space_cfg.group_by, "")
                if space_cfg.subgroup_by:
                    subgroup_slug = sm.get(space_cfg.subgroup_by)

            tags = json.loads(r["tags"]) if isinstance(r["tags"], str) else r["tags"]

            # Read raw snippet for better matching
            raw_snippet = None
            if r["raw_file"]:
                from .storage import raw_file_path
                raw_path = raw_file_path(config, r["raw_file"].replace(".txt", ".md"))
                if not raw_path.exists():
                    # raw_file is stored as the .txt path relative to notes/
                    raw_path = config.raw_dir / r["raw_file"]
                if raw_path.exists():
                    try:
                        raw_snippet = raw_path.read_text(encoding="utf-8").strip()[:RAW_SNIPPET_LENGTH]
                    except Exception:
                        pass

            self.upsert_note_summary(
                note_id=r["id"],
                space=r["space"],
                group_slug=group_slug,
                subgroup_slug=subgroup_slug,
                title=r["title"],
                summary=r["summary"],
                date=r["date"],
                tags=tags,
                raw_snippet=raw_snippet,
            )
            note_count += 1

        return dir_count, note_count

    def _scan_filesystem_directories(self, config: Any, db: Any) -> int:
        """Scan the notes/ folder for directories and ensure they're indexed.

        For directories not already in the DB, auto-generate descriptions
        from recent note titles.
        """
        import json
        count = 0

        if not config.notes_dir.exists():
            return 0

        for space_name, space_cfg in config.spaces.items():
            space_dir = config.notes_dir / space_name
            if not space_dir.exists():
                continue

            for group_dir in sorted(space_dir.iterdir()):
                if not group_dir.is_dir() or group_dir.name.startswith("."):
                    continue

                group_slug = group_dir.name
                dir_id = f"{space_name}/{group_slug}"

                # Skip if already in DB
                if db.get_directory(dir_id):
                    continue

                # Auto-generate description from recent notes
                recent = db.get_recent_notes_in_group(
                    space_name, space_cfg.group_by, group_slug, limit=3
                )
                display_name = group_slug.replace("-", " ").title()
                if recent:
                    titles = [n["title"] for n in recent]
                    description = f"{display_name} -- {', '.join(titles)}"
                else:
                    description = display_name

                db.upsert_directory(
                    dir_id=dir_id,
                    space=space_name,
                    group_slug=group_slug,
                    display_name=display_name,
                    description=description,
                )
                self.upsert_directory(
                    dir_id=dir_id,
                    space=space_name,
                    group_slug=group_slug,
                    subgroup_slug=None,
                    display_name=display_name,
                    description=description,
                )
                count += 1

                # Check for subgroups
                if space_cfg.subgroup_by:
                    for sub_dir in sorted(group_dir.iterdir()):
                        if not sub_dir.is_dir() or sub_dir.name.startswith("."):
                            continue
                        sub_slug = sub_dir.name
                        sub_id = f"{space_name}/{group_slug}/{sub_slug}"

                        if db.get_directory(sub_id):
                            continue

                        sub_display = sub_slug.replace("-", " ").title()
                        db.upsert_directory(
                            dir_id=sub_id,
                            space=space_name,
                            group_slug=group_slug,
                            display_name=sub_display,
                            description=sub_display,
                            subgroup_slug=sub_slug,
                        )
                        self.upsert_directory(
                            dir_id=sub_id,
                            space=space_name,
                            group_slug=group_slug,
                            subgroup_slug=sub_slug,
                            display_name=sub_display,
                            description=sub_display,
                        )
                        count += 1

        return count

    def is_available(self) -> bool:
        """Check if the vector store has any data."""
        try:
            db = self._get_db()
            return "directories" in db.table_names() or "note_summaries" in db.table_names()
        except Exception:
            return False


def get_vector_store(config: Any) -> VectorStore:
    """Create a VectorStore instance, handling import errors gracefully."""
    return VectorStore(config.vectors_dir)


def try_vector_sync_note(config: Any, note: Any) -> None:
    """Fire-and-forget: sync a note to the vector store.

    Non-fatal — if vectors fail, the note is still saved.
    """
    import json
    try:
        vec = get_vector_store(config)
        space_cfg = config.get_space(note.space)
        group_slug = ""
        subgroup_slug = None
        if space_cfg:
            sm = note.space_metadata if isinstance(note.space_metadata, dict) else json.loads(note.space_metadata)
            group_slug = sm.get(space_cfg.group_by, "")
            if space_cfg.subgroup_by:
                subgroup_slug = sm.get(space_cfg.subgroup_by)

        tags = note.tags if isinstance(note.tags, list) else json.loads(note.tags)

        # Include raw snippet for better matching against raw pastes
        raw_snippet = None
        raw_text = getattr(note, "raw_text", None) or ""
        if raw_text:
            raw_snippet = raw_text.strip()[:RAW_SNIPPET_LENGTH]

        vec.upsert_note_summary(
            note_id=note.id,
            space=note.space,
            group_slug=group_slug,
            subgroup_slug=subgroup_slug,
            title=note.title,
            summary=note.summary,
            date=note.date,
            tags=tags,
            raw_snippet=raw_snippet,
        )
    except Exception as e:
        logger.debug(f"Vector sync failed for note {note.id}: {e}")


def try_vector_sync_directory(
    config: Any,
    dir_id: str,
    space: str,
    group_slug: str,
    display_name: str,
    description: str = "",
    subgroup_slug: str | None = None,
    note_count: int = 0,
    last_note_date: str = "",
) -> None:
    """Fire-and-forget: sync a directory to the vector store."""
    try:
        vec = get_vector_store(config)
        vec.upsert_directory(
            dir_id=dir_id,
            space=space,
            group_slug=group_slug,
            subgroup_slug=subgroup_slug,
            display_name=display_name,
            description=description,
            note_count=note_count,
            last_note_date=last_note_date,
        )
    except Exception as e:
        logger.debug(f"Vector sync failed for directory {dir_id}: {e}")


def try_vector_delete_note(config: Any, note_id: str) -> None:
    """Fire-and-forget: remove a note from the vector store."""
    try:
        vec = get_vector_store(config)
        vec.delete_note_summary(note_id)
    except Exception as e:
        logger.debug(f"Vector delete failed for note {note_id}: {e}")


def try_vector_delete_directory(config: Any, dir_id: str) -> None:
    """Fire-and-forget: remove a directory from the vector store."""
    try:
        vec = get_vector_store(config)
        vec.delete_directory(dir_id)
    except Exception as e:
        logger.debug(f"Vector delete failed for directory {dir_id}: {e}")
