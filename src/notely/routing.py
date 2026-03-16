"""Routing pipeline — semantic search + user confirmation for note placement."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from rich.console import Console
from rich.prompt import Prompt

from .config import NotelyConfig
from .db import Database
from .prompts import duplicate_found, pick_from_list

logger = logging.getLogger(__name__)
console = Console()

# Vector distance thresholds for routing decisions
DIST_DUPLICATE = 0.2    # Super-close: "You may already have this"
DIST_GOOD_MATCH = 0.4   # Close: "Update one of these?" / "Which folder?"
DIST_WEAK_MATCH = 0.6   # Weak: still show in explore_routing results
CONTEXT_SNIPPET_LENGTH = 500  # Chars of raw text used for search context


@dataclass
class RoutingDecision:
    """Where a note should be filed."""

    space: str
    group_slug: str
    group_display: str
    group_is_new: bool = False
    subgroup_slug: str | None = None
    subgroup_display: str | None = None
    subgroup_is_new: bool = False
    append_to_note: str | None = None
    description: str = ""
    records_only: bool = False
    existing_note_id: str | None = None


def extract_context(raw_text: str, user_context: str | None = None) -> str:
    """Build a context string for vector search.

    Combines user-typed context words with the first chunk of raw text.
    """
    parts = []
    if user_context and user_context.strip():
        parts.append(user_context.strip())
    # Take first 500 chars of raw text for context
    snippet = raw_text.strip()[:CONTEXT_SNIPPET_LENGTH]
    if snippet:
        parts.append(snippet)
    return " ".join(parts) if parts else ""


def _handle_dup_choice(
    choice: str,
    dup: dict[str, Any],
    config: NotelyConfig,
    db: Database,
) -> RoutingDecision | None:
    """Handle user choice for a duplicate/near-duplicate match.

    Returns RoutingDecision if user chose to update, None otherwise.
    """
    if choice.lower() == "u":
        space_cfg = config.get_space(dup["space"])
        group_field = space_cfg.group_by if space_cfg else "client"
        import json
        row = db.get_note(dup["id"])
        sm = json.loads(row.get("space_metadata", "{}")) if row else {}
        group_slug = sm.get(group_field, "")

        return RoutingDecision(
            space=dup["space"],
            group_slug=group_slug,
            group_display=group_slug.replace("-", " ").title(),
            append_to_note=dup["id"],
        )
    return None


def route_input(
    config: NotelyConfig,
    db: Database,
    vec_store: Any,
    raw_text: str,
    user_context: str | None = None,
    hints: dict[str, str] | None = None,
    folder_default: dict | None = None,
) -> RoutingDecision | None:
    """Main routing pipeline: 3-layer duplicate/similarity detection + user confirmation.

    The pipeline runs three layers in order, stopping at the first match:

    Layer 1 -- Exact hash: SHA256 of full paste content. Catches re-pastes of
        identical text. User chooses [U]pdate / [N]ew / [S]kip.

    Layer 2 -- Snippet hash: SHA256 of first 300 chars. Catches same content
        with minor edits or additions at the end. Same prompt as Layer 1.

    Layer 3 -- Vector search: Embeds the context string (user-typed context +
        raw text snippet) and searches both directories and note summaries in
        LanceDB. Results are presented via present_matches() which has its own
        decision tree based on distance thresholds (<0.2, <0.4, etc.).

    Hash checks use paste content only (not typed context) so "meeting notes
    [paste]" and "[paste]" match the same hash. If _TypedInput is provided,
    its paste_content attribute is used for hashing.

    If CLI hints fully specify routing (space + group), skips search entirely.
    If folder_default is set and no matches found, routes to that folder
    instead of asking "Any context?" or falling back to manual routing.
    If no matches found and no user context, prompts "Any context?" before
    falling back to manual routing.

    Args:
        config: NotelyConfig instance.
        db: Database instance for hash lookups and note queries.
        vec_store: VectorStore instance for semantic search (or None).
        raw_text: The raw input text (may be a _TypedInput with paste_content).
        user_context: Text the user typed alongside the paste (used for search).
        hints: CLI-provided hints like {"space": "clients", "client": "acme"}.
        folder_default: Working folder dict {"space", "group_slug", "display"}.
            Used as fallback when no matches found.

    Returns:
        RoutingDecision with space, group, optional subgroup, and optional
        append_to_note (note ID if updating). Returns None if user skips/cancels.
    """

    hints = hints or {}

    # Step 0: Duplicate detection (two layers)
    # Use paste content if available — typed context shouldn't affect the hash
    hash_text = raw_text
    if hasattr(raw_text, "paste_content") and raw_text.paste_content:
        hash_text = raw_text.paste_content

    # Layer 1: Exact match (full content hash)
    dup = db.find_exact_duplicate(hash_text)
    if dup:
        dup_choice = duplicate_found(dup["title"], dup["date"], "exact", console=console)
        if dup_choice == "update":
            result = _handle_dup_choice("u", dup, config, db)
            if result is not None:
                return result
        elif dup_choice == "records":
            result = _handle_dup_choice("u", dup, config, db)
            if result is not None:
                result.records_only = True
                result.existing_note_id = dup["id"]
                return result
        elif dup_choice == "skip":
            return None

    # Layer 2: Snippet match (first 300 chars — same content with edits)
    if not dup:
        snippet_match = db.find_snippet_match(hash_text)
        if snippet_match:
            dup_choice = duplicate_found(snippet_match["title"], snippet_match["date"], "near", console=console)
            if dup_choice == "update":
                result = _handle_dup_choice("u", snippet_match, config, db)
                if result is not None:
                    return result
            elif dup_choice == "records":
                result = _handle_dup_choice("u", snippet_match, config, db)
                if result is not None:
                    result.records_only = True
                    result.existing_note_id = snippet_match["id"]
                    return result
            elif dup_choice == "skip":
                return None

    # If hints fully specify routing, use them directly
    if hints.get("space") and (hints.get("client") or hints.get("category")):
        return _routing_from_hints(config, db, hints)

    # Extract context for search
    context = extract_context(raw_text, user_context)
    if not context:
        return ask_routing_manually(config, hints=hints)

    # Search for matches, narrowing by hints if provided
    space_hint = hints.get("space")
    group_hint = hints.get("client") or hints.get("category")
    try:
        dir_matches = vec_store.search_directories(
            context, limit=5, space=space_hint, group_slug=group_hint,
        )
        note_matches = vec_store.search_notes(
            context, limit=5, space=space_hint, group_slug=group_hint,
        )
    except Exception as e:
        logger.debug(f"Vector search failed: {e}")
        dir_matches = []
        note_matches = []

    if not dir_matches and not note_matches:
        # Working folder set — use it as default instead of prompting
        if folder_default:
            return _routing_from_folder_default(config, db, folder_default)
        # No matches and no typed context — ask for context before manual routing
        if not user_context:
            extra = Prompt.ask("[dim]Any context? (Enter to skip)[/dim]", default="")
            if extra.strip():
                context = extract_context(raw_text, extra.strip())
                try:
                    dir_matches = vec_store.search_directories(
                        context, limit=5, space=space_hint, group_slug=group_hint,
                    )
                    note_matches = vec_store.search_notes(
                        context, limit=5, space=space_hint, group_slug=group_hint,
                    )
                except Exception:
                    pass
                if dir_matches or note_matches:
                    return present_matches(config, db, dir_matches, note_matches, vec_store, raw_text)
        return ask_routing_manually(config, hints=hints, context=context)

    return present_matches(config, db, dir_matches, note_matches, vec_store, raw_text)


def _routing_from_folder_default(
    config: NotelyConfig, db: Database, folder_default: dict,
) -> RoutingDecision:
    """Build a RoutingDecision from the working folder default."""
    space = folder_default["space"]
    group_slug = folder_default.get("group_slug", "")
    display = folder_default.get("display", group_slug or space)

    # Check if folder is space-level (no group_slug)
    if not group_slug:
        # Space-level — use space name as both space and group
        return RoutingDecision(
            space=space,
            group_slug="",
            group_display=display,
        )

    # Check for subgroup (group_slug contains "/")
    subgroup_slug = None
    subgroup_display = None
    if "/" in group_slug:
        parts = group_slug.split("/", 1)
        group_slug = parts[0]
        subgroup_slug = parts[1]
        # Look up display names from DB
        dirs = db.get_all_directories()
        for d in dirs:
            if d["space"] == space and d["group_slug"] == parts[0] and d.get("subgroup_slug") == parts[1]:
                subgroup_display = d["display_name"]
                break
        if not subgroup_display:
            subgroup_display = subgroup_slug
        # Find group display name
        for d in dirs:
            if d["space"] == space and d["group_slug"] == parts[0] and not d.get("subgroup_slug"):
                display = d["display_name"]
                break

    return RoutingDecision(
        space=space,
        group_slug=group_slug,
        group_display=display,
        subgroup_slug=subgroup_slug,
        subgroup_display=subgroup_display,
    )


def _routing_from_note(note: dict[str, Any]) -> RoutingDecision:
    """Build a RoutingDecision to merge into an existing note."""
    return RoutingDecision(
        space=note["space"],
        group_slug=note["group_slug"],
        group_display=note.get("group_slug", "").replace("-", " ").title(),
        subgroup_slug=note.get("subgroup_slug"),
        append_to_note=note["note_id"],
    )


def _routing_from_dir(d: dict[str, Any]) -> RoutingDecision:
    """Build a RoutingDecision for a directory match."""
    return RoutingDecision(
        space=d["space"],
        group_slug=d["group_slug"],
        group_display=d["display_name"],
        subgroup_slug=d.get("subgroup_slug"),
    )


def _pick_folder(
    config: NotelyConfig,
    dirs: list[dict[str, Any]],
) -> RoutingDecision | None:
    """Show suggested folders and prompt with tab completion across ALL folders.

    Suggested folders (from vector search) are shown as numbered shortcuts.
    The prompt itself has full autocomplete so the user can type any folder
    name and tab-complete — no secondary prompt needed.
    """
    top_dirs = [d for d in dirs[:5] if d.get("group_slug")] if dirs else []

    # Show suggestions above the prompt
    if top_dirs:
        for i, d in enumerate(top_dirs, 1):
            console.print(f"  [{i}] {d['display_name']}")
        console.print(r"  \[s] Skip")

    return _prompt_folder_with_autocomplete(
        config,
        suggested_dirs=top_dirs,
    )


def present_matches(
    config: NotelyConfig,
    db: Database,
    dir_matches: list[dict[str, Any]],
    note_matches: list[dict[str, Any]],
    vec_store: Any = None,
    raw_text: str = "",
) -> RoutingDecision | None:
    """Present search results: note update and folder pick are separate steps.

    Decision tree:
      1. Close note (dist < 0.2) → "You already have 'X'. Update?" [u/n/s]
      2. Close note (dist < 0.4) → "Update 'X'?" [u/n/s] (one at a time)
      3. Pick folder → numbered folder list OR autocomplete prompt
         Typing a new name creates the folder.

    Notes and folders are never mixed in the same list.
    Returns None if user cancels.
    """
    # --- Gather close note matches ---
    close_notes: list[dict[str, Any]] = []
    seen_note_ids: set[str] = set()
    for nm in note_matches:
        if nm["note_id"] in seen_note_ids:
            continue
        seen_note_ids.add(nm["note_id"])
        if nm.get("_distance", 1) < DIST_GOOD_MATCH:
            close_notes.append(nm)

    # --- Gather directory matches (skip space-level entries without group) ---
    dirs: list[dict[str, Any]] = []
    seen_dir_keys: set[tuple[str, str]] = set()
    for dm in dir_matches:
        if not dm.get("group_slug"):
            continue
        key = (dm["space"], dm["group_slug"])
        if key in seen_dir_keys:
            continue
        seen_dir_keys.add(key)
        dirs.append(dm)

    # ── Step 1: Note update check (notes and folders are separate) ──
    if close_notes:
        best = close_notes[0]
        # Very close match — likely duplicate
        if best.get("_distance", 1) < DIST_DUPLICATE:
            dup_choice = duplicate_found(best["title"], best["date"], "similar", console=console)
        else:
            # Close but not duplicate — ask about update
            dup_choice = duplicate_found(best["title"], best["date"], "related", console=console)

        if dup_choice == "update":
            return _routing_from_note(best)
        if dup_choice == "skip":
            return None
        # User chose "new" — fall through to folder pick

    # ── Step 2: Pick folder (never mixed with notes) ──
    return _pick_folder(config, dirs)


class _Back:
    """Sentinel: go back to the exploration loop."""


_BACK = _Back()


def explore_routing(
    config: NotelyConfig,
    db: Database,
    vec_store: Any,
    raw_text: str = "",
) -> RoutingDecision | None:
    """Interactive search loop for "Somewhere else" routing.

    Triggered when the user selects "Somewhere else" from the routing options.
    The user describes their intended destination in natural language, and the
    system searches directories and notes via vector similarity. Results are
    presented as a numbered list of folders and related notes.

    The loop supports:
    - Picking a folder to browse (shows recent notes, offers new/update/back)
    - Picking a related note (offers update/browse folder/back)
    - [r] Refine search with new terms
    - [n] Create a new folder via manual routing
    - [q] Cancel entirely

    Uses the _BACK sentinel internally to distinguish "go back to the search
    loop" from "cancel the whole operation" (None). When a sub-action like
    _browse_directory returns _BACK, the loop continues; when it returns None,
    explore_routing returns None.

    Falls back to ask_routing_manually() if vec_store is None, vectors are
    unavailable, or the user presses Enter on the description prompt.

    Args:
        config: NotelyConfig instance.
        db: Database instance for querying notes within folders.
        vec_store: VectorStore instance for semantic search.
        raw_text: Raw input text, appended as a snippet to user queries
            for better search relevance.

    Returns:
        RoutingDecision if the user picks a destination, or None if cancelled.
    """
    # Check vectors availability
    if vec_store is None:
        return ask_routing_manually(config)
    try:
        available = vec_store.is_available()
    except Exception:
        available = False
    if not available:
        return ask_routing_manually(config)

    while True:
        user_query = Prompt.ask(
            "\n[bold]Describe where this should go[/bold] [dim](Enter to pick manually)[/dim]",
            default="",
        )

        if not user_query.strip():
            return ask_routing_manually(config)

        # Use user's query as-is — don't append raw content snippet
        # which can drown out the user's specific destination name
        query_context = user_query

        try:
            dir_matches = vec_store.search_directories(query_context, limit=8)
            note_matches = vec_store.search_notes(query_context, limit=5)
        except Exception as e:
            logger.debug(f"Vector search failed in explore_routing: {e}")
            return ask_routing_manually(config)

        # Name-based directory matching — supplements vector search so exact
        # name matches aren't missed when embeddings are dominated by note content
        query_lower = user_query.lower()
        name_matched_dirs: list[dict[str, Any]] = []
        try:
            all_dirs = db.get_all_directories()
            for d in all_dirs:
                slug = d.get("group_slug", "")
                display = d.get("display_name", "")
                if not slug:
                    continue
                if query_lower in slug.lower() or query_lower in display.lower():
                    name_matched_dirs.append(d)
        except Exception:
            pass

        # Filter: skip space-level dirs, deduplicate on (space, group_slug), cap at 5
        # Name matches first, then vector matches
        dirs: list[dict[str, Any]] = []
        seen_dir_keys: set[tuple[str, str]] = set()
        for dm in name_matched_dirs + dir_matches:
            if not dm.get("group_slug"):
                continue
            key = (dm["space"], dm["group_slug"])
            if key in seen_dir_keys:
                continue
            seen_dir_keys.add(key)
            dirs.append(dm)
            if len(dirs) >= 5:
                break

        # Filter notes: distance < 0.6, deduplicate, cap at 3
        notes: list[dict[str, Any]] = []
        seen_note_ids: set[str] = set()
        for nm in note_matches:
            if nm["note_id"] in seen_note_ids:
                continue
            seen_note_ids.add(nm["note_id"])
            if nm.get("_distance", 1) < DIST_WEAK_MATCH:
                notes.append(nm)
            if len(notes) >= 3:
                break

        if not dirs and not notes:
            console.print("[yellow]No matches. Try different terms.[/yellow]")
            continue

        # Display results
        console.print()
        explore_idx_map: list[tuple[str, dict[str, Any]]] = []  # 0-indexed
        explore_items: list[tuple[str, str]] = []

        if dirs:
            console.print("[bold]Folders:[/bold]")
            for d in dirs:
                label = d["display_name"]
                if d.get("note_count"):
                    label += f" ({d['note_count']} notes)"
                space_label = f"  [dim]{d['space']}[/dim]"
                explore_items.append((str(len(explore_items) + 1), f"{label}{space_label}"))
                explore_idx_map.append(("dir", d))

        if notes:
            console.print("[bold]Related notes:[/bold]")
            for n in notes:
                folder = n["group_slug"].replace("-", " ").title()
                explore_items.append((
                    str(len(explore_items) + 1),
                    f"[cyan]'{n['title']}'[/cyan] ({n['date']})  [dim]{n['space']}/{folder}[/dim]",
                ))
                explore_idx_map.append(("note", n))

        extras = [("r", "Refine search"), ("n", "Create new folder"), ("q", "Cancel")]
        choice = pick_from_list(explore_items, extras=extras, default="1", console=console)

        if choice is None or choice == "q":
            return None
        if choice == "r":
            continue
        if choice == "n":
            return ask_routing_manually(config)

        try:
            picked_idx = int(choice) - 1
        except ValueError:
            console.print("[yellow]Invalid choice.[/yellow]")
            continue

        if picked_idx < 0 or picked_idx >= len(explore_idx_map):
            console.print("[yellow]Invalid choice.[/yellow]")
            continue

        kind, data = explore_idx_map[picked_idx]

        if kind == "dir":
            # Space-level match (no group yet) — shouldn't happen since we filter, but guard
            if not data.get("group_slug"):
                return ask_routing_manually(config, hints={"space": data["space"]})

            result = _browse_directory(config, db, data)
            if isinstance(result, _Back):
                continue
            return result

        if kind == "note":
            result = _handle_note_pick(config, db, data)
            if isinstance(result, _Back):
                continue
            return result


def _browse_directory(
    config: NotelyConfig,
    db: Database,
    dir_info: dict[str, Any],
) -> RoutingDecision | _Back | None:
    """Browse a directory: show recent notes, offer new/update/back.

    Returns RoutingDecision, _BACK (go back to search), or None (cancel).
    """
    space = dir_info["space"]
    display = dir_info["display_name"]
    group_slug = dir_info["group_slug"]
    subgroup_slug = dir_info.get("subgroup_slug")

    space_cfg = config.get_space(space)
    group_field = space_cfg.group_by if space_cfg else "client"

    recent_notes = db.get_recent_notes_in_group(space, group_field, group_slug, limit=10)

    console.print(f"\n[bold]{display}[/bold] [dim]({space})[/dim]")

    if recent_notes:
        console.print("[dim]Recent notes:[/dim]")
        for i, n in enumerate(recent_notes, 1):
            console.print(f"  {i}. [cyan]'{n['title']}'[/cyan] ({n['date']})")
    else:
        console.print("[dim]No notes yet.[/dim]")

    extras = [("n", "New note here")]
    if recent_notes:
        extras.append(("u", "Update an existing note"))
    extras.extend([("b", "Back to search"), ("q", "Cancel")])

    choice = pick_from_list([], extras=extras, default="n", console=console)

    if choice is None or choice == "q":
        return None
    if choice == "b":
        return _BACK
    if choice == "n":
        return RoutingDecision(
            space=space,
            group_slug=group_slug,
            group_display=display,
            subgroup_slug=subgroup_slug,
        )
    if choice == "u" and recent_notes:
        console.print("[dim]Which note? (number)[/dim]")
        num = Prompt.ask("Note #", default="1")
        try:
            note_idx = int(num) - 1
            if 0 <= note_idx < len(recent_notes):
                picked = recent_notes[note_idx]
                return RoutingDecision(
                    space=space,
                    group_slug=group_slug,
                    group_display=display,
                    subgroup_slug=subgroup_slug,
                    append_to_note=picked["id"],
                )
        except ValueError:
            pass
        console.print("[yellow]Invalid choice.[/yellow]")
        return _BACK

    console.print("[yellow]Invalid choice.[/yellow]")
    return _BACK


def _handle_note_pick(
    config: NotelyConfig,
    db: Database,
    note_match: dict[str, Any],
) -> RoutingDecision | _Back | None:
    """Handle when user picks a specific note from search results.

    Options: update this note, browse its folder, back, or cancel.
    Returns RoutingDecision, _BACK, or None.
    """
    title = note_match["title"]
    space = note_match["space"]
    group_slug = note_match["group_slug"]
    subgroup_slug = note_match.get("subgroup_slug")
    folder = group_slug.replace("-", " ").title()

    console.print(
        f"\n[cyan]'{title}'[/cyan] in [bold]{folder}[/bold] [dim]({space})[/dim]"
    )
    extras = [("u", "Update this note"), ("f", "Browse folder"), ("b", "Back to search"), ("q", "Cancel")]
    choice = pick_from_list([], extras=extras, default="u", console=console)

    if choice is None or choice == "q":
        return None
    if choice == "b":
        return _BACK
    if choice == "u":
        return RoutingDecision(
            space=space,
            group_slug=group_slug,
            group_display=folder,
            subgroup_slug=subgroup_slug,
            append_to_note=note_match["note_id"],
        )
    if choice == "f":
        dir_info = {
            "space": space,
            "group_slug": group_slug,
            "subgroup_slug": subgroup_slug,
            "display_name": folder,
        }
        return _browse_directory(config, db, dir_info)

    console.print("[yellow]Invalid choice.[/yellow]")
    return _BACK


def _resolve_folder_text(
    config: NotelyConfig,
    text: str,
    all_dirs: list[dict] | None = None,
    ask_space: bool = True,
    create_new: bool = True,
) -> RoutingDecision | None:
    """Resolve free-text input to a RoutingDecision.

    Matches existing folders by full path, slug, or display name.
    If no match and create_new=True, treats as new folder creation.
    If no match and create_new=False, returns None (caller can fall through
    to autocomplete prompt).
    When ask_space=False, uses first space for bare names instead of prompting.
    """
    from slugify import slugify

    text = text.strip()
    if not text:
        return None

    if all_dirs is None:
        try:
            with Database(config.db_path) as db:
                db.initialize()
                all_dirs = db.get_all_directories()
        except Exception:
            all_dirs = []

    # Build folder list from dirs
    folders: list[tuple[str, str, str, str, str | None]] = []
    seen: set[str] = set()
    for d in all_dirs:
        slug = d.get("group_slug", "")
        if not slug:
            continue
        space = d["space"]
        sub = d.get("subgroup_slug")
        fp = f"{space}/{slug}/{sub}" if sub else f"{space}/{slug}"
        if fp in seen:
            continue
        seen.add(fp)
        folders.append((fp, d.get("display_name", slug), space, slug, sub))

    # Try to match an existing folder
    text_lower = text.lower()
    for fp, display, space, slug, sub in folders:
        if text_lower == fp.lower() or text_lower == display.lower() or text_lower == slug.lower():
            if sub:
                group_display = slug
                for d in all_dirs:
                    if d["space"] == space and d["group_slug"] == slug and not d.get("subgroup_slug"):
                        group_display = d["display_name"]
                        break
                return RoutingDecision(
                    space=space,
                    group_slug=slug,
                    group_display=group_display,
                    group_is_new=False,
                    subgroup_slug=sub,
                    subgroup_display=display,
                )
            return RoutingDecision(
                space=space,
                group_slug=slug,
                group_display=display,
                group_is_new=False,
            )

    # No match found
    if not create_new:
        return None

    # Treat as new folder creation
    space_names = config.space_names()
    if "/" in text:
        parts = text.split("/", 1)
        space = parts[0]
        group_display = parts[1].strip()
        if not group_display:
            return None
        if space not in space_names:
            for sn in space_names:
                sc = config.get_space(sn)
                if sc and sc.display_name.lower() == space.lower():
                    space = sn
                    break
            else:
                console.print(f"[yellow]Unknown space: {space}[/yellow]")
                return None
    else:
        group_display = text
        if len(space_names) == 1:
            space = space_names[0]
        elif not ask_space:
            space = space_names[0]
        else:
            console.print("\n[bold]Which space?[/bold]")
            for i, name in enumerate(space_names, 1):
                sc = config.get_space(name)
                desc = f" -- {sc.description}" if sc and sc.description else ""
                console.print(f"  [{i}] {sc.display_name if sc else name}{desc}")
            choice = Prompt.ask("Space", default="1")
            try:
                idx = int(choice) - 1
                space = space_names[idx]
            except (ValueError, IndexError):
                if choice in space_names:
                    space = choice
                else:
                    console.print("[yellow]Invalid choice.[/yellow]")
                    return None

    group_slug = slugify(group_display)
    group_is_new = not config.group_dir(space, group_slug).exists()

    if group_is_new:
        # Create directory eagerly so it persists even if the user Ctrl+C's
        config.group_dir(space, group_slug).mkdir(parents=True, exist_ok=True)
        display_name = group_display.replace("-", " ").title() if group_display == group_slug else group_display
        dir_id = f"{space}/{group_slug}"
        try:
            with Database(config.db_path) as db:
                db.initialize()
                db.upsert_directory(
                    dir_id=dir_id,
                    space=space,
                    group_slug=group_slug,
                    display_name=display_name,
                    description="",
                )
        except Exception:
            logger.debug("Failed to register new directory in DB", exc_info=True)
        try:
            from .vectors import try_vector_sync_directory
            try_vector_sync_directory(
                config,
                dir_id=dir_id,
                space=space,
                group_slug=group_slug,
                display_name=display_name,
                description="",
            )
        except Exception:
            pass
        console.print(f"[dim]Created folder: {space}/{group_slug}[/dim]")

    return RoutingDecision(
        space=space,
        group_slug=group_slug,
        group_display=group_display,
        group_is_new=group_is_new,
    )


def _prompt_folder_with_autocomplete(
    config: NotelyConfig,
    prefill: str = "",
    suggested_dirs: list[dict[str, Any]] | None = None,
    default: str = "",
) -> RoutingDecision | None:
    """Single prompt with tab autocomplete across all folders.

    Args:
        suggested_dirs: Numbered shortcut folders shown above the prompt.
            "1".."N" picks from this list, "s" skips.
        default: Default value if user presses Enter.
    """
    from prompt_toolkit import prompt as pt_prompt
    from prompt_toolkit.completion import Completer, Completion

    suggested_dirs = suggested_dirs or []

    # Load all directories — DB first, filesystem fallback for empty directories table
    try:
        with Database(config.db_path) as db:
            db.initialize()
            all_dirs = db.get_all_directories()
    except Exception:
        all_dirs = []

    # Filesystem fallback: scan notes/ for folders not yet in DB
    seen_paths = {f"{d['space']}/{d.get('group_slug', '')}" for d in all_dirs if d.get("group_slug")}
    if config.notes_dir.exists():
        for space_dir in sorted(config.notes_dir.iterdir()):
            if not space_dir.is_dir() or space_dir.name.startswith("."):
                continue
            for group_dir in sorted(space_dir.iterdir()):
                if not group_dir.is_dir() or group_dir.name.startswith("."):
                    continue
                fp = f"{space_dir.name}/{group_dir.name}"
                if fp not in seen_paths:
                    seen_paths.add(fp)
                    all_dirs.append({
                        "space": space_dir.name,
                        "group_slug": group_dir.name,
                        "display_name": group_dir.name.replace("-", " ").title(),
                    })

    # Build completion list: full paths with display names
    # (full_path, display_name, space, group_slug, subgroup_slug)
    folders: list[tuple[str, str, str, str, str | None]] = []
    seen = set()
    for d in all_dirs:
        slug = d.get("group_slug", "")
        if not slug:
            continue
        space = d["space"]
        sub = d.get("subgroup_slug")
        if sub:
            full_path = f"{space}/{slug}/{sub}"
        else:
            full_path = f"{space}/{slug}"
        if full_path in seen:
            continue
        seen.add(full_path)
        folders.append((full_path, d.get("display_name", slug), space, slug, sub))

    space_names = config.space_names()

    class _FolderCompleter(Completer):
        def get_completions(self, document, complete_event):
            text = document.text_before_cursor.lower()
            cursor_len = len(document.text_before_cursor)

            if not text:
                # Empty → show all config spaces as starting points
                for sn in space_names:
                    sc = config.get_space(sn)
                    yield Completion(
                        f"{sn}/",
                        start_position=-cursor_len,
                        display_meta=sc.display_name if sc else sn,
                    )
                return

            if "/" in text:
                # Has slash → prefix match for drill-down
                for fp, display, space, slug, sub in folders:
                    if fp.lower().startswith(text) and fp.lower() != text.rstrip("/"):
                        yield Completion(
                            fp,
                            start_position=-cursor_len,
                            display_meta=display,
                        )
                # Also show space names that match the prefix (for spaces without groups yet)
                prefix = text.rstrip("/")
                for sn in space_names:
                    if sn.lower() == prefix and not any(
                        fp.lower().startswith(text) for fp, *_ in folders
                    ):
                        # Space exists but has no groups — offer the space itself
                        sc = config.get_space(sn)
                        yield Completion(
                            sn,
                            start_position=-cursor_len,
                            display_meta=sc.display_name if sc else sn,
                        )
            else:
                # No slash → fuzzy match spaces + existing folders
                for sn in space_names:
                    if text in sn.lower():
                        sc = config.get_space(sn)
                        yield Completion(
                            f"{sn}/",
                            start_position=-cursor_len,
                            display_meta=sc.display_name if sc else sn,
                        )
                for fp, display, space, slug, sub in folders:
                    if text in slug.lower() or text in display.lower():
                        yield Completion(
                            fp,
                            start_position=-cursor_len,
                            display_meta=display,
                        )

    hint = "[dim]Tab to browse folders, type a name to create new, s to skip[/dim]"
    if not suggested_dirs:
        hint = "[dim]Tab to browse folders, type a name to create new[/dim]"
    console.print(hint)
    try:
        result = pt_prompt(
            "Folder: ",
            completer=_FolderCompleter(),
            default=prefill or default,
        )
    except (KeyboardInterrupt, EOFError):
        return None

    result = result.strip()

    # Handle skip
    if result.lower() == "s" and suggested_dirs:
        return None

    # Handle numbered shortcut for suggested folders
    if suggested_dirs:
        try:
            idx = int(result) - 1
            if 0 <= idx < len(suggested_dirs):
                return _routing_from_dir(suggested_dirs[idx])
        except ValueError:
            pass

    if not result:
        # Enter with no default → skip
        return None

    return _resolve_folder_text(config, result, all_dirs=all_dirs)


def ask_routing_manually(
    config: NotelyConfig,
    hints: dict[str, str] | None = None,
    context: str = "",
) -> RoutingDecision | None:
    """Fall back to asking the user for routing with folder autocomplete.

    If hints provide enough info, builds routing directly.
    Otherwise, shows top 3 most relevant folders as numbered options,
    ranked by relevance to the context string.
    Returns None if user cancels.
    """
    from slugify import slugify

    hints = hints or {}

    # If hints fully specify, skip the prompt
    space = hints.get("space")
    group_raw = hints.get("client") or hints.get("category")
    if space and group_raw:
        return _routing_from_hints(config, Database(config.db_path), hints)

    # Load existing folders for numbered options
    try:
        with Database(config.db_path) as db:
            db.initialize()
            all_dirs = db.get_all_directories()
    except Exception:
        all_dirs = []

    # Build folder list
    all_folder_items: list[tuple[str, str]] = []  # (full_path, display_label)
    all_folder_data: list[tuple[str, str, str | None]] = []  # (space, group_slug, sub)
    seen: set[str] = set()
    for d in all_dirs:
        slug = d.get("group_slug", "")
        if not slug:
            continue
        s = d["space"]
        sub = d.get("subgroup_slug")
        fp = f"{s}/{slug}/{sub}" if sub else f"{s}/{slug}"
        if fp in seen:
            continue
        seen.add(fp)
        display = d.get("display_name", slug)
        sc = config.get_space(s)
        space_label = sc.display_name if sc else s
        label = f"{display} ({space_label})" if display.lower() != s.lower() else display
        all_folder_items.append((fp, label))
        all_folder_data.append((s, slug, sub))

    if not all_folder_items:
        return _prompt_folder_with_autocomplete(config)

    # Rank by relevance to context — substring match on display name / slug
    context_lower = context.lower() if context else ""
    context_words = context_lower.split() if context_lower else []

    def _relevance(idx: int) -> float:
        """Higher = more relevant. Substring match scores highest."""
        fp, label = all_folder_items[idx]
        _space, slug, _sub = all_folder_data[idx]
        name_lower = slug.lower()
        display_lower = label.lower().split(" (")[0]  # strip space suffix
        score = 0.0
        for word in context_words:
            if word == name_lower or word == display_lower:
                score += 10.0  # exact word match
            elif word in name_lower or word in display_lower:
                score += 5.0  # substring match
            elif name_lower in word or display_lower in word:
                score += 3.0  # folder name is part of a context word
        return score

    ranked = sorted(range(len(all_folder_items)), key=_relevance, reverse=True)

    # Show top 3 as suggestions, then autocomplete prompt
    MAX_OPTIONS = 3
    top_indices = ranked[:MAX_OPTIONS]

    # Build suggested_dirs list for the autocomplete prompt
    suggested = []
    for i in top_indices:
        s, slug, sub = all_folder_data[i]
        display = all_folder_items[i][1]
        d = {"space": s, "group_slug": slug, "display_name": display}
        if sub:
            d["subgroup_slug"] = sub
        suggested.append(d)

    console.print("\n[bold]Which folder?[/bold]")
    for i, d in enumerate(suggested, 1):
        console.print(f"  [{i}] {d['display_name']}")
    console.print(r"  \[s] Skip")

    return _prompt_folder_with_autocomplete(config, suggested_dirs=suggested)


def _routing_from_hints(
    config: NotelyConfig,
    db: Database,
    hints: dict[str, str],
) -> RoutingDecision:
    """Build a RoutingDecision directly from CLI hints."""
    from slugify import slugify

    space = hints["space"]
    space_cfg = config.get_space(space)

    group_raw = hints.get("client") or hints.get("category") or ""
    group_slug = slugify(group_raw)
    group_display = group_raw

    group_is_new = not config.group_dir(space, group_slug).exists()

    subgroup_slug = None
    subgroup_display = None
    subgroup_is_new = False

    if space_cfg and space_cfg.subgroup_by:
        sub_raw = hints.get("topic") or hints.get(space_cfg.subgroup_by)
        if sub_raw:
            subgroup_slug = slugify(sub_raw)
            subgroup_display = sub_raw
            subgroup_is_new = not config.subgroup_dir(space, group_slug, subgroup_slug).exists()

    return RoutingDecision(
        space=space,
        group_slug=group_slug,
        group_display=group_display,
        group_is_new=group_is_new,
        subgroup_slug=subgroup_slug,
        subgroup_display=subgroup_display,
        subgroup_is_new=subgroup_is_new,
    )


def ensure_directory_indexed(
    config: NotelyConfig,
    db: Database,
    routing: RoutingDecision,
    note_summary: str = "",
) -> None:
    """Ensure the directory for a routing decision exists in SQLite + vectors.

    Called after a note is saved. Only creates entries for NEW groups/subgroups.
    Descriptions are refreshed in bulk on `notely open` startup instead of
    on every save — see `refresh_directory_descriptions()`.
    """
    from .vectors import try_vector_sync_directory

    if routing.group_is_new:
        dir_id = f"{routing.space}/{routing.group_slug}"
        description = f"{routing.group_display} -- {note_summary[:100]}" if note_summary else routing.group_display
        db.upsert_directory(
            dir_id=dir_id,
            space=routing.space,
            group_slug=routing.group_slug,
            display_name=routing.group_display,
            description=description,
        )
        try_vector_sync_directory(
            config,
            dir_id=dir_id,
            space=routing.space,
            group_slug=routing.group_slug,
            display_name=routing.group_display,
            description=description,
        )

    if routing.subgroup_is_new and routing.subgroup_slug:
        sub_id = f"{routing.space}/{routing.group_slug}/{routing.subgroup_slug}"
        sub_display = routing.subgroup_display or routing.subgroup_slug
        db.upsert_directory(
            dir_id=sub_id,
            space=routing.space,
            group_slug=routing.group_slug,
            display_name=sub_display,
            description=sub_display,
            subgroup_slug=routing.subgroup_slug,
        )
        try_vector_sync_directory(
            config,
            dir_id=sub_id,
            space=routing.space,
            group_slug=routing.group_slug,
            subgroup_slug=routing.subgroup_slug,
            display_name=sub_display,
            description=sub_display,
        )


def refresh_directory_descriptions(
    config: NotelyConfig,
    db: Database,
) -> int:
    """Rebuild directory descriptions from base description + sampled note summaries.

    Called once on `notely open` startup. For each directory, combines the
    folder's display name with sampled note summaries (5 recent + 5 random)
    to build a rich description for vector embedding.

    Returns the number of directories updated.
    """
    from .vectors import try_vector_sync_directory

    dirs = db.get_all_directories()
    updated = 0

    for d in dirs:
        # Skip space-level entries — those use the config description
        if not d["group_slug"]:
            continue

        space_cfg = config.get_space(d["space"])
        group_field = space_cfg.group_by if space_cfg else "client"

        all_notes = db.get_recent_notes_in_group(
            d["space"], group_field, d["group_slug"], limit=50,
        )
        if not all_notes:
            continue

        from .vectors import sample_note_summaries
        note_context = sample_note_summaries(all_notes)
        if not note_context:
            continue

        description = f"{d['display_name']} -- {note_context}"

        # Skip if description hasn't changed
        if description == d.get("description", ""):
            continue

        db.upsert_directory(
            dir_id=d["id"],
            space=d["space"],
            group_slug=d["group_slug"],
            display_name=d["display_name"],
            description=description,
            subgroup_slug=d.get("subgroup_slug"),
        )
        try_vector_sync_directory(
            config,
            dir_id=d["id"],
            space=d["space"],
            group_slug=d["group_slug"],
            subgroup_slug=d.get("subgroup_slug"),
            display_name=d["display_name"],
            description=description,
        )
        updated += 1

    return updated
