"""Notely MCP server — exposes notely storage/retrieval as tools for Claude Max."""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from mcp.server.fastmcp import FastMCP

from .ai import build_taxonomy_context
from .config import NotelyConfig
from .db import Database, safe_json_loads, safe_parse_tags
from .models import ActionItem, ActionItemStatus, InputSize, Note, Refinement, SearchFilters
logger = logging.getLogger(__name__)

from .storage import (
    delete_note_files,
    generate_file_path,
    read_note,
    sync_ideas_index,
    sync_todo_index,
    update_action_status,
    write_note,
)
from .routing import DIST_GOOD_MATCH
from .vectors import get_vector_store, try_vector_delete_note, try_vector_sync_note

mcp = FastMCP(
    "notely",
    instructions="""\
Notely is the user's personal note-taking system. It stores structured markdown \
notes organized into spaces, groups, and subgroups.

## IMPORTANT: When to use these tools

**ONLY use write tools (save_note, update_note, delete_note, add_todo, add_idea, \
complete_todo, reopen_todo, store_secret, store_reference, store_contact) when the user EXPLICITLY asks you to.** \
Look for phrases like:
- "save this", "note this down", "record this", "capture this"
- "add this to my notes", "put this in notely", "file this"
- "add a todo", "remind me to", "add to my ideas"
- "update that note", "delete that note"
- "mark that done", "complete that todo"

Do NOT proactively save conversation content. Most conversations are just \
conversations — not everything needs to be recorded.

**Read tools (search_notes, get_note, get_context, get_taxonomy, get_secrets, \
get_references, get_contacts, find_similar) are fine to use proactively** when they help you answer the user's \
question — e.g. looking up past notes for context, checking what's already saved, \
or finding related information.

## Workflow for saving notes

When the user asks you to save something:
1. Call find_similar() first to check for duplicates
2. If duplicates found, ask the user whether to update or create new
3. Call get_taxonomy() to know which spaces/groups exist
4. Structure the content yourself, then call save_note() or update_note()
""",
)

# Lazily initialised singletons — created on first tool call.
_config: NotelyConfig | None = None
_db: Database | None = None


def _get_config() -> NotelyConfig:
    global _config
    if _config is None:
        _config = NotelyConfig()
    return _config


def _get_db() -> Database:
    global _db
    if _db is None:
        cfg = _get_config()
        _db = Database(cfg.db_path)
        _db.initialize()
    return _db


def _cleanup() -> None:
    """Close the DB connection on server shutdown."""
    global _db
    if _db is not None:
        _db.close()
        _db = None


import atexit
atexit.register(_cleanup)


# ---------- Tools ----------


@mcp.tool()
def get_taxonomy() -> dict:
    """Get the full taxonomy of spaces, groups, and subgroups.

    Returns the current workspace structure so you know which spaces exist,
    what groups (clients/categories) are in each space, and how many notes
    each one has. Use this before save_note to pick the right routing.
    """
    config = _get_config()
    db = _get_db()
    return build_taxonomy_context(config, db)


