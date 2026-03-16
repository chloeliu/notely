"""notely dump — main command for creating notes from raw input."""

from __future__ import annotations

import sys
import uuid
from datetime import datetime, timezone

import click
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt

from ..config import NotelyConfig
from ..db import Database
from ..models import InputSize, Note, Refinement
from ..prompts import confirm_action, no_changes_retry
from ..storage import (
    apply_merge,
    classify_input_size,
    edit_merge_result,
    generate_file_path,
    preview_and_save_records,
    save_and_sync,
    show_merge_preview,
    sync_ideas_index,
    sync_todo_index,
    write_note,
)

console = Console()


def _read_input(file_path: str | None) -> tuple[str, "Path | None"]:
    """Read raw text from file, stdin pipe, or interactive prompt.

    Returns (text, attachment_path). attachment_path is set for binary files
    (PDF, images) that should be copied as attachments.
    """
    if file_path:
        from pathlib import Path

        from ..files import IMAGE_EXTENSIONS, PDF_EXTENSIONS, extract_text
        p = Path(file_path)
        if p.suffix.lower() in (PDF_EXTENSIONS | IMAGE_EXTENSIONS):
            text, file_type = extract_text(p)
            return text, p.resolve()
        return p.read_text(encoding="utf-8"), None

    if not sys.stdin.isatty():
        text = sys.stdin.read()
        # Reopen tty for interactive prompts after reading piped input
        old_stdin = sys.stdin
        try:
            sys.stdin = open("/dev/tty", "r")
        except OSError:
            pass
        else:
            old_stdin.close()
        return text, None

    console.print("[dim]Paste or type your note. Press Ctrl+D (or Ctrl+Z on Windows) when done:[/dim]")
    lines = []
    try:
        while True:
            line = input()
            lines.append(line)
    except EOFError:
        pass
    return "\n".join(lines), None


@click.command("dump")
@click.option("--space", "-s", type=str, default=None, help="Target space (e.g., clients, ideas)")
@click.option("--client", type=str, default=None, help="Client slug (for clients space)")
@click.option("--topic", type=str, default=None, help="Topic slug (for clients space)")
@click.option("--category", type=str, default=None, help="Category slug (for ideas space)")
@click.option("--source", type=str, default=None, help="Source type (meeting, slack, podcast, etc.)")
@click.option("--title", "-t", type=str, default=None, help="Note title")
@click.option("--file", "-f", "file_path", type=str, default=None, help="Read input from file")
@click.option("--no-ai", is_flag=True, default=False, help="Skip AI structuring, create raw note")
@click.option("--yes", "-y", is_flag=True, default=False, help="Skip confirmation prompt")
@click.pass_context
def dump_cmd(
    ctx: click.Context,
    space: str | None,
    client: str | None,
    topic: str | None,
    category: str | None,
    source: str | None,
    title: str | None,
    file_path: str | None,
    no_ai: bool,
    yes: bool,
) -> None:
    """Capture raw text and structure it into a note."""
    config: NotelyConfig = ctx.obj["config"]

    config.ensure_initialized()

    # Read input
    raw_text, attachment_path = _read_input(file_path)
    if not raw_text.strip():
        console.print("[red]No input provided.[/red]")
        raise SystemExit(1)

    input_size = classify_input_size(raw_text)

    if no_ai:
        _create_raw_note(config, raw_text, input_size, space, client, topic,
                         category, source, title, auto_confirm=yes,
                         attachment_path=attachment_path)
    else:
        _create_ai_note(config, raw_text, input_size, space, client, topic,
                        category, source, title, auto_confirm=yes,
                        attachment_path=attachment_path)


