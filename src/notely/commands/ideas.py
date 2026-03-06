"""notely ideas — board-like view of content ideas with status tracking."""

from __future__ import annotations

import json

import click
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from ..config import NotelyConfig
from ..db import Database, safe_json_loads, safe_parse_tags
from ..storage import sync_ideas_index

console = Console()

STATUSES = ["seed", "draft", "used"]
STATUS_STYLES = {
    "seed": "yellow",
    "draft": "cyan",
    "used": "green",
}


@click.group("ideas", invoke_without_command=True)
@click.option("--status", type=click.Choice(STATUSES), default=None, help="Filter by status")
@click.option("--category", type=str, default=None, help="Filter by category")
@click.option("--tag", type=str, default=None, help="Filter by tag")
@click.option("--board", is_flag=True, default=False, help="Show board view grouped by status")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
@click.pass_context
def ideas_cmd(
    ctx: click.Context,
    status: str | None,
    category: str | None,
    tag: str | None,
    board: bool,
    as_json: bool,
) -> None:
    """View and manage content ideas.

    Shows all ideas with their status (seed -> draft -> used),
    category, and tags — like a mini Notion database.

    Use --board for a kanban-style view grouped by status.
    """
    if ctx.invoked_subcommand is not None:
        return

    config: NotelyConfig = ctx.obj["config"]

    config.ensure_initialized()

    # Find the ideas space
    ideas_space = config.find_ideas_space()
    if not ideas_space:
        console.print("[dim]No ideas space configured. Add a space with content_status field.[/dim]")
        return

    db = Database(config.db_path)
    db.initialize()

    try:
        items = _get_ideas(db, ideas_space, status, category, tag)
    finally:
        db.close()

    if not items:
        if as_json:
            click.echo(json.dumps({"status": "ok", "count": 0, "items": []}))
        else:
            console.print("[dim]No ideas yet. Use 'notely dump' to capture one.[/dim]")
        return

    if as_json:
        _output_json(items)
    elif board:
        _output_board(items)
    else:
        _output_table(items)


@ideas_cmd.command("status")
@click.argument("note_id")
@click.argument("new_status", type=click.Choice(STATUSES))
@click.pass_context
def ideas_status_cmd(ctx: click.Context, note_id: str, new_status: str) -> None:
    """Update the status of an idea.

    Move an idea through: seed -> draft -> used

    Example: notely ideas status e5f6g7h8 draft
    """
    config: NotelyConfig = ctx.obj["config"]
    db = Database(config.db_path)
    db.initialize()

    try:
        row = db.get_note(note_id)
        if not row:
            console.print(f"[red]No note with ID {note_id}.[/red]")
            raise SystemExit(1)

        meta = safe_json_loads(row["space_metadata"])
        old_status = meta.get("content_status", "seed")
        meta["content_status"] = new_status

        db.update_note_metadata(note_id, space_metadata=json.dumps(meta))

        # Also update the frontmatter in the markdown file
        _update_file_status(config, row["file_path"], new_status)

        # Sync the index file
        sync_ideas_index(config, db)

        console.print(
            f"[green]Updated:[/green] {row['title']}\n"
            f"  [{STATUS_STYLES.get(old_status, 'dim')}]{old_status}[/{STATUS_STYLES.get(old_status, 'dim')}]"
            f" -> [{STATUS_STYLES[new_status]}]{new_status}[/{STATUS_STYLES[new_status]}]"
        )
    finally:
        db.close()



def _get_ideas(
    db: Database,
    space: str,
    status: str | None,
    category: str | None,
    tag: str | None,
) -> list[dict]:
    """Query ideas with filters."""
    return db.get_ideas(space=space, status=status, category=category, tag=tag)


def _output_table(items: list[dict]) -> None:
    """Display ideas as a table."""
    table = Table(title="Ideas", show_lines=False)
    table.add_column("ID", style="dim", width=10)
    table.add_column("Status", width=8)
    table.add_column("Title", min_width=30)
    table.add_column("Category", width=18)
    table.add_column("Date", width=12)
    table.add_column("Tags", style="dim", width=25)

    for item in items:
        meta = safe_json_loads(item["space_metadata"])
        status = meta.get("content_status", "seed")
        style = STATUS_STYLES.get(status, "dim")
        category = meta.get("category_display") or meta.get("category", "")
        tags = safe_parse_tags(item["tags"])

        table.add_row(
            item["id"],
            f"[{style}]{status}[/{style}]",
            item["title"],
            category,
            item["date"],
            ", ".join(tags[:3]),
        )

    console.print(table)

    # Show counts by status
    counts: dict[str, int] = {}
    for item in items:
        meta = safe_json_loads(item["space_metadata"])
        s = meta.get("content_status", "seed")
        counts[s] = counts.get(s, 0) + 1

    parts = []
    for s in STATUSES:
        if s in counts:
            parts.append(f"[{STATUS_STYLES[s]}]{counts[s]} {s}[/{STATUS_STYLES[s]}]")
    console.print(f"\n{' / '.join(parts)}")


def _output_board(items: list[dict]) -> None:
    """Display ideas in a kanban-style board grouped by status."""
    grouped: dict[str, list[dict]] = {s: [] for s in STATUSES}

    for item in items:
        meta = safe_json_loads(item["space_metadata"])
        status = meta.get("content_status", "seed")
        if status not in grouped:
            grouped[status] = []
        grouped[status].append(item)

    for status in STATUSES:
        items_in_status = grouped.get(status, [])
        if not items_in_status:
            continue

        style = STATUS_STYLES[status]
        lines = []
        for item in items_in_status:
            meta = safe_json_loads(item["space_metadata"])
            category = meta.get("category_display") or meta.get("category", "")
            cat_str = f" [dim]({category})[/dim]" if category else ""
            lines.append(f"  [dim]{item['id']}[/dim]  {item['title']}{cat_str}")
            lines.append(f"         [dim]{item['summary'][:60]}[/dim]")
            lines.append("")

        console.print(Panel(
            "\n".join(lines).rstrip(),
            title=f"[{style}]{status.upper()}[/{style}] ({len(items_in_status)})",
            border_style=style,
            width=75,
        ))
        console.print()


def _output_json(items: list[dict]) -> None:
    """Output ideas as JSON."""
    results = []
    for item in items:
        meta = safe_json_loads(item["space_metadata"])
        tags = safe_parse_tags(item["tags"])
        results.append({
            "id": item["id"],
            "title": item["title"],
            "date": item["date"],
            "summary": item["summary"],
            "source": item["source"],
            "tags": tags,
            "content_status": meta.get("content_status", "seed"),
            "category": meta.get("category"),
            "category_display": meta.get("category_display"),
        })

    click.echo(json.dumps({
        "status": "ok",
        "count": len(results),
        "items": results,
    }, indent=2))


def _update_file_status(config: NotelyConfig, file_path: str, new_status: str) -> None:
    """Update content_status in the markdown file's frontmatter."""
    import frontmatter

    full_path = config.notes_dir / file_path
    if not full_path.exists():
        return

    post = frontmatter.load(str(full_path))
    post["content_status"] = new_status
    frontmatter.dump(post, str(full_path))
