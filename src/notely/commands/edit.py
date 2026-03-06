"""notely edit — open a note in $EDITOR, re-index on save."""

from __future__ import annotations

import os
import subprocess

import click
from rich.console import Console

from ..config import NotelyConfig
from ..db import Database
from ..storage import absolute_path, read_note

console = Console()


@click.command("edit")
@click.argument("note_id")
@click.pass_context
def edit_cmd(ctx: click.Context, note_id: str) -> None:
    """Open a note in $EDITOR. Re-indexes the note on save."""
    config: NotelyConfig = ctx.obj["config"]

    config.ensure_initialized()

    with Database(config.db_path) as db:
        db.initialize()

        row = db.get_note(note_id)
        if not row:
            console.print(f"[red]Note not found: {note_id}[/red]")
            raise SystemExit(1)

        file_path = row["file_path"]
        abs_path = absolute_path(config, file_path)

        if not abs_path.exists():
            console.print(f"[red]File not found: {abs_path}[/red]")
            raise SystemExit(1)

        editor = os.environ.get("EDITOR", "vi")
        mtime_before = abs_path.stat().st_mtime

        subprocess.run([editor, str(abs_path)])

        mtime_after = abs_path.stat().st_mtime

        if mtime_after != mtime_before:
            # File was modified — re-index
            note = read_note(config, file_path)
            if note:
                # Mark as human-reviewed since user edited it
                from ..models import Refinement
                note.refinement = Refinement.HUMAN_REVIEWED
                db.upsert_note(note)
                try:
                    from ..vectors import try_vector_sync_note
                    try_vector_sync_note(config, note)
                except Exception:
                    pass
                console.print(f"[green]Re-indexed: {note.title}[/green]")
            else:
                console.print("[yellow]Could not re-read note after edit.[/yellow]")
        else:
            console.print("[dim]No changes detected.[/dim]")