def _create_raw_note(
    config: NotelyConfig,
    raw_text: str,
    input_size: InputSize,
    space: str | None,
    client: str | None,
    topic: str | None,
    category: str | None,
    source: str | None,
    title: str | None,
    auto_confirm: bool = False,
    attachment_path: "Path | None" = None,
) -> None:
    """Create a note without AI — minimal processing."""
    # Require space for --no-ai
    if not space:
        space = Prompt.ask(
            "Space", choices=config.space_names(), default=config.space_names()[0]
        )

    space_config = config.get_space(space)
    if not space_config:
        console.print(f"[red]Unknown space: {space}[/red]")
        raise SystemExit(1)

    # Get group
    group_by = space_config.group_by
    group = client or category
    if not group:
        group = Prompt.ask(f"{group_by.title()}")
    if not group:
        console.print(f"[red]{group_by} is required.[/red]")
        raise SystemExit(1)

    from slugify import slugify
    group_slug = slugify(group)

    # Get subgroup if needed
    subgroup_slug = None
    if space_config.subgroup_by:
        subgroup = topic
        if not subgroup:
            subgroup = Prompt.ask(f"{space_config.subgroup_by.title()} (optional)", default="")
        if subgroup:
            subgroup_slug = slugify(subgroup)

    # Title
    if not title:
        first_line = raw_text.strip().split("\n")[0][:80]
        title = Prompt.ask("Title", default=first_line)

    now = datetime.now(timezone.utc).isoformat()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    note_id = uuid.uuid4().hex[:8]

    # Build first line as summary
    summary = raw_text.strip().split("\n")[0][:200]

    # Space metadata
    space_metadata: dict = {}
    if space == "clients" or group_by == "client":
        space_metadata["client"] = group_slug
        space_metadata["client_display"] = group
        if subgroup_slug:
            space_metadata["topic"] = subgroup_slug
            space_metadata["topic_display"] = topic or subgroup_slug
    elif space == "ideas" or group_by == "category":
        space_metadata["category"] = group_slug
        space_metadata["category_display"] = group
        space_metadata["content_status"] = "seed"

    rel_path = generate_file_path(config, space, group_slug, today, title, subgroup_slug)

    note = Note(
        id=note_id,
        space=space,
        title=title,
        source=source or "manual",
        refinement=Refinement.RAW,
        input_size=input_size,
        date=today,
        created=now,
        updated=now,
        summary=summary,
        tags=[],
        participants=[],
        file_path=rel_path,
        body=raw_text.strip(),
        raw_text=raw_text,
        related_contexts=[],
        space_metadata=space_metadata,
    )

    # Show preview and confirm
    if not auto_confirm:
        def _raw_edit():
            from ..storage import edit_note_in_editor
            edited = edit_note_in_editor(note)
            if edited:
                note.file_path = generate_file_path(
                    config, space, group_slug, today, note.title, subgroup_slug
                )
            else:
                console.print("[dim]No changes.[/dim]")

        if not confirm_action(
            lambda: _show_preview(note), verb="save",
            edit_fn=_raw_edit, console=console,
        ):
            return
    else:
        _show_preview(note)

    # Copy attachment if present
    if attachment_path:
        from ..files import copy_attachment
        rel = copy_attachment(attachment_path, config, space, group_slug, subgroup_slug)
        note.attachments.append(rel)
        console.print(f"[dim]Attached: {rel}[/dim]")

    # Save via shared pipeline
    with Database(config.db_path) as db:
        db.initialize()
        save_and_sync(config, db, note, source_file=attachment_path)

    console.print(f"\n[green]Saved:[/green] {note.file_path}")
    console.print(f"[dim]ID: {note.id}[/dim]")


