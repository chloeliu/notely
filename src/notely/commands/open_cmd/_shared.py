"""Shared utilities for the open_cmd package — console, logger, folder helpers."""

from __future__ import annotations

import logging
from pathlib import Path

from rich.console import Console
from rich.prompt import Prompt

from ...config import NotelyConfig
from ...db import Database

logger = logging.getLogger(__name__)

console = Console()


def _get_all_folders(config: NotelyConfig) -> list[tuple[str, str, str]]:
    """Get (slug, display_name, space) for all navigable levels.

    Three levels of entries:
    - Space-level: slug = space name (e.g. "clients"). Always included so users
      can query across an entire space. For spaces without groups, this is the
      only entry. For spaces with groups, it's a rollup entry.
    - Group-level: slug = group_slug (e.g. "sanity" for clients/sanity).
    - Subgroup-level: slug = "group/subgroup" (e.g. "sanity/onboarding").

    Shared by _SlashCompleter, _prompt_connect_folder, etc.
    """
    try:
        with Database(config.db_path) as db:
            db.initialize()
            dirs = db.get_all_directories()
    except Exception:
        dirs = []

    result: list[tuple[str, str, str]] = []
    seen: set[str] = set()  # track "space/slug" to avoid duplicates

    # --- From DB directories ---
    space_display_from_dir: dict[str, str] = {}
    for d in dirs:
        if not d["group_slug"]:
            space_display_from_dir[d["space"]] = d["display_name"]

    # Space-level entries from both config and DB
    all_spaces = set(config.space_names()) | {d["space"] for d in dirs}
    for space_name in sorted(all_spaces):
        if space_name in space_display_from_dir:
            display = space_display_from_dir[space_name]
        else:
            space_cfg = config.get_space(space_name)
            display = space_cfg.display_name if space_cfg else space_name.title()
        result.append((space_name, display, space_name))
        seen.add(space_name)

    # Group-level dirs from DB
    for d in dirs:
        if d["group_slug"] and not d.get("subgroup_slug"):
            key = f"{d['space']}/{d['group_slug']}"
            if key not in seen:
                result.append((d["group_slug"], d["display_name"], d["space"]))
                seen.add(key)

    # Subgroup-level dirs from DB
    for d in dirs:
        if d["group_slug"] and d.get("subgroup_slug"):
            slug = f"{d['group_slug']}/{d['subgroup_slug']}"
            key = f"{d['space']}/{slug}"
            if key not in seen:
                result.append((slug, d["display_name"], d["space"]))
                seen.add(key)

    # --- Fallback: scan filesystem for folders not in DB ---
    # This ensures autocomplete works even before reindex populates directories
    notes_dir = config.notes_dir
    if notes_dir.is_dir():
        for space_dir in sorted(notes_dir.iterdir()):
            if not space_dir.is_dir() or space_dir.name.startswith("."):
                continue
            space_name = space_dir.name
            # Add space-level if not already present
            if space_name not in seen:
                space_cfg = config.get_space(space_name)
                display = space_cfg.display_name if space_cfg else space_name.title()
                result.append((space_name, display, space_name))
                seen.add(space_name)
            # Add group-level folders
            for group_dir in sorted(space_dir.iterdir()):
                if not group_dir.is_dir() or group_dir.name.startswith("."):
                    continue
                key = f"{space_name}/{group_dir.name}"
                if key not in seen:
                    display = group_dir.name.replace("-", " ").title()
                    result.append((group_dir.name, display, space_name))
                    seen.add(key)

    return result


