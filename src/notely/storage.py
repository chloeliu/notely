"""Markdown file I/O, CSV index sync, and shared note operations.

This module is the single source of truth for:
- Reading/writing markdown files with YAML frontmatter
- Managing .raw/ folder for original input
- CSV index generation (_todos.csv, _ideas.csv)
- Action item status updates (one-way flow: Note → markdown → DB)
- Merge preview/edit/apply helpers (shared by open_cmd and dump)
- The full save-and-sync pipeline (write → DB → vectors → CSVs)
- Input size classification

For data models, see models.py.
For SQLite operations, see db.py.
For vector operations, see vectors.py.
"""

from __future__ import annotations

import json
import logging
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import frontmatter
from slugify import slugify

from .config import NotelyConfig
from .models import InputSize, Note, Refinement

logger = logging.getLogger(__name__)


def generate_file_path(
    config: NotelyConfig,
    space: str,
    group: str,
    date: str,
    title: str,
    subgroup: str | None = None,
) -> str:
    """Generate the relative file path for a note."""
    slug = slugify(title, max_length=60)
    filename = f"{date}_{slug}.md"
    if subgroup:
        return str(Path(space) / group / subgroup / filename)
    return str(Path(space) / group / filename)


def absolute_path(config: NotelyConfig, relative_path: str) -> Path:
    return config.notes_dir / relative_path


def ensure_parent_dir(config: NotelyConfig, relative_path: str) -> None:
    abs_path = absolute_path(config, relative_path)
    abs_path.parent.mkdir(parents=True, exist_ok=True)


def raw_file_path(config: NotelyConfig, note_rel_path: str) -> Path:
    """Return the .raw/ path for a note's raw text."""
    return config.raw_dir / note_rel_path.replace(".md", ".txt")


def write_note(
    config: NotelyConfig, note: Note, source_file: Path | None = None,
) -> Path:
    """Write a note to disk as markdown with YAML frontmatter.

    Args:
        config: workspace config
        note: the Note to write
        source_file: optional original binary file (PDF/image) to copy into .raw/
            for provenance. Text files (.txt, .py, etc.) are skipped since the
            extracted text is already stored as .raw/*.txt.
    """
    from .files import TEXT_EXTENSIONS

    ensure_parent_dir(config, note.file_path)
    abs_path = absolute_path(config, note.file_path)

    fm = _note_to_frontmatter(note)
    body = note.body

    # Write raw text to .raw/ folder (keeps .md files clean)
    if note.raw_text:
        raw_path = raw_file_path(config, note.file_path)
        raw_path.parent.mkdir(parents=True, exist_ok=True)
        raw_content = note.raw_text[:100_000]
        if len(note.raw_text) > 100_000:
            raw_content += "\n\n---\n*[Truncated: original input was "
            raw_content += f"{len(note.raw_text):,} characters]*\n"
        raw_path.write_text(raw_content, encoding="utf-8")

    # Copy original binary file to .raw/ for provenance (PDF, images)
    if source_file and source_file.is_file():
        suffix = source_file.suffix.lower()
        if suffix not in TEXT_EXTENSIONS:
            raw_dir = config.raw_dir / Path(note.file_path).parent
            raw_dir.mkdir(parents=True, exist_ok=True)
            stem = Path(note.file_path).stem  # e.g. "2026-02-23_meeting-notes"
            dest = raw_dir / f"{stem}{suffix}"
            shutil.copy2(source_file, dest)

    post = frontmatter.Post(body, **fm)
    abs_path.write_text(frontmatter.dumps(post), encoding="utf-8")
    return abs_path


def read_note(config: NotelyConfig, relative_path: str) -> Note | None:
    """Read a note from disk, parsing frontmatter."""
    abs_path = absolute_path(config, relative_path)
    if not abs_path.exists():
        return None

    post = frontmatter.load(str(abs_path))
    meta = dict(post.metadata)
    body = post.content

    # Load raw text — check .raw/ folder first, then legacy locations
    raw_text = ""
    new_raw = raw_file_path(config, relative_path)
    if new_raw.exists():
        raw_text = new_raw.read_text(encoding="utf-8")
    else:
        # Legacy: embedded <details> block
        raw_marker = "<details><summary>Raw Source</summary>"
        if raw_marker in body:
            idx = body.index(raw_marker)
            raw_section = body[idx:]
            body = body[:idx].rstrip()
            start = raw_section.find("\n\n", len(raw_marker))
            end = raw_section.rfind("</details>")
            if start != -1 and end != -1:
                raw_text = raw_section[start + 2 : end].rstrip()
        # Legacy: sibling .raw.md file
        if not raw_text:
            legacy_raw = abs_path.with_suffix(".raw.md")
            if legacy_raw.exists():
                raw_text = legacy_raw.read_text(encoding="utf-8")

    # Build space_metadata from known space-specific fields
    space_metadata = meta.get("space_metadata", {})
    # Also pick up top-level space-specific fields for backwards compat
    for key in ("client", "client_display", "topic", "topic_display",
                "category", "category_display", "content_status",
                "source_ref"):
        if key in meta and key not in space_metadata:
            space_metadata[key] = meta[key]

    return Note(
        id=meta.get("id", ""),
        space=meta.get("space", ""),
        title=meta.get("title", ""),
        source=meta.get("source", "manual"),
        refinement=Refinement(meta.get("refinement", "raw")),
        input_size=InputSize(meta.get("input_size", "medium")),
        date=meta.get("date", ""),
        created=meta.get("created", ""),
        updated=meta.get("updated", ""),
        summary=meta.get("summary", ""),
        tags=meta.get("tags", []),
        participants=meta.get("participants", []),
        file_path=relative_path,
        body=body,
        raw_text=raw_text,
        source_url=meta.get("source_url", ""),
        related_contexts=meta.get("related_contexts", []),
        attachments=meta.get("attachments", []),
        space_metadata=space_metadata,
    )


def append_to_note(
    config: NotelyConfig,
    existing: Note,
    new_body: str,
    new_raw: str,
    new_tags: list[str] | None = None,
    new_participants: list[str] | None = None,
    new_summary: str | None = None,
) -> Note:
    """Append new content to an existing note. Returns the updated Note."""
    # Append body with separator
    separator = f"\n\n---\n\n"
    existing.body = existing.body.rstrip() + separator + new_body

    # Append raw text
    if new_raw:
        existing.raw_text = (existing.raw_text or "").rstrip() + separator + new_raw

    # Merge tags (union, preserve order)
    if new_tags:
        seen = set(existing.tags)
        for tag in new_tags:
            if tag not in seen:
                existing.tags.append(tag)
                seen.add(tag)

    # Merge participants
    if new_participants:
        seen = set(existing.participants)
        for p in new_participants:
            if p not in seen:
                existing.participants.append(p)
                seen.add(p)

    # Update summary if provided
    if new_summary:
        existing.summary = new_summary

    existing.updated = datetime.now(timezone.utc).isoformat()

    # Write updated note
    write_note(config, existing)
    return existing


def delete_note_files(config: NotelyConfig, relative_path: str) -> None:
    """Delete a note's .md file, raw text file, and any binary originals in .raw/."""
    md = absolute_path(config, relative_path)
    if md.exists():
        md.unlink()
    # New location — .txt raw
    raw = raw_file_path(config, relative_path)
    if raw.exists():
        raw.unlink()
    # Binary originals in .raw/ (same stem, any extension)
    raw_dir = raw.parent
    stem = Path(relative_path).stem  # e.g. "2026-02-23_meeting-notes"
    if raw_dir.is_dir():
        for f in raw_dir.glob(f"{stem}.*"):
            if f != raw:  # .txt already handled above
                f.unlink()
    # Legacy locations
    legacy_raw = md.with_suffix(".raw.md")
    if legacy_raw.exists():
        legacy_raw.unlink()


def read_all_notes(config: NotelyConfig) -> list[Note]:
    """Read all notes from disk. Used by reindex."""
    notes = []
    if not config.notes_dir.exists():
        return notes

    for md_file in config.notes_dir.rglob("*.md"):
        # Skip raw files and index files
        if md_file.name.endswith(".raw.md"):
            continue
        if md_file.name.startswith("_"):
            continue
        relative = str(md_file.relative_to(config.notes_dir))
        note = read_note(config, relative)
        if note and note.id:
            notes.append(note)
    return notes