def _create_ai_note(
    config: NotelyConfig,
    raw_text: str,
    input_size: InputSize,
    space: str | None,
    client: str | None,
    topic: str | None,
    category: str | None,
    source: str | None,
    title: str | None,
    auto_confirm: bool = False,
    attachment_path: "Path | None" = None,
) -> None:
    """Create a note or list item with AI structuring + vector routing."""
    from ..ai import (
        ListItemResult,
        RecordsOnlyResult,
        SnippetResult,
        mask_secrets,
        merge_with_existing,
        structure_only,
        unmask_secrets,
    )
    from ..models import AIStructuredOutput, NoteRouting
    from ..routing import ensure_directory_indexed, route_input
    from ..vectors import get_vector_store, try_vector_sync_note

    with Database(config.db_path) as db:
        db.initialize()

        # Hints from CLI flags
        hints: dict = {}
        if space:
            hints["space"] = space
        if client:
            hints["client"] = client
        if topic:
            hints["topic"] = topic
        if category:
            hints["category"] = category
        if source:
            hints["source"] = source
        if title:
            hints["title"] = title

        # Route via vector search
        vec_store = get_vector_store(config)
        routing = route_input(config, db, vec_store, raw_text, hints=hints)

        if routing is None:
            console.print("[yellow]Cancelled.[/yellow]")
            return

        # Mask secrets before sending to AI
        masked_text, secret_mapping = mask_secrets(raw_text)

        # Auto-capture secrets to .secrets.toml
        if secret_mapping:
            from ..secrets import SecretsStore
            SecretsStore(config.secrets_path).store_mapping(secret_mapping)

        # Build space config for the AI prompt
        space_cfg = config.get_space(routing.space)
        space_config_dict = {}
        if space_cfg:
            space_config_dict = {
                "space": routing.space,
                "description": space_cfg.description,
                "group_by": space_cfg.group_by,
                "subgroup_by": space_cfg.subgroup_by,
                "fields": space_cfg.fields,
            }

        # Handle append
        if routing.append_to_note:
            from ..storage import read_note
            from ..storage import write_note as do_write
            existing_db = db.get_note(routing.append_to_note)
            if existing_db:
                existing = read_note(config, existing_db["file_path"])
                if existing:
                    merge_input = masked_text
                    existing_actions = db.get_note_todos(existing.id)

                    while True:
                        console.print(f"[dim]Merging with '{existing.title}'...[/dim]")
                        try:
                            merge_result = merge_with_existing(
                                merge_input, existing,
                                space_config_dict, input_size,
                                user_name=config.user_name,
                                workspace_path=config.base_dir,
                                action_items=existing_actions,
                            )
                        except Exception as e:
                            console.print(f"[red]AI merge failed: {e}[/red]")
                            return

                        # Unmask secrets
                        if secret_mapping:
                            merge_result["updated_body"] = unmask_secrets(merge_result["updated_body"], secret_mapping)
                            merge_result["updated_summary"] = unmask_secrets(merge_result["updated_summary"], secret_mapping)
                            for item in merge_result.get("new_action_items", []):
                                item.task = unmask_secrets(item.task, secret_mapping)
                            for rec in merge_result.get("new_extracted_records", []):
                                if rec.get("value"):
                                    rec["value"] = unmask_secrets(rec["value"], secret_mapping)
                                if rec.get("entity"):
                                    rec["entity"] = unmask_secrets(rec["entity"], secret_mapping)

                        if not auto_confirm:
                            has_changes = show_merge_preview(existing, merge_result, display=False)
                            if not has_changes:
                                retry = no_changes_retry(console=console)
                                if retry == "d":
                                    hint = Prompt.ask("[dim]What should be updated?[/dim]")
                                    if hint.strip():
                                        merge_input = f"WHAT'S NEW: {hint.strip()}\n\n{masked_text}"
                                        continue
                                return
                        break

                    if not auto_confirm:
                        def _dump_merge_preview():
                            show_merge_preview(existing, merge_result)

                        def _dump_merge_edit():
                            nonlocal merge_result
                            merge_result = edit_merge_result(existing, merge_result)

                        def _dump_merge_revise():
                            nonlocal merge_result
                            instruction = Prompt.ask("[dim]What should AI change?[/dim]")
                            if instruction.strip():
                                console.print("[dim]Revising merge...[/dim]")
                                m_input = f"WHAT'S NEW: {instruction.strip()}\n\n{masked_text}"
                                try:
                                    merge_result = merge_with_existing(
                                        m_input, existing,
                                        space_config_dict, input_size,
                                        user_name=config.user_name,
                                        workspace_path=config.base_dir,
                                        action_items=existing_actions,
                                    )
                                    if secret_mapping:
                                        merge_result["updated_body"] = unmask_secrets(merge_result["updated_body"], secret_mapping)
                                        merge_result["updated_summary"] = unmask_secrets(merge_result["updated_summary"], secret_mapping)
                                        for item in merge_result.get("new_action_items", []):
                                            item.task = unmask_secrets(item.task, secret_mapping)
                                        for rec in merge_result.get("new_extracted_records", []):
                                            if rec.get("value"):
                                                rec["value"] = unmask_secrets(rec["value"], secret_mapping)
                                            if rec.get("entity"):
                                                rec["entity"] = unmask_secrets(rec["entity"], secret_mapping)
                                except Exception as e:
                                    console.print(f"[red]AI revision failed: {e}[/red]")

                        if not confirm_action(
                            _dump_merge_preview, verb="merge",
                            edit_fn=_dump_merge_edit, revise_fn=_dump_merge_revise,
                            console=console,
                        ):
                            return

                    new_actions, new_records = apply_merge(config, db, existing, merge_result, raw_text)
                    try_vector_sync_note(config, existing)
                    sync_todo_index(config, db)
                    sync_ideas_index(config, db)
                    console.print(f"\n[green]Merged into:[/green] {existing.file_path}")
                    console.print(f"[dim]ID: {existing.id}[/dim]")

                    # Step 2: confirm and save extracted records separately
                    if new_actions or new_records:
                        preview_and_save_records(
                            config, db, existing,
                            action_items=new_actions or None,
                            extracted_records=new_records or None,
                            console=console,
                            auto_confirm=auto_confirm,
                        )
                    else:
                        console.print("[dim]No new action items or records extracted.[/dim]")
                    return

        # New note — structure with AI
        console.print("[dim]Structuring with AI...[/dim]")

        try:
            result = structure_only(
                masked_text, space_config_dict, input_size,
                user_name=config.user_name,
                workspace_path=config.base_dir,
            )
        except ListItemResult as e:
            list_data = e.data
            if secret_mapping:
                for item in list_data.get("items", []):
                    item["text"] = unmask_secrets(item["text"], secret_mapping)
                    if item.get("summary"):
                        item["summary"] = unmask_secrets(item["summary"], secret_mapping)
            _handle_list_items(config, db, list_data, auto_confirm)
            return
        except SnippetResult as sr:
            snippet_data = sr.data
            if secret_mapping:
                _handle_secret_snippets(config, snippet_data, secret_mapping)
            else:
                _handle_snippet_result(config, db, snippet_data)
            return
        except RecordsOnlyResult as e:
            from ..models import ActionItem
            raw_records = e.data.get("extracted_records", [])
            if secret_mapping:
                for rec in raw_records:
                    if rec.get("value"):
                        rec["value"] = unmask_secrets(rec["value"], secret_mapping)
                    if rec.get("entity"):
                        rec["entity"] = unmask_secrets(rec["entity"], secret_mapping)
            items = []
            records = []
            for rec in raw_records:
                if rec.get("snippet_type") == "todo":
                    items.append(ActionItem(
                        owner=rec.get("owner", "me"),
                        task=rec["entity"],
                        due=rec.get("due"),
                    ))
                else:
                    records.append(rec)
            if items or records:
                from ..models import Note
                placeholder = Note(id="", space="", file_path="")
                from ..storage import preview_and_save_records
                preview_and_save_records(
                    config, db, placeholder,
                    action_items=items or None,
                    extracted_records=records or None,
                    auto_confirm=auto_confirm,
                )
            else:
                console.print("[dim]No records found to extract.[/dim]")
            return
        except Exception as e:
            console.print(f"[red]AI structuring failed: {e}[/red]")
            console.print("[yellow]Falling back to --no-ai mode.[/yellow]")
            _create_raw_note(config, raw_text, input_size, space, client, topic,
                             category, source, title, auto_confirm=auto_confirm)
            return

        # Unmask secrets in AI-structured output
        if secret_mapping:
            result.body_markdown = unmask_secrets(result.body_markdown, secret_mapping)
            result.metadata.summary = unmask_secrets(result.metadata.summary, secret_mapping)
            for item in result.metadata.action_items:
                item.task = unmask_secrets(item.task, secret_mapping)
            for rec in result.extracted_records:
                if rec.get("value"):
                    rec["value"] = unmask_secrets(rec["value"], secret_mapping)
                if rec.get("entity"):
                    rec["entity"] = unmask_secrets(rec["entity"], secret_mapping)

        meta = result.metadata

        # Override source/title from CLI hints if provided
        if source:
            meta.source = source
        if title:
            meta.title = title

        now = datetime.now(timezone.utc).isoformat()
        note_id = uuid.uuid4().hex[:8]

        # Build space_metadata
        from ..storage import build_space_metadata
        space_metadata = build_space_metadata(
            config, routing.space, routing.group_slug, routing.group_display,
            routing.subgroup_slug, routing.subgroup_display, extra=meta.extra,
        )

        rel_path = generate_file_path(
            config, routing.space, routing.group_slug, meta.date,
            meta.title, routing.subgroup_slug,
        )

        note = Note(
            id=note_id,
            space=routing.space,
            title=meta.title,
            source=meta.source,
            refinement=Refinement.AI_STRUCTURED,
            input_size=input_size,
            date=meta.date,
            created=now,
            updated=now,
            summary=meta.summary,
            tags=meta.tags,
            participants=meta.participants,
            file_path=rel_path,
            body=result.body_markdown,
            raw_text=raw_text,
            related_contexts=result.related_contexts,
            space_metadata=space_metadata,
        )

        # Action items stored separately — will go to DB via save_and_sync
        dump_action_items = list(meta.action_items)
        dump_extracted_records = list(result.extracted_records)

        # Show preview and confirm
        if routing.group_is_new and space_cfg:
            console.print(f"  [yellow]NEW {space_cfg.group_by}: {routing.group_display} ({routing.group_slug})[/yellow]")
        if routing.subgroup_is_new and routing.subgroup_slug and space_cfg:
            console.print(f"  [yellow]NEW {space_cfg.subgroup_by}: {routing.subgroup_display} ({routing.subgroup_slug})[/yellow]")

        if not auto_confirm:
            def _dump_note_edit():
                from ..storage import edit_note_in_editor
                edited = edit_note_in_editor(note)
                if edited:
                    note.file_path = generate_file_path(
                        config, routing.space, routing.group_slug, meta.date,
                        note.title, routing.subgroup_slug,
                    )
                else:
                    console.print("[dim]No changes.[/dim]")

            def _dump_note_revise():
                nonlocal dump_action_items
                instruction = Prompt.ask("[dim]What should AI change?[/dim]")
                if instruction.strip():
                    console.print("[dim]Revising...[/dim]")
                    try:
                        from ..ai import revise_note
                        revised = revise_note(
                            note, instruction.strip(), space_config_dict, input_size,
                            user_name=config.user_name,
                            action_items=dump_action_items,
                        )
                        note.title = revised.metadata.title
                        note.summary = revised.metadata.summary
                        note.tags = revised.metadata.tags
                        note.participants = revised.metadata.participants
                        dump_action_items = list(revised.metadata.action_items)
                        note.body = revised.body_markdown
                        note.refinement = Refinement.HUMAN_REVIEWED
                        note.file_path = generate_file_path(
                            config, routing.space, routing.group_slug,
                            revised.metadata.date, note.title, routing.subgroup_slug,
                        )
                    except Exception as e:
                        console.print(f"[red]AI revision failed: {e}[/red]")

            if not confirm_action(
                lambda: _show_preview(note), verb="save",
                edit_fn=_dump_note_edit, revise_fn=_dump_note_revise,
                console=console,
            ):
                return
        else:
            _show_preview(note)

        # Copy attachment if present
        if attachment_path:
            from ..files import copy_attachment
            rel = copy_attachment(
                attachment_path, config, routing.space, routing.group_slug,
                routing.subgroup_slug,
            )
            note.attachments.append(rel)
            console.print(f"[dim]Attached: {rel}[/dim]")

        # Save note via shared pipeline (records confirmed separately)
        save_and_sync(config, db, note, routing=routing, source_file=attachment_path)

        console.print(f"\n[green]Saved:[/green] {note.file_path}")
        console.print(f"[dim]ID: {note.id}[/dim]")

        # Step 2: confirm and save extracted records separately
        if dump_action_items or dump_extracted_records:
            preview_and_save_records(
                config, db, note,
                action_items=dump_action_items or None,
                extracted_records=dump_extracted_records or None,
                console=console,
                auto_confirm=auto_confirm,
            )