@mcp.tool()
def save_note(
    space: str,
    group_slug: str,
    group_display: str,
    title: str,
    date: str,
    summary: str,
    body_markdown: str,
    source: str = "manual",
    tags: list[str] | None = None,
    participants: list[str] | None = None,
    action_items: list[dict] | None = None,
    subgroup_slug: str | None = None,
    subgroup_display: str | None = None,
    extra_metadata: dict | None = None,
    related_contexts: list[str] | None = None,
    group_is_new: bool = False,
    subgroup_is_new: bool = False,
    attachment_paths: list[str] | None = None,
    extracted_records: list[dict] | None = None,
) -> dict:
    """Save a structured note to the notely workspace.

    ONLY call this when the user explicitly asks to save, record, or capture
    something into their notes. Do NOT call this proactively.

    Structure the note yourself, then call this tool to persist it.
    Use get_taxonomy first to know which spaces/groups exist.
    Use find_similar first to check for duplicates.

    Args:
        space: Which space (e.g. "clients", "ideas")
        group_slug: Slug for the primary group (e.g. "acme-corp")
        group_display: Display name for the group (e.g. "ACME Corp")
        title: Clear, descriptive note title
        date: Date of the event in YYYY-MM-DD format
        summary: 1-2 sentence summary of the key takeaway
        body_markdown: The organized note body in markdown
        source: Source type (meeting, slack, email, podcast, article, thought, manual)
        tags: Relevant tags as lowercase slugs
        participants: People mentioned or involved
        action_items: List of {owner, task, due?} dicts
        subgroup_slug: Slug for subgroup (e.g. "api-project"), if applicable
        subgroup_display: Display name for the subgroup
        extra_metadata: Space-specific extra fields (e.g. content_status, source_ref)
        related_contexts: Cross-references as paths like "clients/acme-corp/api-project"
        group_is_new: Whether this is a new group not yet in the taxonomy
        subgroup_is_new: Whether this is a new subgroup
        attachment_paths: Absolute paths to files to attach to this note
    """
    config = _get_config()
    db = _get_db()

    tags = tags or []
    participants = participants or []
    action_items_raw = action_items or []
    extra_metadata = extra_metadata or {}
    related_contexts = related_contexts or []

    note_id = uuid.uuid4().hex[:8]
    now = datetime.now(timezone.utc).isoformat()

    # Build space_metadata
    from .storage import build_space_metadata
    space_metadata = build_space_metadata(
        config, space, group_slug, group_display,
        subgroup_slug, subgroup_display, extra=extra_metadata,
    )

    # Create directories for new groups/subgroups
    if group_is_new:
        config.group_dir(space, group_slug).mkdir(parents=True, exist_ok=True)
    if subgroup_is_new and subgroup_slug:
        config.subgroup_dir(space, group_slug, subgroup_slug).mkdir(parents=True, exist_ok=True)

    rel_path = generate_file_path(config, space, group_slug, date, title, subgroup_slug)

    # Parse action items
    parsed_actions = [
        ActionItem(
            owner=ai.get("owner", "me"),
            task=ai["task"],
            due=ai.get("due"),
        )
        for ai in action_items_raw
    ]

    note = Note(
        id=note_id,
        space=space,
        title=title,
        source=source,
        refinement=Refinement.AI_STRUCTURED,
        input_size=InputSize.MEDIUM,
        date=date,
        created=now,
        updated=now,
        summary=summary,
        tags=tags,
        participants=participants,
        file_path=rel_path,
        body=body_markdown,
        raw_text="",
        related_contexts=related_contexts,
        space_metadata=space_metadata,
    )

    # Copy attachments — track the first binary file for .raw/ provenance
    source_file = None
    if attachment_paths:
        from pathlib import Path as _Path
        from .files import copy_attachment, TEXT_EXTENSIONS
        for ap in attachment_paths:
            p = _Path(ap)
            if p.is_file():
                rel = copy_attachment(p, config, space, group_slug, subgroup_slug)
                note.attachments.append(rel)
                # First binary attachment becomes the source_file for .raw/
                if source_file is None and p.suffix.lower() not in TEXT_EXTENSIONS:
                    source_file = p

    from .storage import save_and_sync

    save_and_sync(config, db, note, source_file=source_file, action_items=parsed_actions,
                  extracted_records=extracted_records)
    abs_path = config.notes_dir / note.file_path

    return {
        "status": "ok",
        "note_id": note_id,
        "file_path": rel_path,
        "absolute_path": str(abs_path),
        "attachments": note.attachments,
    }


@mcp.tool()
def search_notes(
    query: str | None = None,
    space: str | None = None,
    client: str | None = None,
    category: str | None = None,
    tags: list[str] | None = None,
    limit: int = 20,
    include_body: bool = False,
) -> dict:
    """Search notes with text and/or filters.

    This is a read-only tool — safe to use proactively when looking up
    context or answering user questions about their notes.

    Args:
        query: Full-text search query (optional)
        space: Filter by space name
        client: Filter by client slug
        category: Filter by category slug
        tags: Filter by tags
        limit: Max results (default 20)
        include_body: Include full note body in results
    """
    config = _get_config()
    db = _get_db()

    filters = SearchFilters(
        space=space,
        client=client,
        category=category,
        tags=tags or [],
    )

    rows = db.search(text_query=query, filters=filters, limit=limit)

    results = []
    for r in rows:
        entry: dict[str, Any] = {
            "id": r["id"],
            "title": r["title"],
            "space": r["space"],
            "date": r["date"],
            "summary": r["summary"],
            "tags": safe_parse_tags(r["tags"]),
            "file_path": r["file_path"],
        }
        sm = safe_json_loads(r.get("space_metadata"))
        entry.update(sm)

        items = db.get_note_todos(r["id"])
        entry["action_items_open"] = sum(1 for i in items if i["status"] == "open")

        if include_body:
            note = read_note(config, r["file_path"])
            entry["body"] = note.body if note else ""

        results.append(entry)

    return {"status": "ok", "count": len(results), "results": results}