def write_index_file(config: NotelyConfig, name: str, headers: list[str], rows: list[list[str]]) -> Path:
    """Write an auto-generated CSV index file.

    Plain CSV — opens in Excel, Sheets, Numbers, or any text editor.
    Stays in sync with the database automatically.
    """
    import csv
    import io

    path = config.base_dir / f"_{name}.csv"

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(headers)
    for row in rows:
        writer.writerow(row)

    path.write_text(buf.getvalue(), encoding="utf-8")
    return path


def update_action_status(
    config: NotelyConfig,
    db: "Database",
    item_id: int,
    new_status: str,
) -> dict[str, Any] | None:
    """Update a todo's status.

    Todos are DB-only (snippets table) — just update the row and sync the CSV index.
    Returns the todo row dict if found, None otherwise.
    """
    row = db.get_todo(item_id)
    if not row:
        return None

    if row["status"] == new_status:
        return row

    db.update_todo_status(item_id, new_status)
    sync_todo_index(config, db)
    return row


def update_action_owner(
    config: NotelyConfig,
    db: "Database",
    item_id: int,
    new_owner: str,
) -> dict[str, Any] | None:
    """Update a todo's owner.

    Todos are DB-only (snippets table) — just update the row and sync the CSV index.
    Returns the todo row dict if found, None otherwise.
    """
    row = db.get_todo(item_id)
    if not row:
        return None

    db.update_todo_owner(item_id, new_owner)
    sync_todo_index(config, db)
    return row


def merge_duplicate_todos(
    config: NotelyConfig,
    db: "Database",
    cluster: list[dict[str, Any]],
    merged_task: str,
    merged_due: str | None,
    source_ref: str,
) -> int:
    """Merge a cluster of duplicate todos into one standalone item.

    Marks all originals as done (via one-way markdown flow), creates a new
    standalone todo with the merged task text and source references.
    Returns the new todo's ID.
    """
    # Mark all originals as done
    for item in cluster:
        update_action_status(config, db, item["id"], "done")

    # Build task text with source refs
    task_text = merged_task
    if source_ref:
        task_text = f"{merged_task} ({source_ref})"

    # Create standalone merged todo
    owner = cluster[0].get("owner", "")
    new_id = db.add_todo(
        owner=owner,
        task=task_text,
        due=merged_due,
    )

    # Sync CSV
    sync_todo_index(config, db)

    return new_id


def sync_todo_index(config: NotelyConfig, db: "Database") -> Path:
    """Regenerate _todos.csv from current todos (snippets table)."""
    items = db.get_open_todos()
    headers = ["Status", "Task", "Owner", "Due", "Space", "From"]
    rows = []

    for item in items:
        note_title = item.get("note_title", "")
        space = item.get("space", "")
        group_slug = item.get("group_slug", "")
        from_str = space
        if group_slug:
            from_str += f" / {group_slug}"
        if note_title and note_title != "(standalone)":
            from_str += f" / {note_title}"

        rows.append([
            item["status"],
            item["task"],
            item["owner"],
            item.get("due") or "",
            space,
            from_str,
        ])

    return write_index_file(config, "todos", headers, rows)


def sync_ideas_index(config: NotelyConfig, db: "Database") -> Path:
    """Regenerate _ideas.md from current ideas notes."""
    from .db import safe_json_loads, safe_parse_tags

    ideas_space = config.find_ideas_space()
    if not ideas_space:
        return None

    rows_raw = db.conn.execute(
        "SELECT id, title, date, summary, tags, space_metadata FROM notes WHERE space = ? ORDER BY date DESC",
        (ideas_space,),
    ).fetchall()

    if not rows_raw:
        return None

    headers = ["Status", "Title", "Category", "Date", "Tags", "Summary"]
    rows = []

    for r in rows_raw:
        meta = safe_json_loads(r["space_metadata"])
        tags = safe_parse_tags(r["tags"])
        rows.append([
            meta.get("content_status", "seed"),
            r["title"],
            meta.get("category_display") or meta.get("category", ""),
            r["date"],
            ", ".join(tags[:3]),
            r["summary"][:80],
        ])

    return write_index_file(config, "ideas", headers, rows)


def _note_to_frontmatter(note: Note) -> dict[str, Any]:
    """Convert a Note to frontmatter dict.

    System-only fields (created, refinement, input_size) live in the DB
    only — not written to frontmatter so users can't accidentally break them.
    """
    fm: dict[str, Any] = {
        "id": note.id,
        "space": note.space,
        "title": note.title,
        "source": note.source,
        "date": note.date,
        "updated": note.updated,
        "summary": note.summary,
        "tags": note.tags,
        "participants": note.participants,
    }

    # Include space-specific fields at top level for readability
    for key, value in note.space_metadata.items():
        fm[key] = value

    if note.source_url:
        fm["source_url"] = note.source_url

    if note.related_contexts:
        fm["related_contexts"] = note.related_contexts

    if note.attachments:
        fm["attachments"] = note.attachments

    return fm


# ---------------------------------------------------------------------------
# Shared helpers — used by both open_cmd.py and dump.py
# ---------------------------------------------------------------------------


# Input size thresholds (chars)
INPUT_SIZE_SMALL = 500
INPUT_SIZE_MEDIUM = 10_000


def build_space_metadata(
    config: NotelyConfig,
    space: str,
    group_slug: str,
    group_display: str,
    subgroup_slug: str | None = None,
    subgroup_display: str | None = None,
    extra: dict | None = None,
) -> dict:
    """Build the space_metadata dict for a note from routing info.

    Uses the space config to determine key names (client, category, etc.)
    and populates group/subgroup fields accordingly.
    """
    space_metadata: dict = {}
    space_cfg = config.get_space(space)
    if space_cfg:
        space_metadata[space_cfg.group_by] = group_slug
        space_metadata[f"{space_cfg.group_by}_display"] = group_display
        if subgroup_slug and space_cfg.subgroup_by:
            space_metadata[space_cfg.subgroup_by] = subgroup_slug
            space_metadata[f"{space_cfg.subgroup_by}_display"] = subgroup_display or subgroup_slug
    if extra:
        space_metadata.update(extra)
    return space_metadata


def classify_input_size(text: str) -> InputSize:
    """Classify raw input text by length for metadata tracking.

    Returns:
        InputSize.SMALL for <INPUT_SIZE_SMALL, MEDIUM for <INPUT_SIZE_MEDIUM,
        LARGE for >=INPUT_SIZE_MEDIUM.
    """
    length = len(text)
    if length < INPUT_SIZE_SMALL:
        return InputSize.SMALL
    if length < INPUT_SIZE_MEDIUM:
        return InputSize.MEDIUM
    return InputSize.LARGE


