"""notely spaces — list configured spaces and stats."""

from __future__ import annotations

import click
from rich.console import Console
from rich.table import Table

from ..config import NotelyConfig
from ..db import Database

console = Console()


@click.command("spaces")
@click.pass_context
def spaces_cmd(ctx: click.Context) -> None:
    """List configured spaces and their stats."""
    config: NotelyConfig = ctx.obj["config"]

    config.ensure_initialized()

    with Database(config.db_path) as db:
        db.initialize()
        stats = db.get_space_stats()

    table = Table(title="Spaces")
    table.add_column("Name", style="bold")
    table.add_column("Display Name")
    table.add_column("Group By")
    table.add_column("Notes", justify="right")
    table.add_column("Description", max_width=50)

    for name, sc in config.spaces.items():
        count = stats.get(name, {}).get("count", 0)
        table.add_row(
            name,
            sc.display_name,
            sc.group_by,
            str(count),
            sc.description[:50],
        )

    console.print(table)