@mcp.tool()
def get_note(note_id: str) -> dict:
    """Get a single note by ID with full body.

    Read-only — safe to use proactively for context.

    Args:
        note_id: The 8-character note ID
    """
    config = _get_config()
    db = _get_db()

    row = db.get_note(note_id)
    if not row:
        return {"status": "error", "message": f"Note not found: {note_id}"}

    note = read_note(config, row["file_path"])
    if not note:
        return {"status": "error", "message": f"Note file not found: {row['file_path']}"}

    sm = safe_json_loads(row.get("space_metadata"))
    action_items = db.get_note_todos(note_id)
    cross_refs = db.get_note_cross_refs(note_id)

    return {
        "status": "ok",
        "note": {
            "id": note.id,
            "space": note.space,
            "title": note.title,
            "source": note.source,
            "date": note.date,
            "summary": note.summary,
            "tags": note.tags,
            "participants": note.participants,
            "body": note.body,
            "file_path": note.file_path,
            **sm,
            "action_items": [
                {"id": a["id"], "owner": a["owner"], "task": a["task"],
                 "due": a["due"], "status": a["status"]}
                for a in action_items
            ],
            "related_contexts": cross_refs,
            "attachments": note.attachments,
        },
    }


@mcp.tool()
def get_context(space: str, client: str | None = None) -> dict:
    """Get full context for a space or client.

    Read-only — safe to use proactively for context.

    Returns an overview (groups, topics), recent notes, and open action items.
    Use this to understand the current state before adding notes.

    Args:
        space: Space name (e.g. "clients", "ideas")
        client: Optional client slug to focus on
    """
    config = _get_config()
    db = _get_db()

    space_cfg = config.get_space(space)
    if not space_cfg:
        return {"status": "error", "message": f"Unknown space: {space}"}

    overview: dict[str, Any] = {
        "space": space,
        "display_name": space_cfg.display_name,
        "description": space_cfg.description,
    }

    # Get groups
    groups = db.get_groups(space, space_cfg.group_by)
    overview["groups"] = [
        {
            "slug": g["grp"],
            "display": g["grp_display"] or g["grp"],
            "note_count": g["note_count"],
            "last_note": g["last_note"],
        }
        for g in groups
    ]

    # Build filters
    filters = SearchFilters(space=space)
    if client:
        filters.client = client
        overview["focus"] = {"client": client}

        # Get subgroups (topics) for this client
        if space_cfg.subgroup_by:
            subgroups = db.get_subgroups(space, space_cfg.group_by, client, space_cfg.subgroup_by)
            overview["topics"] = [
                {
                    "slug": sg["subgrp"],
                    "display": sg["subgrp_display"] or sg["subgrp"],
                    "note_count": sg["note_count"],
                }
                for sg in subgroups
            ]

    # Recent notes
    rows = db.search(filters=filters, limit=10, sort_by="recency")
    recent = []
    for r in rows:
        entry: dict[str, Any] = {
            "id": r["id"],
            "title": r["title"],
            "date": r["date"],
            "summary": r["summary"],
            "tags": safe_parse_tags(r["tags"]),
        }
        sm = safe_json_loads(r.get("space_metadata"))
        entry.update(sm)
        recent.append(entry)

    # Open action items
    action_items = db.get_open_todos(space=space, group_slug=client)
    open_items = [
        {
            "id": a["id"],
            "owner": a["owner"],
            "task": a["task"],
            "due": a["due"],
            "note_title": a["note_title"],
        }
        for a in action_items
    ]

    return {
        "status": "ok",
        "overview": overview,
        "recent_notes": recent,
        "open_action_items": open_items,
    }


