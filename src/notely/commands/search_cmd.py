"""notely search — search notes with filters."""

from __future__ import annotations

import json

import click
from rich.console import Console
from rich.table import Table

from ..config import NotelyConfig
from ..db import Database, safe_json_loads, safe_parse_tags
from ..models import SearchFilters

console = Console()


@click.command("search")
@click.argument("query", required=False, default=None)
@click.option("--space", "-s", type=str, default=None, help="Filter by space")
@click.option("--client", type=str, default=None, help="Filter by client slug")
@click.option("--category", type=str, default=None, help="Filter by category slug")
@click.option("--tag", "-t", type=str, multiple=True, help="Filter by tag (repeatable)")
@click.option("--source", type=str, default=None, help="Filter by source type")
@click.option("--from", "date_from", type=str, default=None, help="Start date (YYYY-MM-DD)")
@click.option("--to", "date_to", type=str, default=None, help="End date (YYYY-MM-DD)")
@click.option("--limit", "-n", type=int, default=20, help="Max results")
@click.option("--json-output", "--json", "json_out", is_flag=True, help="Output as JSON")
@click.pass_context
def search_cmd(
    ctx: click.Context,
    query: str | None,
    space: str | None,
    client: str | None,
    category: str | None,
    tag: tuple[str, ...],
    source: str | None,
    date_from: str | None,
    date_to: str | None,
    limit: int,
    json_out: bool,
) -> None:
    """Search notes. Optionally provide a text query for full-text search."""
    config: NotelyConfig = ctx.obj["config"]

    config.ensure_initialized()

    with Database(config.db_path) as db:
        db.initialize()

        filters = SearchFilters(
            space=space,
            client=client,
            category=category,
            tags=list(tag),
            source=source,
            date_from=date_from,
            date_to=date_to,
        )

        sort_by = "relevance" if query else "recency"
        rows = db.search(text_query=query, filters=filters, limit=limit, sort_by=sort_by)

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

                # Include open action item count
                items = db.get_note_action_items(r["id"])
                entry["action_items_open"] = sum(1 for i in items if i["status"] == "open")

                results.append(entry)

            click.echo(json.dumps({"status": "ok", "count": len(results), "results": results}, indent=2))
            return

    if not rows:
        console.print("[dim]No results found.[/dim]")
        return

    table = Table(title=f"Search Results ({len(rows)})", show_lines=False)
    table.add_column("ID", style="dim", width=10)
    table.add_column("Date", width=12)
    table.add_column("Space", width=10)
    table.add_column("Title", min_width=30)
    table.add_column("Summary", max_width=50)

    for r in rows:
        summary = r["summary"][:50] + "..." if len(r["summary"]) > 50 else r["summary"]
        table.add_row(
            r["id"],
            r["date"],
            r["space"],
            r["title"],
            summary,
        )

    console.print(table)
