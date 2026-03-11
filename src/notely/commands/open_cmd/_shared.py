"""Shared utilities for the open_cmd package — console, logger, folder helpers."""

from __future__ import annotations

import logging

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
        return []

    result: list[tuple[str, str, str]] = []
    seen_spaces: set[str] = set()

    # First pass: space-level entries for all spaces
    # Use directory display_name for spaces without groups, config display_name for others
    spaces_with_groups: set[str] = set()
    space_display_from_dir: dict[str, str] = {}
    for d in dirs:
        if d["group_slug"] and not d.get("subgroup_slug"):
            spaces_with_groups.add(d["space"])
        if not d["group_slug"]:
            space_display_from_dir[d["space"]] = d["display_name"]

    for space_name in sorted({d["space"] for d in dirs}):
        if space_name in space_display_from_dir:
            display = space_display_from_dir[space_name]
        else:
            space_cfg = config.get_space(space_name)
            display = space_cfg.display_name if space_cfg else space_name.title()
        result.append((space_name, display, space_name))

    # Second pass: group-level dirs
    for d in dirs:
        if d["group_slug"] and not d.get("subgroup_slug"):
            result.append((d["group_slug"], d["display_name"], d["space"]))

    # Third pass: subgroup-level dirs
    for d in dirs:
        if d["group_slug"] and d.get("subgroup_slug"):
            slug = f"{d['group_slug']}/{d['subgroup_slug']}"
            result.append((slug, d["display_name"], d["space"]))
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
    with Database(config.db_path) as db:
        db.initialize()
        dirs = db.get_all_directories()

    # Build candidate list at all levels: spaces, groups, subgroups
    # Each entry is (dict, folder_path) where folder_path is the slug for callers
    all_folders: list[tuple[dict, str]] = []
    seen_spaces: set[str] = set()
    space_display_from_dir: dict[str, str] = {}

    # Collect info for space-level entries
    for d in dirs:
        if not d["group_slug"]:
            space_display_from_dir[d["space"]] = d["display_name"]

    # First pass: space-level entries for all spaces
    for space_name in sorted({d["space"] for d in dirs}):
        if space_name in space_display_from_dir:
            display = space_display_from_dir[space_name]
        else:
            space_cfg = config.get_space(space_name)
            display = space_cfg.display_name if space_cfg else space_name.title()
        # Synthesize a dict-like entry for the space level
        space_entry = {"space": space_name, "group_slug": "", "display_name": display}
        all_folders.append((space_entry, ""))
        seen_spaces.add(space_name)

    # Second pass: group-level dirs
    for d in dirs:
        if d["group_slug"] and not d.get("subgroup_slug"):
            all_folders.append((d, d["group_slug"]))
    # Third pass: subgroup-level dirs
    for d in dirs:
        if d["group_slug"] and d.get("subgroup_slug"):
            folder_path = f"{d['group_slug']}/{d['subgroup_slug']}"
            all_folders.append((d, folder_path))

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
    from ...vectors import get_vector_store
    from ...routing import refresh_directory_descriptions

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
    from ...storage import sync_todo_index, sync_ideas_index
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