def _fuzzy_match_folder(
    config: NotelyConfig, query: str
) -> tuple[str, str, str, str | None] | None:
    """Fuzzy match a user query to a folder at any level of the hierarchy.

    Returns (space, folder_path, display_name, subgroup_field) or None.
    folder_path is the path below the space — "" for space-level,
    "sanity" for groups, "sanity/onboarding" for subgroups.
    Used in file_path LIKE queries (empty = all notes in space).
    If ambiguous, shows a numbered picker. If no arg, shows all folders.
    """
    # Build candidate list from _get_all_folders (includes DB + filesystem fallback)
    folders = _get_all_folders(config)
    all_folders: list[tuple[dict, str]] = []
    for slug, display, space in folders:
        is_space = slug == space
        folder_path = "" if is_space else slug
        entry = {"space": space, "group_slug": folder_path, "display_name": display}
        all_folders.append((entry, folder_path))

    if not all_folders:
        return None

    if not query.strip():
        # No arg — show picker of all folders
        candidates = all_folders
    else:
        query_lower = query.strip().lower()
        if "/" in query_lower:
            # Full path matching (e.g. "clients/sanity")
            candidates = [
                (d, fp) for d, fp in all_folders
                if query_lower in (f"{d['space']}/{fp}" if fp else d["space"]).lower()
            ]
        else:
            # Fuzzy match on slug/display (e.g. "sanity", "vault")
            candidates = [
                (d, fp) for d, fp in all_folders
                if query_lower in d["display_name"].lower()
                or query_lower in (fp or d["space"]).lower()
            ]

    if not candidates:
        return None

    if len(candidates) == 1:
        d, folder_path = candidates[0]
        space_cfg = config.get_space(d["space"])
        subgroup_field = space_cfg.subgroup_by if space_cfg else None
        return (d["space"], folder_path, d["display_name"], subgroup_field)

    # Multiple matches — show picker
    console.print()
    for i, (d, fp) in enumerate(candidates, 1):
        console.print(f"  [{i}] {d['display_name']} [dim]({d['space']})[/dim]")
    console.print(f"  [{len(candidates) + 1}] Cancel")

    choice = Prompt.ask("Choice", default="1")
    try:
        idx = int(choice) - 1
        if 0 <= idx < len(candidates):
            d, folder_path = candidates[idx]
            space_cfg = config.get_space(d["space"])
            subgroup_field = space_cfg.subgroup_by if space_cfg else None
            return (d["space"], folder_path, d["display_name"], subgroup_field)
    except ValueError:
        pass
    return None


def _working_folder_query(working_folder: dict) -> str:
    """Build a query string from the working folder dict for commands like /chat."""
    if not working_folder:
        return ""
    space = working_folder["space"]
    group_slug = working_folder.get("group_slug", "")
    if group_slug:
        return f"{space}/{group_slug}"
    return space


def _ensure_vectors(config: NotelyConfig, db: Database) -> None:
    """Auto-build vectors if missing, then refresh directory descriptions."""
    from ...routing import refresh_directory_descriptions
    from ...vectors import get_vector_store

    try:
        vec = get_vector_store(config)

        if not vec.is_available():
            note_count = db.count_notes()
            if note_count == 0:
                return  # No notes to index
            console.print("[dim]Building search index... (one-time)[/dim]")
            dir_count, indexed = vec.rebuild_from_db(config, db)
            console.print(f"[dim]Indexed {dir_count} directories, {indexed} notes.[/dim]")
            return  # rebuild_from_db already builds fresh descriptions

        # Vectors exist — refresh directory descriptions from recent notes
        updated = refresh_directory_descriptions(config, db)
        if updated:
            logger.debug(f"Refreshed {updated} directory descriptions")
    except Exception as e:
        # Non-fatal — vectors are optional
        console.print(f"[dim]Search index not available: {e}[/dim]")


def _confirm_new_database(config: NotelyConfig, db_name: str) -> bool:
    """Check if db_name is a new database; if so, ask user to confirm creation.

    Returns True if the database already exists OR user confirmed creation.
    Returns False if user declined.
    """
    from ...storage import confirm_new_database

    with Database(config.db_path) as db:
        db.initialize()
        if db.database_exists(db_name):
            return True
        return confirm_new_database(db, db_name) is not None


def _resync(config: NotelyConfig) -> None:
    """Re-sync DB from files on disk. Picks up manual edits."""
    from ...storage import sync_ideas_index, sync_todo_index
    from ...vectors import get_vector_store

    with Database(config.db_path) as db:
        db.initialize()
        updated, pruned = db.resync_from_files(config)
        sync_todo_index(config, db)
        sync_ideas_index(config, db)

        # Rebuild vectors
        console.print("[dim]Rebuilding search index...[/dim]")
        try:
            vec = get_vector_store(config)
            dir_count, note_count = vec.rebuild_from_db(config, db)
            console.print(f"[dim]Indexed {dir_count} directories, {note_count} notes.[/dim]")
        except Exception as e:
            console.print(f"[dim]Vector rebuild skipped: {e}[/dim]")

    console.print(f"[green]Synced:[/green] {updated} note(s) re-indexed, {pruned} removed.")