def preview_and_save_records(
    config: NotelyConfig,
    db: "Database",
    note: Note,
    action_items: list | None = None,
    extracted_records: list[dict] | None = None,
    console: Any | None = None,
    auto_confirm: bool = False,
) -> bool:
    """Show extracted records preview and save after user confirmation.

    Two-step flow: the note is already saved. This shows action items and
    database records extracted alongside the note. User confirms via
    ``confirm_action`` (same verb-centric UX as the note preview).

    Args:
        auto_confirm: If True, save without prompting (for --yes / scripted use).

    Returns True if records were saved, False if skipped/cancelled.
    """
    from rich.console import Console as RichConsole
    from rich.panel import Panel
    from rich.prompt import Prompt

    from .prompts import confirm_action

    if console is None:
        console = RichConsole()

    items = list(action_items or [])
    records = list(extracted_records or [])

    if not items and not records:
        return False

    # --- Diff against DB: split into new vs already-saved ---
    # Lower threshold (0.60) than /todo dedup (0.80) because AI rephrases
    # the same task differently each extraction — e.g. "Invite Chloe to
    # Slack channel" vs "Send Slack channel invites to Chloe" (0.40 with
    # strict matching). False positives are cheap here (user sees "already
    # saved" count, can still edit/revise).
    from .dedup import task_similarity
    _RECORD_DEDUP_THRESHOLD = 0.55

    existing_todos = db.get_note_todos(note.id) if note.id else []

    existing_items: list = []
    new_items: list = []
    for item in items:
        is_dup = False
        for et in existing_todos:
            # Match on task text alone — AI often assigns the same task to
            # different owners across extractions (non-deterministic).
            if task_similarity(item.task, et.get("task", "")) > _RECORD_DEDUP_THRESHOLD:
                is_dup = True
                break
        if is_dup:
            existing_items.append(item)
        else:
            new_items.append(item)

    existing_records: list[dict] = []
    new_records: list[dict] = []
    for rec in records:
        stype = rec.get("snippet_type", "")
        entity = rec.get("entity", "")
        key = rec.get("key", "")
        if stype and entity and key:
            found = db.find_existing_snippet(entity, key, stype)
            if found:
                existing_records.append(rec)
            else:
                new_records.append(rec)
        else:
            new_records.append(rec)

    # If everything is already saved, tell user and return early
    if not new_items and not new_records:
        n = len(existing_items) + len(existing_records)
        console.print(f"[dim]All {n} record(s) already saved.[/dim]")
        return False

    # Work with new items only — existing ones are skipped
    items = new_items
    records = new_records

    # Keep originals for recovery (e.g. "bring them back" after dropping)
    original_items = list(items)
    original_records = list(records)
    # Track revision history so AI understands the chain of changes
    revision_history: list[str] = []

    def _show_preview():
        lines: list[str] = []
        if items:
            lines.append(f"[bold]New actions:[/bold]  {len(items)} item(s)")
            for item in items:
                due_str = f" (due {item.due})" if item.due else ""
                lines.append(f"    [green]+[/green] [cyan]{item.owner}[/cyan]: {item.task}{due_str}")
        if records:
            lines.append(f"[bold]New records:[/bold]  {len(records)} item(s)")
            for rec in records:
                stype = rec.get("snippet_type", "")
                entity = rec.get("entity", "")
                key = rec.get("key", "")
                value = rec.get("value", "")
                lines.append(f"    [green]+[/green] [cyan]{stype}[/cyan]: {entity}.{key} = {value}")
        if existing_items or existing_records:
            n = len(existing_items) + len(existing_records)
            lines.append(f"[dim]Already saved: {n} record(s)[/dim]")
        console.print(Panel("\n".join(lines), title="Extracted Records", border_style="cyan"))

    if auto_confirm:
        _show_preview()
    else:
        def _records_edit():
            _edit_records(items, records, console)
            if not items and not records:
                console.print("[yellow]All records dropped.[/yellow]")

        def _records_revise():
            instruction = Prompt.ask("[dim]What should change?[/dim]")
            if instruction.strip():
                console.print("[dim]Revising records...[/dim]")
                _revise_records(
                    items, records, instruction.strip(), console,
                    original_items=original_items,
                    original_records=original_records,
                    user_name=config.user_name,
                    note=note,
                    revision_history=revision_history,
                )
                revision_history.append(instruction.strip())
                if not items and not records:
                    console.print("[yellow]All records dropped.[/yellow]")

        confirmed = confirm_action(
            _show_preview,
            verb="save",
            edit_fn=_records_edit,
            revise_fn=_records_revise,
            console=console,
        )
        if not confirmed or (not items and not records):
            return False

    # Save action items + records (already deduped above)
    space = note.space
    parts = note.file_path.split("/")
    group_slug = parts[1] if len(parts) > 1 else ""

    if items:
        db.add_todos_for_note(note.id, items, space, group_slug)

    if records:
        _save_extracted_records(db, records, space, group_slug, note.id)

    # Sync CSVs
    if items:
        sync_todo_index(config, db)
    sync_database_indexes(config, db)

    saved_count = len(items) + len(records)
    console.print(f"[green]Saved {saved_count} record(s).[/green]")
    return True


def _edit_records(items: list, records: list, console: Any) -> None:
    """Open records in $EDITOR. Mutates items and records lists in place.

    Renders all items as a simple text format. User edits fields, deletes
    blocks to drop items. Parsed back on save.
    """
    import os
    import subprocess
    import tempfile

    from .models import ActionItem

    # Build editable text
    lines: list[str] = []
    lines.append("# Edit records below. Delete a block to drop it.")
    lines.append("# Save and close to apply changes.")
    lines.append("")

    for i, item in enumerate(items, 1):
        lines.append(f"## Action {i}")
        lines.append(f"owner: {item.owner}")
        lines.append(f"task: {item.task}")
        lines.append(f"due: {item.due or ''}")
        lines.append("")

    for i, rec in enumerate(records, len(items) + 1):
        lines.append(f"## Record {i}")
        lines.append(f"type: {rec.get('snippet_type', '')}")
        lines.append(f"entity: {rec.get('entity', '')}")
        lines.append(f"key: {rec.get('key', '')}")
        lines.append(f"value: {rec.get('value', '')}")
        lines.append("")

    content = "\n".join(lines)

    # Write to temp file and open in editor
    editor = os.environ.get("EDITOR", "vi")
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".txt", prefix="notely-records-", delete=False,
    ) as f:
        f.write(content)
        tmp_path = f.name

    try:
        subprocess.run([editor, tmp_path], check=True)

        with open(tmp_path, "r", encoding="utf-8") as f:
            edited = f.read()
    except Exception as e:
        console.print(f"[red]Editor failed: {e}[/red]")
        return
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass

    if edited.strip() == content.strip():
        console.print("[dim]No changes.[/dim]")
        return

    # Parse edited text back into items and records
    new_items: list = []
    new_records: list[dict] = []

    blocks = edited.split("##")
    for block in blocks:
        block = block.strip()
        if not block:
            continue

        # Parse key: value lines
        fields: dict[str, str] = {}
        block_type = ""
        for line in block.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line.lower().startswith("action"):
                block_type = "action"
                continue
            if line.lower().startswith("record"):
                block_type = "record"
                continue
            if ": " in line:
                k, v = line.split(": ", 1)
                fields[k.strip().lower()] = v.strip()
            elif line.endswith(":"):
                # Handle "due:" with no value
                fields[line[:-1].strip().lower()] = ""

        if block_type == "action" and fields.get("task"):
            new_items.append(ActionItem(
                owner=fields.get("owner", "me"),
                task=fields["task"],
                due=fields.get("due") or None,
            ))
        elif block_type == "record" and fields.get("entity"):
            new_records.append({
                "snippet_type": fields.get("type", "fact"),
                "entity": fields["entity"],
                "key": fields.get("key", ""),
                "value": fields.get("value", ""),
            })

    items.clear()
    items.extend(new_items)
    records.clear()
    records.extend(new_records)


