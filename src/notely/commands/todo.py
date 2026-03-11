"""notely todo — universal action items list across all notes."""

from __future__ import annotations

import json

import click
from rich.console import Console
from rich.table import Table

from ..config import NotelyConfig
from ..db import Database, safe_json_loads
from ..storage import sync_todo_index, update_action_status

console = Console()


@click.group("todo", invoke_without_command=True)
@click.option("--space", default=None, help="Filter by space")
@click.option("--client", default=None, help="Filter by client")
@click.option("--owner", default=None, help="Filter by owner")
@click.option("--all", "show_all", is_flag=True, help="Include completed items")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
@click.pass_context
def todo_cmd(
    ctx: click.Context,
    space: str | None,
    client: str | None,
    owner: str | None,
    show_all: bool,
    as_json: bool,
) -> None:
    """View action items across all your notes.

    Shows a universal list of to-dos pulled from every note,
    tagged with where they came from (space, client, note title).

    Use --space or --client to narrow it down.
    """
    if ctx.invoked_subcommand is not None:
        return

    config: NotelyConfig = ctx.obj["config"]

    with Database(config.db_path) as db:
        db.initialize()
        items = _get_items(db, space, client, owner, show_all)

        # Dedup check (only for open items in table mode)
        if not as_json and not show_all and items:
            from ..dedup import find_duplicate_clusters
            from ..storage import handle_todo_dedup

            open_items = [i for i in items if i["status"] == "open"]
            clusters = find_duplicate_clusters(open_items)
            if clusters:
                merged_any = handle_todo_dedup(config, db, clusters)
                if merged_any:
                    items = _get_items(db, space, client, owner, show_all)

    if not items:
        if as_json:
            click.echo(json.dumps({"status": "ok", "count": 0, "items": []}))
        else:
            console.print("[dim]No open action items.[/dim]")
        return

    if as_json:
        _output_json(items)
    else:
        _output_table(items, show_all)


@todo_cmd.command("done")
@click.argument("item_id", type=int)
@click.pass_context
def todo_done_cmd(ctx: click.Context, item_id: int) -> None:
    """Mark an action item as done.

    Use the ID shown in `notely todo`.
    """
    config: NotelyConfig = ctx.obj["config"]
    with Database(config.db_path) as db:
        db.initialize()

        row = db.get_todo(item_id)
        if not row:
            console.print(f"[red]No action item with ID {item_id}.[/red]")
            raise SystemExit(1)

        if row["status"] == "done":
            console.print(f"[yellow]Already done:[/yellow] {row['task']}")
            return

        update_action_status(config, db, item_id, "done")
        sync_todo_index(config, db)
        console.print(f"[green]Done:[/green] {row['task']}")


@todo_cmd.command("reopen")
@click.argument("item_id", type=int)
@click.pass_context
def todo_reopen_cmd(ctx: click.Context, item_id: int) -> None:
    """Re-open a completed action item."""
    config: NotelyConfig = ctx.obj["config"]
    with Database(config.db_path) as db:
        db.initialize()

        row = db.get_todo(item_id)
        if not row:
            console.print(f"[red]No action item with ID {item_id}.[/red]")
            raise SystemExit(1)

        if row["status"] == "open":
            console.print(f"[yellow]Already open:[/yellow] {row['task']}")
            return

        update_action_status(config, db, item_id, "open")
        sync_todo_index(config, db)
        console.print(f"[green]Reopened:[/green] {row['task']}")


def _get_items(
    db: Database,
    space: str | None,
    client: str | None,
    owner: str | None,
    show_all: bool,
) -> list[dict]:
    """Query todos with filters."""
    return db.get_todos_filtered(
        space=space, client=client, owner=owner, show_all=show_all,
    )


def _output_table(items: list[dict], show_all: bool) -> None:
    """Display action items as two-line cards."""
    console.print("\n[bold]Action Items[/bold]\n")

    open_count = 0
    done_count = 0
    for item in items:
        is_done = item["status"] == "done"
        if is_done:
            done_count += 1
        else:
            open_count += 1

        # Derive folder path from space/group_slug
        space = item.get("space") or ""
        group_slug = item.get("group_slug") or ""
        folder_path = f"{space}/{group_slug}" if space and group_slug else space

        title = item.get("note_title", "")
        from_str = folder_path if folder_path else title

        # Line 1: ID + task
        task_text = item["task"]
        if is_done:
            task_text = f"[strike dim]{task_text}[/strike dim]"
        console.print(f"  [dim]#{item['id']}[/dim]  {task_text}")

        # Line 2: metadata
        meta_parts = []
        meta_parts.append(f"[cyan]{item['owner']}[/cyan]")
        if item.get("due"):
            meta_parts.append(f"due [yellow]{item['due']}[/yellow]")
        if is_done:
            meta_parts.append("[green]done[/green]")
        if from_str:
            meta_parts.append(f"[dim]{from_str}[/dim]")
        console.print(f"       {' · '.join(meta_parts)}")
        console.print()

    if show_all:
        console.print(f"[dim]{open_count} open, {done_count} done[/dim]")
    else:
        console.print(f"[dim]{open_count} open items[/dim]")


def _output_json(items: list[dict]) -> None:
    """Output action items as JSON."""
    results = []
    for item in items:
        results.append({
            "id": item["id"],
            "task": item["task"],
            "owner": item["owner"],
            "due": item["due"],
            "status": item["status"],
            "space": item.get("space", ""),
            "note_id": item.get("note_id"),
            "note_title": item.get("note_title", ""),
            "note_date": item.get("note_date", ""),
            "group_slug": item.get("group_slug", ""),
        })

    click.echo(json.dumps({
        "status": "ok",
        "count": len(results),
        "items": results,
    }, indent=2))
