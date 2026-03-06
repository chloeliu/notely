"""Inbox review and filing — /inbox command handlers."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING

from rich.panel import Panel
from rich.prompt import Prompt

from ...config import NotelyConfig
from ...db import Database
from ...models import NoteRouting
from ...storage import show_merge_preview, edit_merge_result, apply_merge

from ._shared import console
from ._input import _handle_list_result, _handle_snippet_result

if TYPE_CHECKING:
    from ._completers import _SlashCompleter


def _handle_inbox(
    config: NotelyConfig,
    arg: str,
    working_folder: dict,
    completer: "_SlashCompleter",
) -> None:
    """View and process inbox items — review, file, skip, or pull from agents."""
    from ...db import safe_json_loads, safe_parse_tags
    from ...storage import (
        generate_file_path, save_and_sync, edit_note_in_editor,
        sync_todo_index, sync_ideas_index,
    )
    from ...models import ActionItem, InboxItem, Note, Refinement, InputSize

    parts = arg.strip().split(None, 1) if arg.strip() else []
    subcmd = parts[0].lower() if parts else ""

    # /inbox count
    if subcmd == "count":
        with Database(config.db_path) as db:
            db.initialize()
            pending = db.count_inbox("pending")
            console.print(f"[cyan]{pending} pending item(s) in inbox.[/cyan]")
        return

    # /inbox skip all
    if subcmd == "skip" and len(parts) > 1 and parts[1].strip().lower() == "all":
        with Database(config.db_path) as db:
            db.initialize()
            items = db.get_inbox_items(status="pending")
            if not items:
                console.print("[dim]No pending items.[/dim]")
                return
            for item in items:
                db.update_inbox_status(item["id"], "skipped")
            console.print(f"[yellow]Skipped {len(items)} item(s).[/yellow]")
        return

    # /inbox history
    if subcmd == "history":
        with Database(config.db_path) as db:
            db.initialize()
            items = db.get_inbox_items(status="filed")
            if not items:
                console.print("[dim]No filed items.[/dim]")
                return
            for item in items[:20]:
                console.print(
                    f"  [green]Filed:[/green] {item['title']} "
                    f"[dim]({item['source']}, {item['reviewed_at'][:10]})[/dim]"
                )
        return

    # /inbox clear [SOURCE]
    if subcmd == "clear":
        source_filter = parts[1].strip() if len(parts) > 1 else ""
        with Database(config.db_path) as db:
            db.initialize()
            where = "WHERE source = ?" if source_filter else ""
            params = (source_filter,) if source_filter else ()

            rows = db.conn.execute(
                f"SELECT status, count(*) FROM inbox {where} GROUP BY status",
                params,
            ).fetchall()
            if not rows:
                label = f" from '{source_filter}'" if source_filter else ""
                console.print(f"[dim]No inbox items{label}.[/dim]")
                return

            total = sum(r[1] for r in rows)
            breakdown = ", ".join(f"{r[1]} {r[0]}" for r in rows)
            label = f"'{source_filter}'" if source_filter else "all sources"
            console.print(f"[dim]{label}: {breakdown} ({total} total)[/dim]")
            console.print(
                "[dim]Clearing removes dedup records — "
                "next /workflow pull will re-fetch these items.[/dim]"
            )
            confirm = Prompt.ask(
                f"Delete {total} item(s)?",
                choices=["y", "n"], default="n",
            )
            if confirm == "y":
                db.conn.execute(f"DELETE FROM inbox {where}", params)
                db.conn.commit()
                console.print(f"[yellow]Deleted {total} item(s).[/yellow]")
        return

    # Redirect old create/pull to /workflow
    if subcmd == "create":
        console.print("[dim]Use /workflow create to create workflows.[/dim]")
        return

    if subcmd == "pull":
        console.print("[dim]Use /workflow pull to run workflows.[/dim]")
        return

    if subcmd:
        console.print(f"[yellow]Unknown subcommand: {subcmd}[/yellow]")
        console.print("[dim]Usage: /inbox | /inbox count | /inbox skip all | /inbox history | /inbox clear [SOURCE][/dim]")
        return

    # /inbox — show pending items and review one by one
    with Database(config.db_path) as db:
        db.initialize()
        items = db.get_inbox_items(status="pending")

        if not items:
            console.print("[dim]Inbox is empty.[/dim]")
            return

        console.print(f"[cyan]{len(items)} pending item(s) in inbox.[/cyan]")

        for idx, row in enumerate(items):
            # Parse JSON fields
            tags = safe_parse_tags(row.get("tags"))
            participants = safe_json_loads(row.get("participants"), [])
            action_items_raw = safe_json_loads(row.get("action_items"), [])
            action_items = [
                ActionItem(
                    owner=a.get("owner", ""),
                    task=a.get("task", ""),
                    due=a.get("due"),
                    status=a.get("status", "open"),
                )
                for a in action_items_raw
            ]

            # Build preview
            lines = [
                f"[bold]Source:[/bold]   {row['source']}",
                f"[bold]Title:[/bold]    {row['title']}",
            ]
            if row.get("summary"):
                lines.append(f"[bold]Summary:[/bold]  {row['summary']}")
            if tags:
                lines.append(f"[bold]Tags:[/bold]     {', '.join(tags)}")
            if participants:
                lines.append(f"[bold]People:[/bold]   {', '.join(participants)}")
            if action_items:
                lines.append(f"[bold]Actions:[/bold]  {len(action_items)} item(s)")
                for a in action_items:
                    due_str = f" (due {a.due})" if a.due else ""
                    lines.append(f"    - [cyan]{a.owner}[/cyan]: {a.task}{due_str}")
            if row.get("source_url"):
                lines.append(f"[bold]URL:[/bold]      {row['source_url']}")
            if row.get("body"):
                lines.append("")
                for bl in row["body"].strip().splitlines()[:20]:
                    lines.append(f"[dim]{bl}[/dim]")
                body_lines = row["body"].strip().splitlines()
                if len(body_lines) > 20:
                    lines.append(f"[dim]... ({len(body_lines) - 20} more lines)[/dim]")

            is_raw = not row.get("processed", 1)
            panel_title = f"Inbox [{idx + 1}/{len(items)}]"
            if is_raw:
                panel_title += " [yellow](raw — needs processing)[/yellow]"

            console.print(Panel(
                "\n".join(lines),
                title=panel_title,
                border_style="cyan",
            ))

            # Raw items: offer to process with AI or save as-is
            if is_raw:
                try:
                    raw_choice = Prompt.ask(
                        r"\[p]rocess with AI / \[s]ave as-is / \[k]ip",
                        default="p",
                    )
                except KeyboardInterrupt:
                    console.print("\n[yellow]Inbox review cancelled.[/yellow]")
                    return

                if raw_choice.lower() == "k":
                    db.update_inbox_status(row["id"], "skipped")
                    console.print("[yellow]Skipped.[/yellow]")
                    continue

                metadata = safe_json_loads(row.get("metadata"), {})

                if raw_choice.lower() == "p":
                    _process_raw_inbox_item(
                        config, db, row, metadata, tags, participants,
                        action_items, working_folder, completer,
                    )
                    continue

                # Default: save as-is
                _save_raw_inbox_item(
                    config, db, row, tags, participants, action_items,
                    working_folder, completer,
                )
                continue

            # Route through normal pipeline — same as paste
            search_text = f"{row['title']}. {row.get('summary', '')}"
            if row.get("body"):
                search_text += f"\n\n{row['body'][:500]}"

            try:
                from ...vectors import get_vector_store
                vec = get_vector_store(config)
            except Exception:
                vec = None

            try:
                from ...routing import route_input
                routing = route_input(
                    config, db, vec, search_text,
                    folder_default=working_folder,
                )
            except KeyboardInterrupt:
                console.print("\n[yellow]Inbox review cancelled.[/yellow]")
                return

            if routing is None:
                db.update_inbox_status(row["id"], "skipped")
                console.print("[yellow]Skipped.[/yellow]")
                continue

            # Merge with existing note
            if routing.append_to_note:
                from ...storage import read_note
                from ...ai import merge_with_existing
                existing_db_row = db.get_note(routing.append_to_note)
                if not existing_db_row:
                    console.print("[yellow]Note not found, skipping.[/yellow]")
                    continue
                existing = read_note(config, existing_db_row["file_path"])
                if not existing:
                    console.print("[yellow]Could not read note, skipping.[/yellow]")
                    continue

                # Build merge text from inbox item
                merge_text = row.get("body", "")
                if row.get("summary"):
                    merge_text = f"Summary: {row['summary']}\n\n{merge_text}"

                space_cfg = config.get_space(routing.space)
                space_config_dict = {
                    "group_by": space_cfg.group_by if space_cfg else "client",
                    "fields": space_cfg.fields if space_cfg else [],
                } if space_cfg else {}

                console.print(f"[dim]Merging with '{existing.title}'...[/dim]")
                try:
                    merge_result = merge_with_existing(
                        merge_text, existing, space_config_dict,
                        InputSize.MEDIUM,
                        user_name=config.user_name,
                        workspace_path=config.base_dir,
                    )
                except Exception as e:
                    console.print(f"[red]Merge failed: {e}[/red]")
                    continue

                has_changes = show_merge_preview(existing, merge_result)
                if not has_changes:
                    console.print("[dim]No new information to merge.[/dim]")
                    db.update_inbox_status(row["id"], "skipped")
                    continue

                try:
                    merge_choice = Prompt.ask(
                        r"\[Y]es, merge / \[e]dit / \[n]o, skip",
                        default="Y",
                    )
                except KeyboardInterrupt:
                    console.print("\n[yellow]Inbox review cancelled.[/yellow]")
                    return

                if merge_choice.lower() == "n":
                    db.update_inbox_status(row["id"], "skipped")
                    console.print("[yellow]Skipped.[/yellow]")
                    continue
                if merge_choice.lower() == "e":
                    merge_result = edit_merge_result(existing, merge_result)
                    show_merge_preview(existing, merge_result)

                apply_merge(config, db, existing, merge_result, merge_text, merge_text)
                db.update_inbox_status(row["id"], "filed", filed_note_id=existing.id)

                if existing.action_items:
                    sync_todo_index(config, db)
                sync_ideas_index(config, db)
                console.print(f"[green]Merged into:[/green] {existing.file_path}")
                completer.invalidate_todos()
                continue

            # New note — build from inbox item (no AI structuring needed)
            import uuid
            now = datetime.now(timezone.utc).isoformat()
            note_id = uuid.uuid4().hex[:8]
            target_space = routing.space
            target_group = routing.group_slug

            note_date = row.get("created", now)[:10]  # YYYY-MM-DD from ISO
            rel_path = generate_file_path(
                config, target_space, target_group, note_date, row["title"],
            )

            note = Note(
                id=note_id,
                space=target_space,
                title=row["title"],
                source=row["source"],
                refinement=Refinement.AI_STRUCTURED,
                input_size=InputSize.MEDIUM,
                date=note_date,
                created=now,
                updated=now,
                summary=row.get("summary", ""),
                tags=tags,
                participants=participants,
                file_path=rel_path,
                body=row.get("body", ""),
                raw_text="",
                action_items=action_items,
                source_url=row.get("source_url", ""),
            )

            # Quick confirm before save
            try:
                save_choice = Prompt.ask(
                    r"\[Y]es, save / \[e]dit first / \[n]o, skip",
                    default="Y",
                )
            except KeyboardInterrupt:
                console.print("\n[yellow]Inbox review cancelled.[/yellow]")
                return

            if save_choice.lower() == "n":
                db.update_inbox_status(row["id"], "skipped")
                console.print("[yellow]Skipped.[/yellow]")
                continue

            if save_choice.lower() == "e":
                edited = edit_note_in_editor(note)
                if edited:
                    note.file_path = generate_file_path(
                        config, target_space, target_group, note_date, note.title,
                    )
                else:
                    console.print("[dim]No changes.[/dim]")

            # Save via shared pipeline
            save_and_sync(config, db, note, routing=NoteRouting(
                space=target_space,
                group_slug=target_group,
                group_display=routing.group_display or target_group,
            ))
            db.update_inbox_status(row["id"], "filed", filed_note_id=note_id)

            if note.action_items:
                sync_todo_index(config, db)
            sync_ideas_index(config, db)

            console.print(f"[green]Filed:[/green] {note.file_path}")
            completer.invalidate_todos()


def _process_raw_inbox_item(
    config: NotelyConfig,
    db: Database,
    row: dict,
    metadata: dict,
    tags: list[str],
    participants: list[str],
    action_items: list,
    working_folder: dict,
    completer: "_SlashCompleter",
) -> None:
    """Route first, then AI-structure a raw inbox item and save.

    Flow: pick folder → AI structures → preview → save.
    Routing before AI saves an API call if the user skips.
    """
    from ...ai import structure_only, ListItemResult, SnippetResult
    from ...storage import (
        classify_input_size, generate_file_path, save_and_sync,
        edit_note_in_editor, sync_todo_index, sync_ideas_index,
    )
    from ...models import Note, Refinement, InputSize

    # Get raw data — prefer _raw from metadata, fall back to body
    raw_data = metadata.get("_raw")
    if raw_data and isinstance(raw_data, dict):
        import json
        raw_text = json.dumps(raw_data, indent=2, default=str)
    else:
        raw_text = row.get("body", "")

    if not raw_text.strip():
        console.print("[yellow]No content to process.[/yellow]")
        db.update_inbox_status(row["id"], "skipped")
        return

    # --- Step 1: Route (pick folder before spending API call) ---
    search_text = f"{row['title']}. {row.get('body', '')[:300]}"
    try:
        from ...vectors import get_vector_store
        vec = get_vector_store(config)
    except Exception:
        vec = None

    try:
        from ...routing import route_input
        routing = route_input(
            config, db, vec, search_text,
            folder_default=working_folder,
        )
    except KeyboardInterrupt:
        console.print("\n[yellow]Cancelled.[/yellow]")
        return

    if routing is None:
        db.update_inbox_status(row["id"], "skipped")
        console.print("[yellow]Skipped.[/yellow]")
        return

    # Merge with existing note
    if routing.append_to_note:
        from ...storage import read_note
        from ...ai import merge_with_existing

        existing_db_row = db.get_note(routing.append_to_note)
        if not existing_db_row:
            console.print("[yellow]Note not found, skipping.[/yellow]")
            return
        existing = read_note(config, existing_db_row["file_path"])
        if not existing:
            console.print("[yellow]Could not read note, skipping.[/yellow]")
            return

        input_size = classify_input_size(raw_text)
        space_cfg = config.get_space(routing.space)
        space_config_dict = {
            "group_by": space_cfg.group_by if space_cfg else "client",
            "fields": space_cfg.fields if space_cfg else [],
        } if space_cfg else {}

        console.print(f"[dim]Merging with '{existing.title}'...[/dim]")
        try:
            merge_result = merge_with_existing(
                raw_text, existing, space_config_dict,
                input_size, user_name=config.user_name,
                workspace_path=config.base_dir,
            )
        except Exception as e:
            console.print(f"[red]Merge failed: {e}[/red]")
            return

        has_changes = show_merge_preview(existing, merge_result)
        if not has_changes:
            console.print("[dim]No new information to merge.[/dim]")
            db.update_inbox_status(row["id"], "skipped")
            return

        try:
            merge_choice = Prompt.ask(
                r"\[Y]es, merge / \[e]dit / \[n]o, skip", default="Y",
            )
        except KeyboardInterrupt:
            console.print("\n[yellow]Cancelled.[/yellow]")
            return

        if merge_choice.lower() == "n":
            db.update_inbox_status(row["id"], "skipped")
            console.print("[yellow]Skipped.[/yellow]")
            return
        if merge_choice.lower() == "e":
            merge_result = edit_merge_result(existing, merge_result)
            show_merge_preview(existing, merge_result)

        apply_merge(config, db, existing, merge_result, raw_text, raw_text)
        db.update_inbox_status(row["id"], "filed", filed_note_id=existing.id)
        sync_todo_index(config, db)
        sync_ideas_index(config, db)
        console.print(f"[green]Merged into:[/green] {existing.file_path}")
        completer.invalidate_todos()
        return

    # --- Step 2: AI structure (folder chosen, now process) ---
    input_size = classify_input_size(raw_text)

    console.print("[dim]Structuring with AI...[/dim]")
    try:
        result = structure_only(
            raw_text, {}, input_size,
            user_name=config.user_name,
            workspace_path=config.base_dir,
        )
    except ListItemResult as e:
        _handle_list_result(config, db, e.data)
        db.update_inbox_status(row["id"], "filed")
        return
    except SnippetResult as e:
        _handle_snippet_result(config, db, e.data, working_folder)
        db.update_inbox_status(row["id"], "filed")
        return
    except Exception as e:
        console.print(f"[red]AI structuring failed: {e}[/red]")
        return

    # --- Step 3: Preview and save ---
    import uuid
    now = datetime.now(timezone.utc).isoformat()
    note_id = uuid.uuid4().hex[:8]
    note_date = row.get("created", now)[:10]
    rel_path = generate_file_path(
        config, routing.space, routing.group_slug, note_date,
        result.metadata.title,
    )

    note = Note(
        id=note_id,
        space=routing.space,
        title=result.metadata.title,
        source=row.get("source", "workflow"),
        refinement=Refinement.AI_STRUCTURED,
        input_size=input_size,
        date=note_date,
        created=now,
        updated=now,
        summary=result.metadata.summary,
        tags=result.metadata.tags,
        participants=result.metadata.participants,
        file_path=rel_path,
        body=result.body_markdown,
        raw_text=raw_text,
        action_items=result.metadata.action_items,
        source_url=row.get("source_url", ""),
    )

    # Preview
    preview_lines = [
        f"[bold]Title:[/bold]    {note.title}",
        f"[bold]Summary:[/bold]  {note.summary}",
    ]
    if note.tags:
        preview_lines.append(f"[bold]Tags:[/bold]     {', '.join(note.tags)}")
    if note.action_items:
        preview_lines.append(f"[bold]Actions:[/bold]  {len(note.action_items)} item(s)")
    if note.body:
        preview_lines.append("")
        for bl in note.body.strip().splitlines()[:15]:
            preview_lines.append(f"[dim]{bl}[/dim]")

    console.print(Panel(
        "\n".join(preview_lines),
        title="AI Structured",
        border_style="green",
    ))

    try:
        save_choice = Prompt.ask(
            r"\[Y]es, save / \[e]dit first / \[n]o, skip", default="Y",
        )
    except KeyboardInterrupt:
        console.print("\n[yellow]Cancelled.[/yellow]")
        return

    if save_choice.lower() == "n":
        db.update_inbox_status(row["id"], "skipped")
        console.print("[yellow]Skipped.[/yellow]")
        return

    if save_choice.lower() == "e":
        edited = edit_note_in_editor(note)
        if edited:
            note.file_path = generate_file_path(
                config, routing.space, routing.group_slug, note_date, note.title,
            )

    save_and_sync(config, db, note, routing=NoteRouting(
        space=routing.space,
        group_slug=routing.group_slug,
        group_display=routing.group_display or routing.group_slug,
    ))
    db.update_inbox_status(row["id"], "filed", filed_note_id=note_id)
    sync_todo_index(config, db)
    sync_ideas_index(config, db)
    console.print(f"[green]Filed:[/green] {note.file_path}")
    completer.invalidate_todos()


def _save_raw_inbox_item(
    config: NotelyConfig,
    db: Database,
    row: dict,
    tags: list[str],
    participants: list[str],
    action_items: list,
    working_folder: dict,
    completer: "_SlashCompleter",
) -> None:
    """Save a raw inbox item as a note without AI structuring."""
    from ...storage import (
        generate_file_path, save_and_sync, edit_note_in_editor,
        sync_todo_index, sync_ideas_index,
    )
    from ...models import Note, Refinement, InputSize

    # Route first
    search_text = row.get("title", "")
    if row.get("body"):
        search_text += f"\n\n{row['body'][:500]}"

    try:
        from ...vectors import get_vector_store
        vec = get_vector_store(config)
    except Exception:
        vec = None

    try:
        from ...routing import route_input
        routing = route_input(
            config, db, vec, search_text,
            folder_default=working_folder,
        )
    except KeyboardInterrupt:
        console.print("\n[yellow]Cancelled.[/yellow]")
        return

    if routing is None:
        db.update_inbox_status(row["id"], "skipped")
        console.print("[yellow]Skipped.[/yellow]")
        return

    if routing.append_to_note:
        console.print("[yellow]Cannot merge raw content — use \\[p]rocess first.[/yellow]")
        return

    import uuid
    now = datetime.now(timezone.utc).isoformat()
    note_id = uuid.uuid4().hex[:8]
    note_date = row.get("created", now)[:10]
    title = row.get("title", "").strip() or "(untitled)"

    rel_path = generate_file_path(
        config, routing.space, routing.group_slug, note_date, title,
    )

    note = Note(
        id=note_id,
        space=routing.space,
        title=title,
        source=row.get("source", "workflow"),
        refinement=Refinement.RAW,
        input_size=InputSize.MEDIUM,
        date=note_date,
        created=now,
        updated=now,
        summary="",
        tags=tags,
        participants=participants,
        file_path=rel_path,
        body=row.get("body", ""),
        raw_text=row.get("body", ""),
        action_items=action_items,
        source_url=row.get("source_url", ""),
    )

    try:
        save_choice = Prompt.ask(
            r"\[Y]es, save / \[e]dit first / \[n]o, skip", default="Y",
        )
    except KeyboardInterrupt:
        console.print("\n[yellow]Cancelled.[/yellow]")
        return

    if save_choice.lower() == "n":
        db.update_inbox_status(row["id"], "skipped")
        console.print("[yellow]Skipped.[/yellow]")
        return

    if save_choice.lower() == "e":
        edited = edit_note_in_editor(note)
        if edited:
            note.file_path = generate_file_path(
                config, routing.space, routing.group_slug, note_date, note.title,
            )

    save_and_sync(config, db, note, routing=NoteRouting(
        space=routing.space,
        group_slug=routing.group_slug,
        group_display=routing.group_display or routing.group_slug,
    ))
    db.update_inbox_status(row["id"], "filed", filed_note_id=note_id)
    sync_todo_index(config, db)
    sync_ideas_index(config, db)
    console.print(f"[green]Filed:[/green] {note.file_path}")
    completer.invalidate_todos()
