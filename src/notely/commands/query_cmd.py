"""notely query — structured JSON query for AI agents."""

from __future__ import annotations

import json
from typing import Any

import click
from rich.console import Console

from ..config import NotelyConfig
from ..db import Database, safe_json_loads, safe_parse_tags
from ..models import SearchFilters, SearchOptions, SearchQuery
from ..storage import read_note

console = Console()


@click.command("query")
@click.argument("query_json")
@click.pass_context
def query_cmd(ctx: click.Context, query_json: str) -> None:
    """Execute a structured JSON query. For AI agents and programmatic access."""
    config: NotelyConfig = ctx.obj["config"]

    config.ensure_initialized()

    try:
        raw = json.loads(query_json)
    except json.JSONDecodeError as e:
        click.echo(json.dumps({"status": "error", "message": f"Invalid JSON: {e}"}))
        raise SystemExit(1)

    with Database(config.db_path) as db:
        db.initialize()

        try:
            query = _parse_query(raw)
            intent = query.intent

            if intent == "search_notes":
                result = _handle_search(config, db, query)
            elif intent == "get_context":
                result = _handle_get_context(config, db, query)
            elif intent == "update_status":
                result = _handle_update_status(db, query)
            else:
                result = {"status": "error", "message": f"Unknown intent: {intent}"}
        except Exception as e:
            result = {"status": "error", "message": str(e)}

    click.echo(json.dumps(result, indent=2))


def _parse_query(raw: dict[str, Any]) -> SearchQuery:
    """Parse raw JSON into a SearchQuery, handling nested filters."""
    filters_raw = raw.get("filters", {})
    options_raw = raw.get("options", {})

    filters = SearchFilters(
        space=filters_raw.get("space") or raw.get("space"),
        tags=filters_raw.get("tags", []),
        source=filters_raw.get("source"),
        refinement=filters_raw.get("refinement", []),
        date_from=filters_raw.get("date_from"),
        date_to=filters_raw.get("date_to"),
        client=filters_raw.get("client") or raw.get("client"),
        topic=filters_raw.get("topic"),
        category=filters_raw.get("category"),
        content_status=filters_raw.get("content_status"),
    )

    options = SearchOptions(
        limit=options_raw.get("limit", 20),
        include_body=options_raw.get("include_body", False),
        include_raw=options_raw.get("include_raw", False),
        sort_by=options_raw.get("sort_by", "recency"),
    )

    return SearchQuery(
        intent=raw.get("intent", "search_notes"),
        space=raw.get("space"),
        query=raw.get("query"),
        filters=filters,
        options=options,
        client=raw.get("client"),
        note_id=raw.get("note_id"),
        content_status=raw.get("content_status"),
    )


def _handle_search(
    config: NotelyConfig, db: Database, query: SearchQuery
) -> dict[str, Any]:
    """Handle search_notes intent."""
    # Apply space from top-level if not in filters
    if query.space and not query.filters.space:
        query.filters.space = query.space

    rows = db.search(
        text_query=query.query,
        filters=query.filters,
        limit=query.options.limit,
        sort_by=query.options.sort_by,
    )

    results = []
    for r in rows:
        entry: dict[str, Any] = {
            "id": r["id"],
            "title": r["title"],
            "space": r["space"],
            "date": r["date"],
            "refinement": r["refinement"],
            "summary": r["summary"],
            "tags": safe_parse_tags(r["tags"]),
            "file_path": r["file_path"],
        }

        sm = safe_json_loads(r.get("space_metadata"))
        entry.update(sm)

        # Action items count
        items = db.get_note_action_items(r["id"])
        entry["action_items_open"] = sum(1 for i in items if i["status"] == "open")

        if query.options.include_body:
            note = read_note(config, r["file_path"])
            entry["body"] = note.body if note else ""
            if query.options.include_raw and note:
                entry["raw_text"] = note.raw_text

        results.append(entry)

    return {"status": "ok", "count": len(results), "results": results}


def _handle_get_context(
    config: NotelyConfig, db: Database, query: SearchQuery
) -> dict[str, Any]:
    """Handle get_context intent — give an agent full context for a space/client."""
    space = query.space or query.filters.space
    client = query.client or query.filters.client

    if not space:
        return {"status": "error", "message": "space is required for get_context"}

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

    # Build filters for notes
    filters = SearchFilters(space=space)
    if client:
        filters.client = client
        overview["focus"] = {"client": client}

        # Get subgroups for this client
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
        entry = {
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
    action_items = db.get_open_action_items(space=space, client=client)
    open_items = [
        {
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


def _handle_update_status(db: Database, query: SearchQuery) -> dict[str, Any]:
    """Handle update_status intent (e.g., mark content_status as used)."""
    note_id = query.note_id
    content_status = query.content_status

    if not note_id:
        return {"status": "error", "message": "note_id is required"}

    row = db.get_note(note_id)
    if not row:
        return {"status": "error", "message": f"Note not found: {note_id}"}

    if content_status:
        sm = safe_json_loads(row.get("space_metadata"))
        sm["content_status"] = content_status
        db.update_note_metadata(note_id, space_metadata=json.dumps(sm))

    return {"status": "ok", "updated": note_id}
