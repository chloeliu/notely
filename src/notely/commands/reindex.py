"""notely reindex — rebuild SQLite index and vector store from markdown files."""

from __future__ import annotations

import click
from rich.console import Console

from ..config import NotelyConfig
from ..db import Database
from ..models import ActionItem
from ..storage import read_all_notes

console = Console()


def _extract_legacy_action_items(config: NotelyConfig, file_path: str) -> list[ActionItem]:
    """Extract action items from legacy frontmatter (if present).

    Old .md files may have action_items in YAML frontmatter. Since the Note
    model no longer carries them, we read them directly from the file.
    """
    import frontmatter
    full_path = config.notes_dir / file_path
    if not full_path.exists():
        return []
    try:
        post = frontmatter.load(str(full_path))
    except Exception:
        return []
    raw_items = post.metadata.get("action_items", [])
    if not raw_items:
        return []
    items = []
    for ai in raw_items:
        if isinstance(ai, dict):
            items.append(ActionItem(
                owner=ai.get("owner", ""),
                task=ai.get("task", ""),
                due=ai.get("due"),
                status=ai.get("status", "open"),
            ))
    return items


@click.command("reindex")
@click.pass_context
def reindex_cmd(ctx: click.Context) -> None:
    """Rebuild the SQLite index and vector store from all markdown files on disk."""
    config: NotelyConfig = ctx.obj["config"]

    config.ensure_initialized()

    console.print("[dim]Reading all notes from disk...[/dim]")
    notes = read_all_notes(config)

    with Database(config.db_path) as db:
        db.initialize()
        db.clear_all()

        count = 0
        action_count = 0
        for note in notes:
            try:
                db.upsert_note(note)
                count += 1

                # Extract legacy action items from frontmatter and insert into DB
                legacy_items = _extract_legacy_action_items(config, note.file_path)
                if legacy_items:
                    parts = note.file_path.split("/")
                    group_name = parts[1] if len(parts) > 1 else ""
                    db.add_todos_for_note(
                        note.id, legacy_items, note.space, group_name,
                    )
                    action_count += len(legacy_items)
            except Exception as e:
                console.print(f"[red]Error indexing {note.file_path}: {e}[/red]")

        if action_count:
            console.print(f"[dim]Extracted {action_count} action item(s) from frontmatter.[/dim]")

        # Rebuild vector store
        console.print("[dim]Rebuilding search index...[/dim]")
        try:
            from ..vectors import get_vector_store
            vec = get_vector_store(config)
            dir_count, note_count = vec.rebuild_from_db(config, db)
            console.print(f"[dim]Indexed {dir_count} directories, {note_count} notes.[/dim]")
        except Exception as e:
            console.print(f"[yellow]Vector rebuild skipped: {e}[/yellow]")

    console.print(f"[green]Reindexed {count} note(s).[/green]")