@mcp.tool()
def add_todo(
    task: str,
    owner: str = "me",
    due: str | None = None,
    space: str | None = None,
    group: str | None = None,
) -> dict:
    """Add a standalone todo item.

    ONLY call this when the user explicitly asks to add a todo, task, or
    reminder. Do NOT call this proactively.

    Args:
        task: The task description
        owner: Who owns this task (default: "me")
        due: Due date in YYYY-MM-DD format (optional)
        space: Which space this relates to (optional)
        group: Which group (client/category) this relates to (optional)
    """
    config = _get_config()
    db = _get_db()

    item_id = db.add_todo(
        owner=owner,
        task=task,
        due=due,
        space=space,
        group_slug=group,
    )

    sync_todo_index(config, db)

    return {"status": "ok", "item_id": item_id, "task": task}


@mcp.tool()
def complete_todo(item_id: int) -> dict:
    """Mark a todo as done. ONLY call when the user explicitly asks.

    Args:
        item_id: The action item ID number
    """
    config = _get_config()
    db = _get_db()

    row = db.get_todo(item_id)

    if not row:
        return {"status": "error", "message": f"No todo with ID {item_id}"}

    if row["status"] == "done":
        return {"status": "ok", "message": "Already done", "task": row["task"]}

    update_action_status(config, db, item_id, "done")
    sync_todo_index(config, db)

    return {"status": "ok", "task": row["task"]}


@mcp.tool()
def add_idea(
    title: str,
    summary: str | None = None,
    category: str | None = None,
    tags: list[str] | None = None,
) -> dict:
    """Add a quick idea to the ideas space.

    ONLY call this when the user explicitly asks to save an idea.
    Do NOT call this proactively.

    Args:
        title: The idea title
        summary: One-line summary or expansion (defaults to title)
        category: Category slug (optional)
        tags: Relevant tags (optional)
    """
    config = _get_config()
    db = _get_db()

    note_id = db.add_standalone_idea(
        title=title,
        summary=summary or title,
        category=category,
        tags=tags,
    )

    sync_ideas_index(config, db)

    return {"status": "ok", "note_id": note_id, "title": title}


