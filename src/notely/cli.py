"""CLI entry point — click group and command registration."""

from __future__ import annotations

import click

from .config import NotelyConfig


@click.group()
@click.option(
    "--base-dir",
    envvar="NOTELY_DIR",
    default=None,
    help="Base directory (default: auto-detect from cwd, like git)",
)
@click.option(
    "--debug",
    is_flag=True,
    default=False,
    envvar="NOTELY_DEBUG",
    help="Enable debug logging",
)
@click.pass_context
def cli(ctx: click.Context, base_dir: str | None, debug: bool) -> None:
    """Notely: Structured note system for AI-powered retrieval."""
    import logging
    from pathlib import Path

    level = logging.DEBUG if debug else logging.WARNING
    logging.basicConfig(
        level=level,
        format="%(name)s %(levelname)s: %(message)s",
    )

    base = Path(base_dir) if base_dir else None
    ctx.ensure_object(dict)
    ctx.obj["config"] = NotelyConfig(base_dir=base)


# Register commands
from .commands.dump import dump_cmd
from .commands.edit import edit_cmd
from .commands.init import init_cmd
from .commands.open_cmd import open_cmd
from .commands.query_cmd import query_cmd
from .commands.reindex import reindex_cmd
from .commands.search_cmd import search_cmd
from .commands.todo import todo_cmd

cli.add_command(init_cmd, "init")
cli.add_command(open_cmd, "open")
cli.add_command(dump_cmd, "dump")
cli.add_command(search_cmd, "search")
cli.add_command(todo_cmd, "todo")
cli.add_command(edit_cmd, "edit")
cli.add_command(query_cmd, "query")
cli.add_command(reindex_cmd, "reindex")