def _revise_records(
    items: list, records: list, instruction: str, console: Any,
    original_items: list | None = None,
    original_records: list[dict] | None = None,
    user_name: str = "",
    note: "Note | None" = None,
    revision_history: list[str] | None = None,
) -> None:
    """Use AI to revise extracted records based on user instruction.

    Sends current records + original records + note context + revision
    history so the AI has full context for filtering, restoring, and
    understanding who "me" refers to.
    Mutates items and records lists in place.
    """
    import anthropic

    from .models import ActionItem

    # Build a compact representation of current records
    current: list[dict] = []
    for item in items:
        current.append({
            "type": "action",
            "owner": item.owner,
            "task": item.task,
            "due": item.due or "",
        })
    for rec in records:
        current.append({
            "type": "record",
            "snippet_type": rec.get("snippet_type", ""),
            "entity": rec.get("entity", ""),
            "key": rec.get("key", ""),
            "value": rec.get("value", ""),
        })

    # Build original records for recovery context
    original: list[dict] = []
    if original_items:
        for item in original_items:
            original.append({
                "type": "action",
                "owner": item.owner,
                "task": item.task,
                "due": item.due or "",
            })
    if original_records:
        for rec in original_records:
            original.append({
                "type": "record",
                "snippet_type": rec.get("snippet_type", ""),
                "entity": rec.get("entity", ""),
                "key": rec.get("key", ""),
                "value": rec.get("value", ""),
            })

    tool = {
        "name": "update_records",
        "description": "Return the revised list of records.",
        "input_schema": {
            "type": "object",
            "required": ["records"],
            "properties": {
                "records": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "type": {
                                "type": "string", "enum": ["action", "record"],
                                "description": "'action' for todos/tasks, 'record' for database entries",
                            },
                            "owner": {"type": "string", "description": "Action only: who owns the task"},
                            "task": {"type": "string", "description": "Action only: the task text"},
                            "due": {"type": "string", "description": "Action only: due date YYYY-MM-DD"},
                            "snippet_type": {"type": "string", "description": "Record only: database name (e.g. 'contacts', 'providers')"},
                            "entity": {"type": "string", "description": "Record only: entity name (e.g. person or org)"},
                            "key": {"type": "string", "description": "Record only: field name (e.g. 'npi', 'phone')"},
                            "value": {"type": "string", "description": "Record only: the data value"},
                        },
                        "required": ["type"],
                    },
                },
            },
        },
    }

    # Build system prompt with context
    system_parts = [
        "You are editing a list of extracted records from a note. "
        "Two record types: 'action' (todo with owner/task/due) and "
        "'record' (database entry with snippet_type/entity/key/value).",
    ]
    if user_name:
        system_parts.append(
            f'The current user is "{user_name}". '
            '"me" / "my" / "mine" / "assigned to me" refers to this person.'
        )
    if note:
        system_parts.append(
            f"Note context — Title: {note.title}"
            + (f", Summary: {note.summary}" if note.summary else "")
            + (f", Participants: {', '.join(note.participants)}" if note.participants else "")
        )
    system_parts.append(
        "Apply the user's instruction precisely: add, remove, filter, or modify items. "
        "Return the full updated list via the update_records tool. "
        "To drop an item, simply omit it from the list. "
        "If the user asks to restore/bring back items, use the original records."
    )

    # Build message content
    msg_parts = []
    if original and original != current:
        msg_parts.append(f"Original records (can be restored):\n{json.dumps(original, indent=2)}\n")
    msg_parts.append(f"Current records:\n{json.dumps(current, indent=2)}\n")
    if revision_history:
        msg_parts.append("Previous instructions applied:\n" + "\n".join(
            f"  {i+1}. {h}" for i, h in enumerate(revision_history)
        ) + "\n")
    msg_parts.append(f"New instruction: {instruction}")

    try:
        client = anthropic.Anthropic()
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1024,
            system="\n".join(system_parts),
            messages=[{
                "role": "user",
                "content": "\n".join(msg_parts),
            }],
            tools=[tool],
            tool_choice={"type": "tool", "name": "update_records"},
        )
    except Exception as e:
        console.print(f"[red]AI revision failed: {e}[/red]")
        return

    # Parse response
    for block in response.content:
        if block.type == "tool_use" and block.name == "update_records":
            revised = block.input.get("records", [])
            break
    else:
        console.print("[red]AI returned no records.[/red]")
        return

    new_items: list = []
    new_records: list[dict] = []
    for rec in revised:
        if rec.get("type") == "action" and rec.get("task"):
            new_items.append(ActionItem(
                owner=rec.get("owner", "me"),
                task=rec["task"],
                due=rec.get("due") or None,
            ))
        elif rec.get("type") == "record" and rec.get("entity"):
            new_records.append({
                "snippet_type": rec.get("snippet_type", "fact"),
                "entity": rec["entity"],
                "key": rec.get("key", ""),
                "value": rec.get("value", ""),
            })

    items.clear()
    items.extend(new_items)
    records.clear()
    records.extend(new_records)


def _save_extracted_records(
    db: "Database",
    records: list[dict],
    space: str,
    group_slug: str,
    note_id: str,
) -> int:
    """Save extracted records from note structuring (auto-confirm mode).

    User already confirmed at the note preview, so records are saved silently.
    Handles dedup: skips records for unknown databases, auto-resolves entity
    names, and updates existing entity+key matches.

    Returns the number of records saved or updated.
    """
    saved = 0
    for item in records:
        entity = item.get("entity", "")
        key = item.get("key", "")
        value = item.get("value", "")
        stype = item.get("snippet_type", "fact")

        if not entity or not stype:
            continue

        # Guard against AI hallucinating databases that don't exist
        if not db.database_exists(stype):
            logger.debug("Skipping record for unknown database: %s", stype)
            continue

        # Value dedup — exact entity+key match
        existing = db.find_existing_snippet(entity, key, stype)
        if existing:
            if existing["value"] != value:
                db.update_snippet(existing["id"], value)
                saved += 1
            continue

        # Entity name dedup — auto-resolve to closest match
        similar = db.find_similar_entities(entity, stype)
        if similar:
            entity = similar[0]

        db.add_reference(
            space=space, group_slug=group_slug,
            entity=entity, key=key, value=value,
            snippet_type=stype, note_id=note_id,
        )
        saved += 1

    if saved:
        logger.debug("Saved %d extracted record(s)", saved)
    return saved


def save_and_sync(
    config: NotelyConfig,
    db: "Database",
    note: Note,
    hash_source: str | None = None,
    routing: Any = None,
    source_file: Path | None = None,
    action_items: list | None = None,
    extracted_records: list[dict] | None = None,
) -> None:
    """Full save pipeline: write markdown → upsert DB → sync vectors → sync CSVs.

    This is the single function for persisting a note and keeping all derived
    stores in sync. Use it instead of calling write_note/upsert_note/try_vector
    separately.

    Args:
        config: workspace config
        db: initialized Database instance
        note: the Note to save
        hash_source: raw paste text for duplicate detection hashing.
            Pass the paste content (not typed context) so hash lookups match.
            If None, hashing uses the note's raw_text field.
        routing: NoteRouting object (optional). If provided and the group/subgroup
            is new, indexes the directory in the vector store.
        source_file: optional original binary file (PDF/image) to copy into .raw/
            for provenance.
        action_items: list of ActionItem objects to insert into the DB linked
            to this note. Todos are DB-only, not stored in markdown.
        extracted_records: list of record dicts extracted alongside the note
            for databases marked 'auto-extract from notes'. Saved silently
            (user already confirmed at preview).
    """
    from .vectors import try_vector_sync_note

    # Step 1: write markdown (source of truth)
    write_note(config, note, source_file=source_file)

    # Step 2: upsert into SQLite index
    db.upsert_note(note, hash_source=hash_source)

    # Step 3: sync vectors (fire-and-forget — non-fatal if it fails)
    try:
        try_vector_sync_note(config, note)
    except Exception:
        pass

    # Step 4: index new directories if routing indicates a new group/subgroup
    if routing:
        from .routing import ensure_directory_indexed
        ensure_directory_indexed(config, db, routing, note_summary=note.summary)

    # Step 5: insert todos into DB (linked to this note, snippets table)
    if action_items:
        space = note.space
        # Derive group from file_path: "space/group/..." → "group"
        parts = note.file_path.split("/")
        group_slug = parts[1] if len(parts) > 1 else ""
        db.add_todos_for_note(note.id, action_items, space, group_slug)

    # Step 6: save extracted records (auto-extract from notes)
    if extracted_records:
        space = note.space
        parts = note.file_path.split("/")
        group_slug = parts[1] if len(parts) > 1 else ""
        _save_extracted_records(db, extracted_records, space, group_slug, note.id)

    # Step 7: sync CSV index files
    if action_items:
        sync_todo_index(config, db)
    sync_ideas_index(config, db)
    sync_database_indexes(config, db)