@mcp.tool()
def update_note(
    note_id: str,
    title: str | None = None,
    summary: str | None = None,
    body_markdown: str | None = None,
    tags: list[str] | None = None,
    participants: list[str] | None = None,
    action_items: list[dict] | None = None,
    extra_metadata: dict | None = None,
    related_contexts: list[str] | None = None,
    attachment_paths: list[str] | None = None,
    extracted_records: list[dict] | None = None,
) -> dict:
    """Update an existing note. Only provided fields are changed.

    ONLY call this when the user explicitly asks to update a note.
    Do NOT call this proactively.

    Call get_note first to read the current state, then call this with
    only the fields you want to change. Do your own merge/restructuring,
    then persist the result here.

    If the title changes, the file is moved to match the new slug.

    Args:
        note_id: The 8-character note ID
        title: New title (triggers file rename)
        summary: New summary
        body_markdown: New body content
        tags: Replace all tags
        participants: Replace all participants
        action_items: Replace all action items (list of {owner, task, due?, status?} dicts)
        extra_metadata: Replace space-specific extra fields
        related_contexts: Replace cross-references
        attachment_paths: Absolute paths to files to attach (appended to existing)
    """
    config = _get_config()
    db = _get_db()

    row = db.get_note(note_id)
    if not row:
        return {"status": "error", "message": f"Note not found: {note_id}"}

    note = read_note(config, row["file_path"])
    if not note:
        return {"status": "error", "message": f"Note file not found: {row['file_path']}"}

    old_file_path = note.file_path

    # Apply only provided fields
    if title is not None:
        note.title = title
    if summary is not None:
        note.summary = summary
    if body_markdown is not None:
        note.body = body_markdown
    if tags is not None:
        note.tags = tags
    if participants is not None:
        note.participants = participants
    # action_items handled below via DB operations
    new_action_items = None
    if action_items is not None:
        new_action_items = [
            ActionItem(
                owner=ai.get("owner", "me"),
                task=ai["task"],
                due=ai.get("due"),
                status=ActionItemStatus(ai.get("status", "open")),
            )
            for ai in action_items
        ]
    if extra_metadata is not None:
        note.space_metadata.update(extra_metadata)
    if related_contexts is not None:
        note.related_contexts = related_contexts

    note.updated = datetime.now(timezone.utc).isoformat()

    # If title changed, regenerate file_path and delete old files
    if title is not None and old_file_path != note.file_path:
        sm = note.space_metadata
        space_cfg = config.get_space(note.space)
        group_slug = ""
        subgroup_slug = None
        if space_cfg:
            group_slug = sm.get(space_cfg.group_by, "")
            if space_cfg.subgroup_by:
                subgroup_slug = sm.get(space_cfg.subgroup_by)
        note.file_path = generate_file_path(
            config, note.space, group_slug, note.date, note.title, subgroup_slug,
        )
        if note.file_path != old_file_path:
            delete_note_files(config, old_file_path)

    # Copy new attachments
    if attachment_paths:
        from pathlib import Path as _Path
        from .files import copy_attachment
        sm = note.space_metadata
        space_cfg = config.get_space(note.space)
        g_slug = sm.get(space_cfg.group_by, "") if space_cfg else ""
        sg_slug = sm.get(space_cfg.subgroup_by) if space_cfg and space_cfg.subgroup_by else None
        for ap in attachment_paths:
            p = _Path(ap)
            if p.is_file():
                rel = copy_attachment(p, config, note.space, g_slug, sg_slug)
                note.attachments.append(rel)

    # Save via shared pipeline
    from .storage import save_and_sync

    # For action_items update: delete existing note-linked items, re-insert
    if new_action_items is not None:
        db.conn.execute("DELETE FROM action_items WHERE note_id = ?", (note_id,))
        db.conn.commit()

    save_and_sync(config, db, note, action_items=new_action_items,
                  extracted_records=extracted_records)
    abs_path = config.notes_dir / note.file_path

    return {
        "status": "ok",
        "note_id": note_id,
        "file_path": note.file_path,
        "absolute_path": str(abs_path),
        "attachments": note.attachments,
    }


@mcp.tool()
def delete_note(note_id: str) -> dict:
    """Delete a note permanently. ONLY call when the user explicitly asks to delete.

    Removes the markdown file, raw text file, database entry, vector entry,
    and updates the CSV indexes.

    Args:
        note_id: The 8-character note ID
    """
    config = _get_config()
    db = _get_db()

    row = db.get_note(note_id)
    if not row:
        return {"status": "error", "message": f"Note not found: {note_id}"}

    file_path = row["file_path"]
    title = row["title"]

    # Delete in order: files → DB → vectors → CSVs
    delete_note_files(config, file_path)
    db.delete_note(note_id)

    try:
        try_vector_delete_note(config, note_id)
    except Exception:
        pass

    sync_todo_index(config, db)
    sync_ideas_index(config, db)

    return {"status": "ok", "deleted": title, "file_path": file_path}


@mcp.tool()
def store_secret(service: str, key: str, value: str) -> dict:
    """Store a secret (API key, token, password) in the local secrets file.

    ONLY call this when the user explicitly asks to store a credential,
    or when the user shares a credential and asks you to save it.

    Secrets are stored in .secrets.toml (gitignored) organized by service name.

    Args:
        service: Service name (e.g. "aws-prod", "openai", "stripe")
        key: Key name (e.g. "api_key", "secret", "token")
        value: The secret value
    """
    from .secrets import SecretsStore

    config = _get_config()
    store = SecretsStore(config.secrets_path)
    store.store(service, key, value)
    return {"status": "ok", "service": service, "key": key}