def _handle_list_items(
    config: NotelyConfig,
    db: Database,
    result: dict,
    auto_confirm: bool,
) -> None:
    """Handle AI choosing to add list items instead of a full note."""
    from ..storage import confirm_and_save_list_items
    confirm_and_save_list_items(config, db, result, auto_confirm=auto_confirm)


def _handle_secret_snippets(config, data, secret_mapping):
    """Route |||...|||-marked snippets to .secrets.toml using AI's entity/key naming."""
    from rich.panel import Panel
    from rich.prompt import Prompt

    from ..ai import unmask_secrets

    console = Console()
    items = data.get("items", [])
    if not items:
        console.print("[yellow]AI returned no items.[/yellow]")
        return

    # Preview with masked values
    lines = [f"[bold]Saving {len(items)} secret(s) to .secrets.toml:[/bold]", ""]
    for i, item in enumerate(items, 1):
        service = item.get("entity", "auto")
        key = item.get("key", "secret")
        desc = f" — {item['description']}" if item.get("description") else ""
        lines.append(f"  {i}. [cyan]{service}[/cyan].{key} = ********[dim]{desc}[/dim]")
    console.print(Panel("\n".join(lines), title="Secrets", border_style="green"))

    def _secrets_preview():
        pass  # already shown above

    if not confirm_action(_secrets_preview, verb="save"):
        console.print("[yellow]Cancelled.[/yellow]")
        return

    from ..secrets import SecretsStore
    store = SecretsStore(config.secrets_path)
    for item in items:
        real_value = unmask_secrets(item.get("value", ""), secret_mapping)
        store.store(item.get("entity", "auto"), item.get("key", "secret"), real_value)

    console.print(f"[green]Saved {len(items)} secret(s) to .secrets.toml[/green]")


