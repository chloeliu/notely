"""notely show — display a full note."""

from __future__ import annotations

import json

import click
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel

from ..config import NotelyConfig
from ..db import Database, safe_json_loads, safe_parse_tags
from ..storage import read_note

console = Console()


@click.command("show")
@click.argument("note_id")
@click.option("--json-output", "--json", "json_out", is_flag=True, help="Output as JSON")
@click.option("--raw", is_flag=True, help="Include raw source text")
@click.pass_context
def show_cmd(ctx: click.Context, note_id: str, json_out: bool, raw: bool) -> None:
    """Display a full note by ID."""
    config: NotelyConfig = ctx.obj["config"]

    config.ensure_initialized()

    with Database(config.db_path) as db:
        db.initialize()

        row = db.get_note(note_id)
        if not row:
            console.print(f"[red]Note not found: {note_id}[/red]")
            raise SystemExit(1)

        # Read full note from disk
        note = read_note(config, row["file_path"])

        action_items = db.get_note_todos(note_id)
        cross_refs = db.get_note_cross_refs(note_id)

    if json_out:
        data = {
            "id": row["id"],
            "title": row["title"],
            "space": row["space"],
            "date": row["date"],
            "source": row["source"],
            "refinement": row["refinement"],
            "summary": row["summary"],
            "tags": safe_parse_tags(row["tags"]),
            "participants": safe_parse_tags(row["participants"]),
            "file_path": row["file_path"],
            "space_metadata": safe_json_loads(row.get("space_metadata")),
            "body": note.body if note else "",
            "action_items": action_items,
            "related_contexts": cross_refs,
        }
        if raw and note:
            data["raw_text"] = note.raw_text
        click.echo(json.dumps(data, indent=2))
        return

    # Rich display
    meta_lines = [
        f"[bold]ID:[/bold]         {row['id']}",
        f"[bold]Space:[/bold]      {row['space']}",
        f"[bold]Date:[/bold]       {row['date']}",
        f"[bold]Source:[/bold]     {row['source']}",
        f"[bold]Refinement:[/bold] {row['refinement']}",
        f"[bold]File:[/bold]       {row['file_path']}",
    ]

    sm = safe_json_loads(row.get("space_metadata"))
    for key, value in sm.items():
        if not key.endswith("_display"):
            meta_lines.append(f"[bold]{key.title()}:[/bold]    {value}")

    tags = safe_parse_tags(row["tags"])
    if tags:
        meta_lines.append(f"[bold]Tags:[/bold]       {', '.join(tags)}")

    participants = safe_parse_tags(row["participants"])
    if participants:
        meta_lines.append(f"[bold]People:[/bold]     {', '.join(participants)}")

    meta_lines.append(f"\n[bold]Summary:[/bold]\n{row['summary']}")

    console.print(Panel("\n".join(meta_lines), title=row["title"], border_style="blue"))

    if note and note.body:
        console.print()
        console.print(Markdown(note.body))

    if action_items:
        console.print("\n[bold]Action Items:[/bold]")
        for item in action_items:
            status = "x" if item["status"] == "done" else " "
            due = f" (due {item['due']})" if item.get("due") else ""
            console.print(f"  [{status}] [bold]{item['owner']}[/bold]: {item['task']}{due}")

    if cross_refs:
        console.print("\n[bold]Related Contexts:[/bold]")
        for ref in cross_refs:
            console.print(f"  - {ref}")