def edit_note_in_editor(note: Note) -> Note | None:
    """Open a Note in $EDITOR as a temp markdown file, parse it back on save.

    Writes the note's frontmatter + body to a temp file, opens the user's
    editor, and re-reads the result. Returns the updated Note if the user
    made changes, or None if they quit without saving.

    The note object is modified in-place and returned.
    """
    import os
    import subprocess
    import tempfile

    fm = _note_to_frontmatter(note)
    post = frontmatter.Post(note.body or "", **fm)
    content = frontmatter.dumps(post)

    # Write to temp file with the note's slug as filename hint
    stem = Path(note.file_path).stem if note.file_path else "note"
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=f"_{stem}.md", prefix="notely_",
        delete=False, encoding="utf-8",
    ) as tmp:
        tmp.write(content)
        tmp_path = tmp.name

    try:
        editor = os.environ.get("EDITOR", "vi")
        mtime_before = os.path.getmtime(tmp_path)
        subprocess.run([editor, tmp_path])
        mtime_after = os.path.getmtime(tmp_path)

        if mtime_after == mtime_before:
            return None  # No changes

        # Parse the edited file back
        edited_post = frontmatter.load(tmp_path)
        meta = dict(edited_post.metadata)

        note.title = meta.get("title", note.title)
        note.summary = meta.get("summary", note.summary)
        note.tags = meta.get("tags", note.tags)
        note.participants = meta.get("participants", note.participants)
        note.body = edited_post.content

        # Update space metadata fields that live at top level in frontmatter
        for key in ("client", "client_display", "topic", "topic_display",
                     "category", "category_display", "content_status",
                     "source_ref", "source_url"):
            if key in meta:
                note.space_metadata[key] = meta[key]

        note.refinement = Refinement.HUMAN_REVIEWED
        return note
    finally:
        Path(tmp_path).unlink(missing_ok=True)


def show_merge_preview(existing: Note, merge_result: dict, display: bool = True) -> bool:
    """Show a diff-style preview of what a merge will change.

    Compares merge_result against the existing note and displays a Rich panel
    with red/green diff for summary, body, tags, and participants.
    Action items and extracted records are NOT shown here — they get their
    own confirmation step via ``preview_and_save_records()``.

    Args:
        existing: the current Note being updated
        merge_result: dict with keys: updated_summary, updated_body,
            new_action_items, new_tags, new_participants
        display: If False, only check for changes without rendering.
            Use this to avoid double-rendering when ``confirm_action``
            will call the preview again.

    Returns:
        True if there are meaningful changes to apply, False if nothing changed.
    """
    from rich.console import Console
    from rich.panel import Panel

    console = Console()

    # Determine what actually changed
    old_summary = existing.summary or ""
    new_summary = merge_result.get("updated_summary", old_summary)
    summary_changed = new_summary != old_summary

    new_actions = merge_result.get("new_action_items", [])
    new_records = merge_result.get("new_extracted_records", [])
    has_records = bool(new_actions) or bool(new_records)

    new_tags = merge_result.get("new_tags", [])
    existing_tags = set(existing.tags)
    actually_new_tags = [t for t in new_tags if t not in existing_tags]

    new_participants = merge_result.get("new_participants", [])
    existing_participants = set(existing.participants)
    actually_new_p = [p for p in new_participants if p not in existing_participants]

    # Check if body was meaningfully updated (not just whitespace)
    old_body = (existing.body or "").strip()
    new_body = merge_result.get("updated_body", old_body).strip()
    body_changed = new_body != old_body

    # No meaningful changes — skip the diff panel entirely
    has_note_changes = summary_changed or actually_new_tags or actually_new_p or body_changed
    if not has_note_changes and not has_records:
        if display:
            console.print("[dim]No new notes or records to update.[/dim]")
        return False

    if not display:
        return True

    # Records only, no note-level changes — show what was extracted
    if not has_note_changes and has_records:
        console.print(f"[dim]No note changes for '{existing.title}', but records to extract:[/dim]")
        if new_actions:
            for item in new_actions:
                due_str = f" (due {item.due})" if item.due else ""
                console.print(f"  [cyan]{item.owner}[/cyan]: {item.task}{due_str}")
        if new_records:
            for rec in new_records:
                entity = rec.get("entity", "")
                key = rec.get("key", "")
                value = rec.get("value", "")
                console.print(f"  [cyan]{rec.get('snippet_type', '')}[/cyan]: {entity}.{key} = {value}")
        return True

    # Build diff display — note-level changes only
    lines = [
        f"[bold]Updating:[/bold] {existing.title}",
        "",
    ]

    if summary_changed:
        lines.append("[bold]Summary:[/bold]")
        lines.append(f"  [red]- {old_summary}[/red]")
        lines.append(f"  [green]+ {new_summary}[/green]")

    if body_changed:
        # Show what was added to the body
        added_lines = []
        old_lines = set(old_body.splitlines())
        for line in new_body.splitlines():
            if line.strip() and line not in old_lines:
                added_lines.append(line)
        if added_lines:
            lines.append("")
            lines.append(f"[bold]+ Body updated ({len(added_lines)} new lines):[/bold]")
            for al in added_lines:
                lines.append(f"  [green]+ {al[:200]}[/green]")

    if actually_new_tags:
        lines.append("")
        lines.append(f"[bold]+ Tags:[/bold] [green]{', '.join(actually_new_tags)}[/green]")
        if existing.tags:
            lines.append(f"  [dim](existing: {', '.join(existing.tags)})[/dim]")

    if actually_new_p:
        lines.append(f"[bold]+ People:[/bold] [green]{', '.join(actually_new_p)}[/green]")

    refs = merge_result.get("references", [])
    if refs:
        lines.append("")
        lines.append(f"[bold]+ References:[/bold] {len(refs)} detected")
        for ref in refs:
            lines.append(f"    {ref.entity}.{ref.key} = [cyan]{ref.value}[/cyan]")

    # Hint about records confirmed separately
    if has_records:
        count = len(new_actions) + len(new_records)
        lines.append("")
        lines.append(f"[dim]+ {count} record(s) will be confirmed separately[/dim]")

    console.print(Panel("\n".join(lines), title="Changes", border_style="yellow"))
    return True


def edit_merge_result(existing: Note, merge_result: dict) -> dict:
    """Let the user interactively edit the merge result before applying.

    Prompts for: summary edit, tag add/remove, action item drop.

    Args:
        existing: the current Note (tags may be modified in-place if user removes some)
        merge_result: dict with merge data (modified in-place and returned)

    Returns:
        The modified merge_result dict.
    """
    from rich.console import Console
    from rich.prompt import Prompt

    console = Console()

    # Edit summary
    new_summary = merge_result.get("updated_summary", existing.summary)
    merge_result["updated_summary"] = Prompt.ask("Summary", default=new_summary)

    # Edit tags — show combined (existing + new) for editing
    existing_tags = list(existing.tags)
    new_tags = merge_result.get("new_tags", [])
    actually_new = [t for t in new_tags if t not in set(existing_tags)]
    all_tags = existing_tags + actually_new
    tags_str = Prompt.ask("Tags", default=", ".join(all_tags))
    edited_tags = [t.strip() for t in tags_str.split(",") if t.strip()]
    # Separate new vs existing after edit
    merge_result["new_tags"] = [t for t in edited_tags if t not in set(existing_tags)]
    # Remove any existing tags the user deleted
    existing.tags = [t for t in existing_tags if t in set(edited_tags)]

    # Edit action items — let user drop ones they don't want
    new_actions = merge_result.get("new_action_items", [])
    if new_actions:
        console.print(f"\n[bold]New action items ({len(new_actions)}):[/bold]")
        for i, item in enumerate(new_actions, 1):
            due_str = f" (due {item.due})" if item.due else ""
            console.print(f"  {i}. [{item.owner}] {item.task}{due_str}")
        drop = Prompt.ask(
            "[dim]Drop items? (e.g. 2,5 or Enter to keep all)[/dim]",
            default="",
        )
        if drop.strip():
            drop_set = set()
            for part in drop.split(","):
                try:
                    drop_set.add(int(part.strip()) - 1)
                except ValueError:
                    pass
            merge_result["new_action_items"] = [
                a for i, a in enumerate(new_actions) if i not in drop_set
            ]

    return merge_result