@mcp.tool()
def get_secrets(service: str | None = None) -> dict:
    """List stored secrets. Returns key NAMES only (not values) for safety.

    Args:
        service: Optional service name to filter. If omitted, lists all services.
    """
    from .secrets import SecretsStore

    config = _get_config()
    store = SecretsStore(config.secrets_path)

    if service:
        keys = store.get(service)
        if keys is None:
            return {"status": "error", "message": f"No secrets for service: {service}"}
        return {
            "status": "ok",
            "service": service,
            "keys": list(keys.keys()),
            "count": len(keys),
        }

    all_secrets = store.get_all()
    services = {}
    for svc, keys in all_secrets.items():
        services[svc] = {"keys": list(keys.keys()), "count": len(keys)}
    return {"status": "ok", "services": services}


@mcp.tool()
def list_databases() -> dict:
    """List all user-defined databases with record counts.

    Read-only — safe to use proactively. Returns all databases that have records.
    """
    db = _get_db()
    names = set(db.get_database_names())
    result = {}
    for name in sorted(names):
        records = db.get_database_records(name)
        result[name] = {"count": len(records)}
    return {"status": "ok", "databases": result}


@mcp.tool()
def store_record(
    database: str,
    entity: str,
    key: str,
    value: str,
    description: str = "",
    space: str = "",
    group_slug: str = "",
) -> dict:
    """Store a record in any user-defined database.

    ONLY call this when the user explicitly asks to save/store information.

    Any database name auto-creates a new database if it doesn't exist yet.

    Args:
        database: Database name (e.g. "contacts", "references", "vendors")
        entity: Entity name (e.g. "labcorp", "Jake Chen", "Acme Corp")
        key: Field label (e.g. "email", "npi", "account_number")
        value: The value to store
        description: Optional context
        space: Optional space to scope to
        group_slug: Optional folder to scope to
    """
    db = _get_db()
    config = _get_config()
    ref_id = db.add_reference(
        space=space, group_slug=group_slug,
        entity=entity, key=key, value=value,
        description=description, snippet_type=database,
    )
    from .storage import sync_database_indexes
    sync_database_indexes(config, db)
    return {"status": "ok", "id": ref_id, "database": database, "entity": entity, "key": key}


@mcp.tool()
def get_records(
    database: str | None = None,
    entity: str | None = None,
    space: str | None = None,
    group_slug: str | None = None,
    query: str | None = None,
) -> dict:
    """Look up stored records from any database. Returns full values.

    Read-only — safe to use proactively. For contacts, also returns recent interaction notes.

    Args:
        database: Optional database name to filter by (e.g. "contacts", "references")
        entity: Optional entity name to filter by
        space: Optional space to filter by
        group_slug: Optional folder to filter by
        query: Optional full-text search query
    """
    db = _get_db()

    if query:
        results = db.search_references(query, space=space, group_slug=group_slug)
        if database:
            results = [r for r in results if r.get("snippet_type") == database]
    elif database:
        results = db.get_database_records(database, space=space, group_slug=group_slug, entity=entity)
    else:
        results = db.get_references(space=space, group_slug=group_slug, entity=entity)

    # Group by entity for readability
    by_entity: dict[str, list] = {}
    for r in results:
        by_entity.setdefault(r["entity"], []).append(
            {"key": r["key"], "value": r["value"], "description": r.get("description", ""),
             "database": r.get("snippet_type", "fact"), "id": r["id"]}
        )

    # Add interactions for contacts
    if database == "contacts" or (not database and any(
        item.get("database") == "contacts"
        for items in by_entity.values() for item in items
    )):
        for entity_name in by_entity:
            if any(i.get("database") == "contacts" for i in by_entity[entity_name]):
                interactions = db.get_contact_interactions(entity_name, limit=5)
                for item in by_entity[entity_name]:
                    if item.get("database") == "contacts":
                        item["recent_notes"] = [
                            {"id": n["id"], "title": n["title"], "date": n["date"]}
                            for n in interactions
                        ]
                        break

    return {"status": "ok", "count": len(results), "records": by_entity}


# --- Deprecated aliases (backward compat for existing Claude Desktop conversations) ---

@mcp.tool()
def store_reference(
    entity: str, key: str, value: str,
    description: str = "", snippet_type: str = "fact",
    space: str = "", group_slug: str = "",
) -> dict:
    """[Deprecated — use store_record] Store a reference snippet.

    ONLY call this when the user explicitly asks to save/store reference information.
    """
    return store_record(
        database=snippet_type,
        entity=entity, key=key, value=value,
        description=description, space=space, group_slug=group_slug,
    )


