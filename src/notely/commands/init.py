"""notely init — interactive setup in any directory."""

from __future__ import annotations

from pathlib import Path

import click
from rich.console import Console
from rich.prompt import Confirm

from ..config import DEFAULT_CONFIG, NotelyConfig
from ..db import Database
from ..onboarding import run_onboarding

console = Console()


@click.command("init")
@click.argument("directory", required=False, default=None)
@click.option("--default", "use_defaults", is_flag=True, help="Skip questions, use default config")
@click.pass_context
def init_cmd(ctx: click.Context, directory: str | None, use_defaults: bool) -> None:
    """Initialize a Notely workspace.

    Run in any folder to make it the note root (like git init).
    Optionally pass a DIRECTORY path to initialize there instead.

    Walks you through a few questions to set up the right spaces
    for how you work. Use --default to skip and get the standard config.
    """
    if directory:
        target = Path(directory).resolve()
    else:
        config: NotelyConfig = ctx.obj["config"]
        target = config.base_dir

    config_path = target / "config.toml"

    if config_path.exists():
        console.print(f"[yellow]Already initialized at {target}[/yellow]")
        return

    # Generate config — interactive or default
    if use_defaults:
        config_content = DEFAULT_CONFIG
        # Only ask for location confirm when skipping onboarding
        console.print()
        if not Confirm.ask(f"Initialize notely at [bold]{target}[/bold]?", default=True):
            console.print("[yellow]Cancelled.[/yellow]")
            return
    else:
        # Onboarding already has its own review/confirm step
        config_content = run_onboarding(target_dir=target)

    # Create directories
    target.mkdir(parents=True, exist_ok=True)
    notes_dir = target / "notes"
    notes_dir.mkdir(parents=True, exist_ok=True)

    # Write config
    config_path.write_text(config_content, encoding="utf-8")

    # Clean slate: remove stale index and vectors from previous workspace
    # These are derived stores — they rebuild from .md files on next open/reindex
    db_path = target / "index.db"
    if db_path.exists():
        db_path.unlink()
    vectors_dir = target / ".vectors"
    if vectors_dir.exists():
        import shutil
        shutil.rmtree(vectors_dir)

    # Initialize fresh database
    db = Database(db_path)
    db.initialize()
    db.close()

    # Load the generated config and create space directories
    cfg = NotelyConfig(base_dir=target)

    for space_name in cfg.space_names():
        cfg.space_dir(space_name).mkdir(parents=True, exist_ok=True)

    # Create starter group folders if mentioned in config comments
    _create_starter_folders(cfg, config_content, db_path)

    console.print()
    console.print(f"[green]Initialized Notely at {target}[/green]")
    console.print(f"  Config: {config_path}")
    console.print(f"  Database: {db_path}")
    console.print(f"  Notes: {notes_dir}")
    console.print(f"  Spaces: {', '.join(cfg.space_names())}")
    console.print()
    console.print("[dim]You can edit config.toml anytime to add or change spaces.[/dim]")
    console.print("[dim]Start adding notes with: notely open[/dim]")


def _create_starter_folders(cfg: NotelyConfig, config_content: str, db_path: Path) -> None:
    """Parse starter group names from config comments and create folders.

    Lines look like:  #   [clients] Acme Corp
    Also registers each folder in the directories table and vector store.
    """
    import re

    from slugify import slugify

    pattern = re.compile(r"^#\s+\[([\w-]+)]\s+(.+)$")

    db = Database(db_path)
    db.initialize()

    created = []

    # Register each space itself as a directory (so vector search can match at space level)
    for space_name, space_cfg in cfg.spaces.items():
        dir_id = space_name
        db.upsert_directory(
            dir_id=dir_id,
            space=space_name,
            group_slug="",
            display_name=space_cfg.display_name,
            description=space_cfg.description,
        )
        created.append((dir_id, space_name, "", None, space_cfg.display_name, space_cfg.description))

    # Register starter group folders
    for line in config_content.splitlines():
        m = pattern.match(line)
        if m:
            space_name = m.group(1)
            name = m.group(2).strip()
            if space_name in cfg.space_names() and name:
                slug = slugify(name)
                folder = cfg.space_dir(space_name) / slug
                folder.mkdir(parents=True, exist_ok=True)

                dir_id = f"{space_name}/{slug}"
                db.upsert_directory(
                    dir_id=dir_id,
                    space=space_name,
                    group_slug=slug,
                    display_name=name,
                    description=name,
                )
                created.append((dir_id, space_name, slug, None, name, name))

    db.close()

    # Build vectors for all registered directories
    if created:
        try:
            from ..vectors import get_vector_store
            vec = get_vector_store(cfg)
            for dir_id, space, group_slug, subgroup_slug, display_name, description in created:
                vec.upsert_directory(
                    dir_id=dir_id,
                    space=space,
                    group_slug=group_slug,
                    subgroup_slug=subgroup_slug,
                    display_name=display_name,
                    description=description,
                )
        except Exception:
            pass  # Non-fatal — vectors rebuild on first open