def apply_merge(
    config: NotelyConfig,
    db: "Database",
    existing: Note,
    merge_result: dict,
    raw_text: str,
    paste_content: str | None = None,
) -> None:
    """Apply a merge result to an existing note and save via the full pipeline.

    Merges body, summary, tags, participants, and action items from merge_result
    into the existing Note, then writes to disk and syncs all derived stores.

    Args:
        config: workspace config
        db: initialized Database instance
        existing: the Note to update (modified in-place)
        merge_result: dict with updated_body, updated_summary, new_tags,
            new_participants, new_action_items
        raw_text: raw input text (appended to .raw for provenance)
        paste_content: the paste text for hash-based duplicate detection
    """
    # Apply merged content
    existing.body = merge_result["updated_body"]
    existing.summary = merge_result.get("updated_summary", existing.summary)

    # Append raw text for provenance
    if raw_text:
        separator = "\n\n---\n\n"
        existing.raw_text = (existing.raw_text or "").rstrip() + separator + raw_text

    # Merge tags (union, preserve order)
    if merge_result.get("new_tags"):
        seen = set(existing.tags)
        for tag in merge_result["new_tags"]:
            if tag not in seen:
                existing.tags.append(tag)
                seen.add(tag)

    # Merge participants (union)
    if merge_result.get("new_participants"):
        seen = set(existing.participants)
        for p in merge_result["new_participants"]:
            if p not in seen:
                existing.participants.append(p)
                seen.add(p)

    existing.updated = datetime.now(timezone.utc).isoformat()

    # Save note via the full pipeline — records returned for separate confirmation
    save_and_sync(config, db, existing, hash_source=paste_content)

    # Return records for caller to confirm and save separately
    new_actions = merge_result.get("new_action_items") or []
    new_records = merge_result.get("new_extracted_records") or []
    return new_actions, new_records


def handle_todo_dedup(
    config: NotelyConfig,
    db: "Database",
    clusters: list[list[dict]],
) -> bool:
    """Interactive dedup for duplicate todo clusters.

    Shows each cluster of potential duplicates and offers merge/skip/skip-all.
    Merge marks all originals as done and creates one standalone merged todo.

    Args:
        config: workspace config
        db: initialized Database instance (must stay open for merges)
        clusters: list of duplicate clusters from find_duplicate_clusters()

    Returns:
        True if any merges happened, False otherwise.
    """
    from rich.console import Console
    from rich.panel import Panel
    from rich.prompt import Prompt

    from .dedup import build_source_refs, pick_best_task, pick_earliest_due

    console = Console()
    console.print(f"\n[yellow]Found {len(clusters)} possible duplicate group(s)[/yellow]\n")
    merged_any = False

    for cluster in clusters:
        lines = []
        for item in cluster:
            title = item.get("note_title", "(standalone)")
            if len(title) > 25:
                title = title[:22] + "..."
            lines.append(f"  #{item['id']}  {item['task']}  [dim]({title})[/dim]")

        panel = Panel(
            "\n".join(lines),
            title=f"Possible duplicates ({len(cluster)} items)",
            border_style="yellow",
        )
        console.print(panel)

        try:
            choice = Prompt.ask(
                r"\[m]erge / \[s]kip / \[S]kip all",
                default="s",
            )
        except (KeyboardInterrupt, EOFError):
            break

        if choice == "S":
            break
        if choice != "m":
            continue

        best_task = pick_best_task(cluster)
        try:
            edited_task = Prompt.ask("Task", default=best_task)
        except (KeyboardInterrupt, EOFError):
            continue

        merged_due = pick_earliest_due(cluster)
        source_ref = build_source_refs(cluster)
        new_id = merge_duplicate_todos(config, db, cluster, edited_task, merged_due, source_ref)
        console.print(f"[green]Merged → #{new_id}[/green]")
        merged_any = True

    if merged_any:
        console.print()

    return merged_any


def _render_list_preview(
    console: Any,
    items: list[dict],
    item_type: str,
) -> None:
    """Render a preview panel for list items (todos or ideas)."""
    from rich.panel import Panel

    label = "Todo" if item_type == "todo" else "Idea"
    lines = [f"[bold]Adding {len(items)} {label}(s):[/bold]", ""]

    for i, item in enumerate(items, 1):
        if item_type == "todo":
            owner = item.get("owner", "me")
            due = f" · due {item['due']}" if item.get("due") else ""
            context = ""
            if item.get("space") or item.get("group"):
                parts = [p for p in [item.get("space"), item.get("group")] if p]
                context = f" [dim][{' / '.join(parts)}][/dim]"
            lines.append(f"  {i}. {item['text']}    [cyan]{owner}[/cyan]{due}{context}")
        else:
            tags = ", ".join(item.get("tags", []))
            cat = f" [dim]({item['group']})[/dim]" if item.get("group") else ""
            lines.append(f"  {i}. {item['text']}{cat}")
            if item.get("summary"):
                lines.append(f"     [dim]{item['summary'][:80]}[/dim]")
            if tags:
                lines.append(f"     [dim]tags: {tags}[/dim]")

    console.print(Panel("\n".join(lines), title=f"Quick {label}s", border_style="blue"))


def _edit_list_items_in_editor(
    items: list[dict],
    item_type: str,
) -> list[dict] | None:
    """Open list items in $EDITOR as YAML. Returns updated items or None if cancelled."""
    import os
    import subprocess
    import tempfile

    import yaml

    # Build editable YAML
    if item_type == "todo":
        editable = []
        for item in items:
            entry: dict[str, Any] = {"task": item.get("text", item.get("task", ""))}
            if item.get("owner"):
                entry["owner"] = item["owner"]
            if item.get("due"):
                entry["due"] = item["due"]
            editable.append(entry)
    else:
        editable = []
        for item in items:
            entry = {"text": item.get("text", "")}
            if item.get("tags"):
                entry["tags"] = item["tags"]
            if item.get("summary"):
                entry["summary"] = item["summary"]
            editable.append(entry)

    header = f"# Edit {item_type}s. Delete entries to remove. Save and close.\n"
    content = header + yaml.dump(editable, default_flow_style=False, allow_unicode=True)

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=f"_notely_{item_type}s.yaml", prefix="notely_",
        delete=False, encoding="utf-8",
    ) as tmp:
        tmp.write(content)
        tmp_path = tmp.name

    try:
        editor = os.environ.get("EDITOR", "vi")
        mtime_before = os.path.getmtime(tmp_path)
        subprocess.run([editor, tmp_path])
        mtime_after = os.path.getmtime(tmp_path)

        if mtime_after == mtime_before:
            return None  # No changes

        edited_text = Path(tmp_path).read_text(encoding="utf-8")
        # Strip comment header
        lines = edited_text.splitlines()
        body_lines = [l for l in lines if not l.strip().startswith("#")]
        body = "\n".join(body_lines).strip()
        if not body:
            return []

        parsed = yaml.safe_load(body)
        if not isinstance(parsed, list):
            return None

        # Map back to the expected item format
        result = []
        for entry in parsed:
            if not isinstance(entry, dict):
                continue
            if item_type == "todo":
                result.append({
                    "text": entry.get("task", entry.get("text", "")),
                    "owner": entry.get("owner", "me"),
                    "due": entry.get("due"),
                })
            else:
                result.append({
                    "text": entry.get("text", ""),
                    "tags": entry.get("tags", []),
                    "summary": entry.get("summary"),
                })
        return result
    finally:
        Path(tmp_path).unlink(missing_ok=True)


