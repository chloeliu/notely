"""notely reindex — rebuild SQLite index and vector store from markdown files."""

from __future__ import annotations

import click
from rich.console import Console

from ..config import NotelyConfig
from ..db import Database
from ..storage import read_all_notes

console = Console()


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
        for note in notes:
            try:
                db.upsert_note(note)
                count += 1
            except Exception as e:
                console.print(f"[red]Error indexing {note.file_path}: {e}[/red]")

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