def _handle_snippet_result(
    config: NotelyConfig,
    db: Database,
    data: dict,
) -> None:
    """Handle AI choosing to save snippets instead of a full note."""
    from ..storage import confirm_and_save_snippets
    confirm_and_save_snippets(config, db, data)


def _show_preview(note: Note) -> None:
    """Show a preview panel of the note before saving."""
    lines = [
        f"[bold]Space:[/bold]    {note.space}",
        f"[bold]Title:[/bold]    {note.title}",
        f"[bold]Date:[/bold]     {note.date}",
        f"[bold]Source:[/bold]   {note.source}",
    ]

    # Space-specific fields
    for key in ("client", "client_display", "topic", "topic_display",
                "category", "category_display", "content_status"):
        val = note.space_metadata.get(key)
        if val and not key.endswith("_display"):
            display_key = f"{key}_display"
            display = note.space_metadata.get(display_key, val)
            lines.append(f"[bold]{key.title()}:[/bold]  {display} ({val})")

    if note.tags:
        lines.append(f"[bold]Tags:[/bold]     {', '.join(note.tags)}")
    if note.participants:
        lines.append(f"[bold]People:[/bold]   {', '.join(note.participants)}")
    lines.append(f"[bold]Summary:[/bold]  {note.summary}")

    console.print(Panel("\n".join(lines), title="Note Preview", border_style="blue"))