def confirm_and_save_list_items(
    config: NotelyConfig,
    db: "Database",
    result: dict,
    auto_confirm: bool = False,
) -> bool:
    """Preview list items (todos/ideas) and save after user confirmation.

    Supports editing in $EDITOR, AI revision, and dropping specific items
    before saving — matching the note preview UX.

    Args:
        config: workspace config
        db: initialized Database instance
        result: dict with "item_type" ("todo"/"idea") and "items" list
        auto_confirm: skip the confirmation prompt (for --yes flag)

    Returns:
        True if items were saved, False if cancelled or empty.
    """
    from rich.console import Console
    from rich.prompt import Prompt

    from .prompts import confirm_action

    console = Console()
    item_type = result.get("item_type", "todo")
    state = {"items": result.get("items", [])}

    if not state["items"]:
        console.print("[yellow]AI returned no items.[/yellow]")
        return False

    if not auto_confirm:
        def preview():
            _render_list_preview(console, state["items"], item_type)

        def edit():
            edited = _edit_list_items_in_editor(state["items"], item_type)
            if edited is not None:
                if not edited:
                    console.print("[yellow]All items removed. Cancelled.[/yellow]")
                    state["items"] = []
                    return
                state["items"] = edited
            else:
                console.print("[dim]No changes made.[/dim]")

        def revise():
            try:
                instruction = Prompt.ask("[dim]What to change[/dim]", default="")
            except (KeyboardInterrupt, EOFError):
                return
            if instruction.strip():
                from .ai import revise_list_items
                console.print("[dim]Revising...[/dim]")
                revised = revise_list_items(state["items"], item_type, instruction)
                if revised:
                    state["items"] = revised
                else:
                    console.print("[yellow]Revision returned no items.[/yellow]")

        def drop():
            try:
                nums = Prompt.ask("[dim]Drop (numbers, comma-separated)[/dim]", default="")
            except (KeyboardInterrupt, EOFError):
                return True
            if nums.strip():
                drop_set = set()
                for part in nums.split(","):
                    try:
                        drop_set.add(int(part.strip()) - 1)
                    except ValueError:
                        pass
                state["items"] = [it for i, it in enumerate(state["items"]) if i not in drop_set]
                console.print(f"[dim]Dropped {len(drop_set)} item(s).[/dim]")
                if not state["items"]:
                    console.print("[yellow]All items dropped. Cancelled.[/yellow]")
                    return False
            return True

        confirmed = confirm_action(
            preview, verb="save all",
            edit_fn=edit, revise_fn=revise, drop_fn=drop,
            console=console,
        )
        if not confirmed or not state["items"]:
            return False
    else:
        _render_list_preview(console, state["items"], item_type)

    # Save items
    items = state["items"]
    if item_type == "todo":
        for item in items:
            item_id = db.add_todo(
                owner=item.get("owner", "me"),
                task=item.get("text", item.get("task", "")),
                due=item.get("due"),
                space=item.get("space"),
                group_slug=item.get("group"),
            )
            console.print(f"  [green]Added todo #{item_id}:[/green] {item.get('text', item.get('task', ''))}")
        sync_todo_index(config, db)
    else:
        for item in items:
            note_id = db.add_standalone_idea(
                title=item.get("text", ""),
                summary=item.get("summary") or item.get("text", ""),
                category=item.get("group"),
                tags=item.get("tags"),
            )
            console.print(f"  [green]Added idea {note_id}:[/green] {item.get('text', '')}")
        sync_ideas_index(config, db)

    return True


def confirm_new_database(db: Any, name: str) -> str | None:
    """Prompt user to create a new snippet database with description and fields.

    When ``name`` is empty, shows existing databases to pick from or lets user
    type a new name (the "new" sentinel flow).  When ``name`` is a specific
    name, confirms creation of that database.

    Returns the database name on success, None if declined/cancelled.
    The db connection must already be initialized.
    """
    from rich.console import Console
    from rich.prompt import Prompt

    from .prompts import pick_from_list

    console = Console()

    try:
        # --- "new" sentinel: let user pick existing or name a new one ---
        if not name:
            existing = db.get_database_names()
            if existing:
                console.print("[bold]Which database?[/bold]")
                items = [(n, n) for n in existing]
                choice = pick_from_list(
                    items,
                    extras=[("n", "Create new database"), ("s", "Skip")],
                    console=console,
                )
                if choice is None or choice == "s":
                    return None
                if choice == "n":
                    # Fall through to creation below
                    pass
                else:
                    try:
                        idx = int(choice) - 1
                        if 0 <= idx < len(existing):
                            return existing[idx]
                    except ValueError:
                        pass
                    return None

            # No existing databases or user chose "create new"
            name = Prompt.ask("[dim]Database name (lowercase, e.g. contacts, vendors)[/dim]", default="").strip()
            if not name:
                return None
            name = name.lower().replace(" ", "-")
            if db.database_exists(name):
                return name  # already exists, just use it

        # --- Set up the new database ---
        console.print(f"[dim]Creating '{name}' database...[/dim]")
        desc = Prompt.ask(
            "[dim]Description (what's this database for?)[/dim]",
            default="",
        )
        fields_raw = Prompt.ask(
            "[dim]Expected fields (comma-separated, e.g. email, phone, role)[/dim]",
            default="",
        )
        extract = Prompt.ask(
            r"[dim]Auto-extract from future notes?[/dim]",
            choices=["y", "n"],
            default="n",
        )
        if desc.strip():
            db.set_database_description(name, desc.strip())
        if fields_raw.strip():
            fields = [f.strip().lower() for f in fields_raw.split(",") if f.strip()]
            if fields:
                db.set_database_fields(name, fields)
        if extract == "y":
            db.set_database_meta(name, "extract_from_notes", "true")
        return name
    except (KeyboardInterrupt, EOFError):
        return None


def _render_snippet_preview(
    console: Any,
    items: list[dict],
) -> None:
    """Render a preview panel for snippet items."""
    from rich.panel import Panel

    lines = [f"[bold]Saving {len(items)} snippet(s):[/bold]", ""]
    for i, item in enumerate(items, 1):
        stype = item.get("snippet_type", "fact")
        desc = f" — {item['description']}" if item.get("description") else ""
        lines.append(
            f"  {i}. [cyan]{item['entity']}[/cyan].{item['key']}"
            f" = {item['value']} [dim]({stype}){desc}[/dim]"
        )
    console.print(Panel("\n".join(lines), title="Quick Snippets", border_style="blue"))


def _edit_snippets_in_editor(
    items: list[dict],
) -> list[dict] | None:
    """Open snippets in $EDITOR as YAML. Returns updated items or None if cancelled."""
    import os
    import subprocess
    import tempfile

    import yaml

    editable = []
    for item in items:
        entry: dict[str, Any] = {
            "entity": item.get("entity", ""),
            "key": item.get("key", ""),
            "value": item.get("value", ""),
            "type": item.get("snippet_type", "fact"),
        }
        if item.get("description"):
            entry["description"] = item["description"]
        editable.append(entry)

    header = "# Edit snippets. Delete entries to remove. Save and close.\n"
    content = header + yaml.dump(editable, default_flow_style=False, allow_unicode=True)

    with tempfile.NamedTemporaryFile(
        mode="w", suffix="_notely_snippets.yaml", prefix="notely_",
        delete=False, encoding="utf-8",
    ) as tmp:
        tmp.write(content)
        tmp_path = tmp.name

    try:
        editor = os.environ.get("EDITOR", "vi")
        mtime_before = os.path.getmtime(tmp_path)
        subprocess.run([editor, tmp_path])
        mtime_after = os.path.getmtime(tmp_path)

        if mtime_after == mtime_before:
            return None  # No changes

        edited_text = Path(tmp_path).read_text(encoding="utf-8")
        lines = edited_text.splitlines()
        body_lines = [l for l in lines if not l.strip().startswith("#")]
        body = "\n".join(body_lines).strip()
        if not body:
            return []

        parsed = yaml.safe_load(body)
        if not isinstance(parsed, list):
            return None

        result = []
        for entry in parsed:
            if not isinstance(entry, dict):
                continue
            result.append({
                "entity": entry.get("entity", ""),
                "key": entry.get("key", ""),
                "value": str(entry.get("value", "")),
                "snippet_type": entry.get("type", entry.get("snippet_type", "fact")),
                "description": entry.get("description", ""),
            })
        return result
    finally:
        Path(tmp_path).unlink(missing_ok=True)