@mcp.tool()
def get_references(
    entity: str | None = None, space: str | None = None,
    group_slug: str | None = None, query: str | None = None,
) -> dict:
    """[Deprecated — use get_records] Look up stored reference data."""
    return get_records(database="references", entity=entity, space=space,
                       group_slug=group_slug, query=query)


@mcp.tool()
def store_contact(
    name: str, field: str, value: str,
    space: str = "", group_slug: str = "",
) -> dict:
    """[Deprecated — use store_record] Store contact information.

    ONLY call this when the user explicitly asks to save contact info.
    """
    return store_record(
        database="contacts", entity=name, key=field, value=value,
        space=space, group_slug=group_slug,
    )


@mcp.tool()
def get_contacts(
    name: str | None = None, space: str | None = None,
    query: str | None = None,
) -> dict:
    """[Deprecated — use get_records] Look up stored contact information."""
    return get_records(database="contacts", entity=name, space=space, query=query)


@mcp.tool()
def reopen_todo(item_id: int) -> dict:
    """Reopen a completed todo item. ONLY call when the user explicitly asks.

    Args:
        item_id: The action item ID number
    """
    config = _get_config()
    db = _get_db()

    row = db.get_todo(item_id)

    if not row:
        return {"status": "error", "message": f"No todo with ID {item_id}"}

    if row["status"] == "open":
        return {"status": "ok", "message": "Already open", "task": row["task"]}

    update_action_status(config, db, item_id, "open")
    sync_todo_index(config, db)

    return {"status": "ok", "task": row["task"]}


@mcp.tool()
def find_similar(text: str, space: str | None = None) -> dict:
    """Check if similar notes already exist before saving.

    Read-only — safe to use proactively. Always call this before save_note
    when the user asks to save something.

    Runs 3 detection layers against existing notes:
    1. Exact hash — identical text already saved
    2. Snippet hash — first 300 chars match (near-duplicate)
    3. Vector search — semantically similar notes and directories

    Call this BEFORE save_note when the user pastes text, so you can ask
    whether to update an existing note or create a new one.

    Args:
        text: The raw text to check against existing notes
        space: Optional space name to narrow the search
    """
    config = _get_config()
    db = _get_db()

    result: dict[str, Any] = {
        "status": "ok",
        "exact_match": None,
        "snippet_match": None,
        "similar_notes": [],
        "suggested_directories": [],
        "recommendation": "no_match",
    }

    # Layer 1: exact hash
    exact = db.find_exact_duplicate(text)
    if exact:
        result["exact_match"] = exact
        result["recommendation"] = "exact_duplicate"
        return result

    # Layer 2: snippet hash
    snippet = db.find_snippet_match(text)
    if snippet:
        result["snippet_match"] = snippet
        result["recommendation"] = "likely_duplicate"
        return result

    # Layer 3: vector search (fire-and-forget if unavailable)
    try:
        vec_store = get_vector_store(config)
        if vec_store.is_available():
            # Search notes
            similar = vec_store.search_notes(text, limit=5, space=space)
            result["similar_notes"] = [
                {
                    "note_id": n["note_id"],
                    "title": n["title"],
                    "summary": n["summary"],
                    "date": n["date"],
                    "space": n["space"],
                    "group_slug": n["group_slug"],
                    "tags": n["tags"],
                    "distance": n["_distance"],
                }
                for n in similar
                if n["_distance"] < DIST_GOOD_MATCH
            ]

            # Search directories for routing suggestions
            dirs = vec_store.search_directories(text, limit=5, space=space)
            result["suggested_directories"] = [
                {
                    "id": d["id"],
                    "space": d["space"],
                    "group_slug": d["group_slug"],
                    "display_name": d["display_name"],
                    "note_count": d["note_count"],
                    "distance": d["_distance"],
                }
                for d in dirs
                if d["_distance"] < DIST_GOOD_MATCH
            ]

            if result["similar_notes"]:
                result["recommendation"] = "similar_exists"
    except Exception:
        pass

    return result


# ---------- Entry point ----------


def main():
    """Run the notely MCP server."""
    mcp.run()


if __name__ == "__main__":
    main()
