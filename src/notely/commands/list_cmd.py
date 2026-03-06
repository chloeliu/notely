"""notely list — list notes, recent first."""

from __future__ import annotations

import json

import click
from rich.console import Console
from rich.table import Table

from ..config import NotelyConfig
from ..db import Database, safe_json_loads, safe_parse_tags
from ..models import SearchFilters

console = Console()


@click.command("list")
@click.option("--space", "-s", type=str, default=None, help="Filter by space")
@click.option("--limit", "-n", type=int, default=20, help="Max results")
@click.option("--json-output", "--json", "json_out", is_flag=True, help="Output as JSON")
@click.pass_context
def list_cmd(ctx: click.Context, space: str | None, limit: int, json_out: bool) -> None:
    """List notes, most recent first."""
    config: NotelyConfig = ctx.obj["config"]

    config.ensure_initialized()

    with Database(config.db_path) as db:
        db.initialize()
        filters = SearchFilters(space=space) if space else None
        rows = db.search(filters=filters, limit=limit)

    if json_out:
        results = []
        for r in rows:
            entry = {
                "id": r["id"],
                "title": r["title"],
                "space": r["space"],
                "date": r["date"],
                "source": r["source"],
                "refinement": r["refinement"],
                "summary": r["summary"],
                "tags": safe_parse_tags(r["tags"]),
                "file_path": r["file_path"],
            }
            sm = safe_json_loads(r.get("space_metadata"))
            entry.update(sm)
            results.append(entry)
        click.echo(json.dumps({"count": len(results), "results": results}, indent=2))
        return

    if not rows:
        console.print("[dim]No notes found.[/dim]")
        return

    table = Table(title="Notes", show_lines=False)
    table.add_column("ID", style="dim", width=10)
    table.add_column("Date", width=12)
    table.add_column("Space", width=10)
    table.add_column("Title", min_width=30)
    table.add_column("Source", width=10)

    for r in rows:
        table.add_row(
            r["id"],
            r["date"],
            r["space"],
            r["title"],
            r["source"],
        )

    console.print(table)