def confirm_and_save_snippets(
    config: NotelyConfig,
    db: "Database",
    data: dict,
    space: str = "",
    group_slug: str = "",
    auto_confirm: bool = False,
) -> bool:
    """Preview snippet items, confirm database, dedup entities, and save.

    Simple flow:
    1. Show preview of what AI extracted
    2. Confirm database: "Save to 'X'? [Y]es / [n]ew database / [s]kip"
    3. Entity dedup: "Looks similar to 'Y'. [u]pdate / [s]kip / [n]ew"
    4. Save

    Per-item ``space`` and ``group`` fields override the caller-level defaults.

    Returns:
        True if snippets were saved, False if cancelled or empty.
    """
    from rich.console import Console
    from rich.prompt import Prompt

    console = Console()
    items = data.get("items", [])
    if not items:
        console.print("[yellow]AI returned no snippets.[/yellow]")
        return False

    # Show preview
    _render_snippet_preview(console, items)

    if not auto_confirm:
        # Confirm before saving
        try:
            choice = Prompt.ask(
                r"\[Y]es, save / \[n]o, skip", default="y", show_default=False,
            )
        except (KeyboardInterrupt, EOFError):
            return False
        if choice.strip().lower()[:1] != "y":
            return False

    if not auto_confirm:
        # Step 1: Confirm database for each unique snippet_type
        resolved_dbs: dict[str, str | None] = {}
        for item in items:
            stype = item.get("snippet_type", "fact")
            if stype in resolved_dbs:
                continue
            if db.database_exists(stype):
                # Existing database — use it directly
                resolved_dbs[stype] = stype
            else:
                # New database — confirm with user
                resolved = _confirm_database_choice(db, stype, console, Prompt)
                resolved_dbs[stype] = resolved

        # Apply resolved names and drop skipped items
        kept = []
        for item in items:
            stype = item.get("snippet_type", "fact")
            target = resolved_dbs.get(stype, stype)
            if target is None:
                continue
            item["snippet_type"] = target
            kept.append(item)
        items = kept
        if not items:
            console.print("[yellow]All items skipped.[/yellow]")
            return False

        # Flat snippet: collapse structured items into plain text facts
        if any(it.get("snippet_type") == "fact" for it in items):
            flat_items = []
            for item in items:
                if item.get("snippet_type") == "fact":
                    # Combine entity.key = value into a single text entry
                    entity = item.get("entity", "")
                    key = item.get("key", "")
                    value = item.get("value", "")
                    desc = item.get("description", "")
                    text = f"{key}: {value}" if key else value
                    flat_items.append({
                        "entity": entity,
                        "key": "info",
                        "value": text,
                        "description": desc,
                        "snippet_type": "fact",
                        "tags": item.get("tags", []),
                    })
                else:
                    flat_items.append(item)
            items = flat_items

    # Step 2: Entity dedup — check for similar entities
    resolved_entities: dict[str, str] = {}
    for item in items:
        entity = item["entity"]
        stype = item.get("snippet_type", "fact")
        cache_key = f"{stype}:{entity}"
        if cache_key in resolved_entities:
            resolved = resolved_entities[cache_key]
            if resolved is None:
                item["entity"] = None  # mark for skip
            else:
                item["entity"] = resolved
            continue
        # If entity already exists exactly, skip name dedup — value dedup handles it
        if db.find_existing_snippet(entity, item.get("key", ""), stype):
            resolved_entities[cache_key] = entity
            continue
        similar = db.find_similar_entities(entity, stype)
        if similar:
            from .prompts import pick_from_list
            console.print(f"\n[dim]'{entity}' looks similar to existing entities:[/dim]")
            pick_items = [(s, s) for s in similar[:3]]
            choice = pick_from_list(
                pick_items,
                extras=[("n", "New"), ("s", "Skip")],
                prompt_text="Choice",
                default="1",
                console=console,
            )
            if choice is None or choice == "s":
                resolved_entities[cache_key] = None
            elif choice == "n":
                resolved_entities[cache_key] = entity
            elif choice.isdigit() and 1 <= int(choice) <= len(pick_items):
                resolved = similar[int(choice) - 1]
                item["entity"] = resolved
                resolved_entities[cache_key] = resolved
            else:
                resolved_entities[cache_key] = entity

    # Step 3: Duplicate check + save
    saved = 0
    for item in items:
        entity = item["entity"]
        if entity is None:
            continue  # skipped at entity dedup
        key = item["key"]
        value = item["value"]
        stype = item.get("snippet_type", "fact")

        existing = db.find_existing_snippet(entity, key, stype)
        if existing and not auto_confirm:
            console.print(
                f"\n[dim]Already exists:[/dim] {entity} / {key}"
            )
            console.print(f"  [dim]Current:[/dim] {existing['value']}")
            console.print(f"  [dim]New:[/dim]     {value}")
            try:
                choice = Prompt.ask(
                    r"\[u]pdate / \[s]kip", default="s", show_default=False,
                )
            except (KeyboardInterrupt, EOFError):
                choice = "s"
            ch = choice.strip().lower()[:1]
            if ch == "u":
                db.update_snippet(
                    existing["id"], value,
                    description=item.get("description", "") or existing.get("description", ""),
                )
                saved += 1
            # else skip
            continue

        db.add_reference(
            space=item.get("space") or space,
            group_slug=item.get("group") or group_slug,
            entity=entity,
            key=key,
            value=value,
            description=item.get("description", ""),
            snippet_type=stype,
            tags=item.get("tags", []),
        )
        saved += 1

    if saved:
        console.print(f"[green]Saved {saved} snippet(s).[/green]")
        sync_database_indexes(config, db)
        return True
    else:
        console.print("[yellow]No snippets saved.[/yellow]")
        return False


def _confirm_database_choice(db: Any, suggested: str, console: Any, Prompt: Any) -> str | None:
    """No existing database matches — ask user what to do.

    When existing databases exist, shows them as numbered choices with
    letter extras. Otherwise shows action-only prompt.

    Returns the snippet_type to use, or None to skip.
    """
    from .prompts import pick_from_list

    existing = [n for n in db.get_database_names() if n != "todo"]
    if existing:
        # Show existing databases as numbered picks + actions as extras
        console.print("[dim]Which database?[/dim]")
        items = [(n, n) for n in existing]
        choice = pick_from_list(
            items,
            extras=[("n", "New database"), ("f", "Flat snippet"), ("s", "Skip")],
            console=console,
        )
        if choice is None or choice == "s":
            return None
        if choice == "f":
            return "fact"
        if choice == "n":
            pass  # fall through to creation below
        else:
            try:
                idx = int(choice) - 1
                if 0 <= idx < len(existing):
                    return existing[idx]
            except ValueError:
                pass
            return None
    else:
        # No existing databases — action-only prompt
        console.print("[yellow]No existing database.[/yellow]")
        try:
            choice = Prompt.ask(
                r"\[f]lat snippet / \[c]reate database / \[s]kip",
                default="f", show_default=False,
            )
        except (KeyboardInterrupt, EOFError):
            return None
        ch = choice.strip().lower()
        if ch in ("s", ""):
            return None
        if ch == "f" or not ch:
            return "fact"

    # Create new database — full flow
    try:
        name = Prompt.ask(
            "[dim]Database name[/dim]", default="",
        ).strip().lower().replace(" ", "-")
        if not name:
            return None
        if db.database_exists(name):
            return name
        return confirm_new_database(db, name)
    except (KeyboardInterrupt, EOFError):
        return None


def sync_database_indexes(config: NotelyConfig, db: "Database") -> None:
    """Regenerate one CSV per user-defined database (snippet_type) from DB."""
    from .db import safe_parse_tags

    try:
        db_names = db.get_database_names()
    except Exception:
        return

    headers = ["ID", "Space", "Folder", "Entity", "Key", "Value", "Description", "Tags", "Created"]
    for name in db_names:
        try:
            records = db.get_database_records(name)
        except Exception:
            continue
        if not records:
            continue
        rows = []
        for r in records:
            tags = safe_parse_tags(r.get("tags"))
            rows.append([
                r["id"],
                r.get("space", ""),
                r.get("group_slug", ""),
                r["entity"],
                r["key"],
                r["value"],
                r.get("description", ""),
                ", ".join(tags[:5]),
                r.get("created", ""),
            ])
        write_index_file(config, name, headers, rows)


# Legacy aliases — callers that sync a single database after mutation
def sync_references_index(config: NotelyConfig, db: "Database") -> None:
    """Regenerate _references.csv. Delegates to sync_database_indexes."""
    sync_database_indexes(config, db)


def sync_contacts_index(config: NotelyConfig, db: "Database") -> None:
    """Regenerate _contacts.csv. Delegates to sync_database_indexes."""
    sync_database_indexes(config, db)


