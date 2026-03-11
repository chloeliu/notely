"""Input processing — paste handling, AI structuring, note/snippet/list handling, web clip."""

from __future__ import annotations

import re
import sys
from datetime import datetime, timezone

from rich.panel import Panel
from rich.prompt import Prompt

from ...config import NotelyConfig
from ...db import Database
from ...models import NoteRouting
from ...prompts import confirm_action
from ...storage import classify_input_size, show_merge_preview, edit_merge_result, apply_merge, preview_and_save_records

from ._shared import console, _fuzzy_match_folder


class _TypedInput(str):
    """String subclass that carries typed_context and paste_content alongside the full text."""

    def __new__(cls, text: str, typed_context: str | None = None, paste_content: str | None = None):
        obj = super().__new__(cls, text)
        obj.typed_context = typed_context
        obj.paste_content = paste_content
        return obj


def _read_block(completer=None, prompt: str = "notely-notetaker> ") -> str:
    """Read input. Enter for new lines, Enter on empty line to submit.

    Pasted text is detected via bracket paste mode and shown as
    '[pasted N lines]' — press Enter to submit.
    """
    from prompt_toolkit import PromptSession
    from prompt_toolkit.document import Document
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.keys import Keys

    bindings = KeyBindings()
    paste_content: list[str | None] = [None]  # actual pasted text
    paste_marker: list[str] = [""]  # the "[pasted N lines]" display string

    @bindings.add(Keys.BracketedPaste, eager=True)
    def handle_paste(event):
        data = event.data
        buf = event.current_buffer
        typed = buf.text

        # Accumulate paste content (handles multiple pastes)
        if paste_content[0]:
            paste_content[0] += data
        else:
            paste_content[0] = data

        total_lines = len(paste_content[0].splitlines()) or 1
        new_marker = f'[pasted {total_lines} lines]'

        # Replace old marker if present, otherwise append
        if paste_marker[0] and paste_marker[0] in typed:
            display = typed.replace(paste_marker[0], new_marker)
        elif typed.strip():
            display = typed + new_marker
        else:
            display = new_marker

        paste_marker[0] = new_marker
        buf.document = Document(display, len(display))

    @bindings.add('enter', eager=True)
    def handle_enter(event):
        buf = event.current_buffer
        text = buf.text
        current_line = buf.document.current_line

        # Slash commands submit immediately
        if text.strip().startswith('/') and '\n' not in text:
            buf.validate_and_handle()
        # Empty line with content above — submit
        elif current_line.strip() == '' and text.strip():
            stripped = text.rstrip('\n')
            buf.document = Document(stripped, len(stripped))
            buf.validate_and_handle()
        else:
            buf.insert_text('\n')

    from prompt_toolkit.filters import Condition
    from prompt_toolkit.document import Document as _Doc

    @Condition
    def _should_complete():
        """Only auto-complete when typing slash commands or @notes."""
        app = session.app
        if app and app.current_buffer:
            text = app.current_buffer.text
            return text.lstrip().startswith("/") or "@" in text
        return False

    session = PromptSession()
    display_text = session.prompt(prompt, multiline=True, key_bindings=bindings,
                                  prompt_continuation='. ', completer=completer,
                                  complete_while_typing=_should_complete)

    # Replace paste marker with actual paste content
    if paste_content[0] is not None and paste_marker[0]:
        # Extract typed context (everything the user typed around the paste)
        typed_context = display_text.replace(paste_marker[0], "").strip()
        pasted = paste_content[0]
        result = display_text.replace(paste_marker[0], pasted)
        paste_content[0] = None
        paste_marker[0] = ""
        # Stash typed context and paste content so _process_input can use them
        result = _TypedInput(result, typed_context if typed_context else None, paste_content=pasted)
        return result
    return display_text


def _extract_typed_context(raw_text: str) -> str | None:
    """Extract user-typed context from a _TypedInput, or None for plain text."""
    if isinstance(raw_text, _TypedInput):
        return raw_text.typed_context
    return None


# Patterns that indicate user instructions at the start of pasted text
_INSTRUCTION_PATTERNS = re.compile(
    r"^.*?\b("
    r"i only want|only want to|just capture|just record|focus on|extract only|"
    r"only record|only extract|i want to record|i want to capture|"
    r"don'?t include|skip the|ignore the|"
    r"i only need|only need to|just need"
    r")\b",
    re.IGNORECASE,
)


def _extract_inline_instruction(text: str) -> tuple[str | None, str]:
    """Detect user instructions at the start of pasted text.

    Users sometimes paste their instruction + content together. The instruction
    is typically the first 1-4 lines before the actual source material (Slack
    thread, meeting notes, etc.) begins.

    Heuristic: scan the first few lines for instruction-like phrases. If found,
    everything up to and including that "instruction block" is extracted. The
    boundary is detected by looking for a blank line or a line that looks like
    source content (timestamp, username pattern, etc.).

    Returns:
        (instruction, remaining_text) if instruction found, else (None, text).
    """
    lines = text.split("\n")
    if len(lines) < 3:
        return None, text

    # Check the first 5 lines for instruction patterns
    instruction_end = None
    for i, line in enumerate(lines[:5]):
        if _INSTRUCTION_PATTERNS.search(line):
            # Found instruction — now find where it ends.
            # Instructions typically end at a blank line or when the source
            # material begins (different style: timestamps, usernames, etc.)
            instruction_end = i
            # Extend through continuation lines (same casual style)
            for j in range(i + 1, min(i + 4, len(lines))):
                stripped = lines[j].strip()
                if not stripped:
                    # Blank line = boundary
                    instruction_end = j - 1
                    break
                # If line looks like source content (has timestamps, @mentions,
                # or starts with a username pattern), stop
                if re.match(r"^[\w\s]+\d{1,2}:\d{2}|^@|^\w+\s+\d+[ap]m|^[\w\s]+wrote:", stripped, re.IGNORECASE):
                    instruction_end = j - 1
                    break
                instruction_end = j
            break

    if instruction_end is None:
        return None, text

    instruction_lines = lines[:instruction_end + 1]
    remaining_lines = lines[instruction_end + 1:]

    instruction = "\n".join(instruction_lines).strip()
    remaining = "\n".join(remaining_lines).strip()

    if not instruction or not remaining:
        return None, text

    return instruction, remaining


def _process_input(config: NotelyConfig, raw_text: str, folder_default: dict | None = None) -> None:
    """Process raw text: route via vectors, then structure with AI."""
    from ...ai import (
        structure_only, merge_with_existing, mask_secrets, unmask_secrets,
        ListItemResult, SnippetResult, RecordsOnlyResult,
    )
    from ...files import is_file_path, extract_text, copy_attachment
    from ...models import AIStructuredOutput, InputSize
    from ...routing import route_input, ensure_directory_indexed, extract_context
    from ...storage import sync_todo_index, sync_ideas_index
    from ...vectors import get_vector_store, try_vector_sync_note

    # Check if input is a file path
    attachment_path = None
    file_path = is_file_path(raw_text.strip())
    if file_path:
        if file_path.suffix.lower() in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"):
            console.print(f"[dim]Describing image...[/dim]")
        extracted_text, file_type = extract_text(file_path)
        if file_type in ("text", "pdf"):
            console.print(f"[dim]Detected {file_type} file: {file_path.name}[/dim]")
            attachment_path = file_path
            raw_text = extracted_text
        elif file_type == "image":
            attachment_path = file_path
            if extracted_text.startswith("[Image:"):
                # Vision failed or unavailable — placeholder
                console.print(f"[dim]Image file: {file_path.name} (attached, no text to structure)[/dim]")
                raw_text = f"Attachment: {file_path.name}"
            else:
                # Vision succeeded — use description as input
                console.print(f"[dim]Image described: {file_path.name}[/dim]")
                raw_text = extracted_text

    input_size = classify_input_size(raw_text)
    user_context = _extract_typed_context(raw_text)

    db = Database(config.db_path)
    db.initialize()

    try:
        # Step 1: Extract typed context and paste content
        paste_content = None
        if isinstance(raw_text, _TypedInput) and raw_text.paste_content:
            paste_content = raw_text.paste_content

        # Drain leftover stdin bytes from paste to prevent auto-accepting prompts.
        # prompt-toolkit uses raw terminal mode; after it exits, leftover bytes
        # (newlines, escape sequences) can cause Rich's Prompt.ask to auto-accept.
        # We drain with a brief timeout to catch bytes still in transit.
        import os
        import select
        try:
            fd = sys.stdin.fileno()
            while select.select([fd], [], [], 0.05)[0]:
                os.read(fd, 4096)
        except (OSError, ValueError):
            pass

        # Step 3: Route via vector search
        vec_store = get_vector_store(config)
        routing = route_input(config, db, vec_store, raw_text, user_context=user_context,
                              folder_default=folder_default)

        if routing is None:
            console.print("[yellow]Cancelled.[/yellow]")
            return

        # Step 4: Mask secrets before sending to AI
        # Always scan full raw_text for |||...|||  markers — the markers may be
        # in the typed context (user types ||| around a paste), not in paste_content.
        _, secret_mapping = mask_secrets(str(raw_text))

        # Auto-capture secrets to .secrets.toml
        if secret_mapping:
            from ...secrets import SecretsStore
            SecretsStore(config.secrets_path).store_mapping(secret_mapping)

        # Build the text for the AI — mask secrets in whichever portion we send
        ai_text = paste_content if (user_context and paste_content) else str(raw_text)
        masked_text = ai_text
        if secret_mapping:
            for placeholder, value in secret_mapping.items():
                masked_text = masked_text.replace(value, placeholder)

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

        # Step 5: Records-only mode — skip note merge/create, just extract records
        if routing.records_only:
            # Load existing note
            existing_note = None
            if routing.existing_note_id:
                from ...storage import read_note
                existing_db_row = db.get_note(routing.existing_note_id)
                if existing_db_row:
                    existing_note = read_note(config, existing_db_row["file_path"])

            if not existing_note:
                console.print("[yellow]Could not find the existing note to link records.[/yellow]")
                return

            # Load records already linked to this note so AI avoids re-extracting
            already_extracted = db.get_note_records(existing_note.id)

            console.print("[dim]Extracting records...[/dim]")
            try:
                re_result = structure_only(
                    masked_text, space_config_dict, input_size,
                    user_name=config.user_name,
                    user_instruction=user_context,
                    workspace_path=config.base_dir,
                    existing_records=already_extracted,
                )
                re_items = list(re_result.metadata.action_items)
                re_records = list(re_result.extracted_records)
                if secret_mapping:
                    for item in re_items:
                        item.task = unmask_secrets(item.task, secret_mapping)
                    for rec in re_records:
                        if rec.get("value"):
                            rec["value"] = unmask_secrets(rec["value"], secret_mapping)
                        if rec.get("entity"):
                            rec["entity"] = unmask_secrets(rec["entity"], secret_mapping)
                if re_items or re_records:
                    preview_and_save_records(
                        config, db, existing_note,
                        action_items=re_items or None,
                        extracted_records=re_records or None,
                        console=console,
                    )
                else:
                    console.print("[dim]No records found to extract.[/dim]")
            except Exception as e:
                console.print(f"[red]AI extraction failed: {e}[/red]")
            return

        # Step 6: AI structuring
        if routing.append_to_note:
            # Merge with existing note
            from ...storage import read_note
            existing_db = db.get_note(routing.append_to_note)
            if existing_db:
                existing = read_note(config, existing_db["file_path"])
                if existing:
                    merge_input = masked_text

                    # If user just pasted without typing context, ask what's new
                    if not user_context:
                        hint = Prompt.ask(
                            "[dim]What's new in this update? (Enter to let AI figure it out)[/dim]",
                            default="",
                        )
                        if hint.strip():
                            merge_input = f"WHAT'S NEW: {hint.strip()}\n\n{masked_text}"

                    # Load existing todos from DB for merge context
                    existing_actions = db.get_note_todos(existing.id)

                    console.print(f"[dim]Merging with '{existing.title}'...[/dim]")
                    try:
                        _mr_result = merge_with_existing(
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
                        _mr_result["updated_body"] = unmask_secrets(_mr_result["updated_body"], secret_mapping)
                        _mr_result["updated_summary"] = unmask_secrets(_mr_result["updated_summary"], secret_mapping)
                        for item in _mr_result.get("new_action_items", []):
                            item.task = unmask_secrets(item.task, secret_mapping)
                        for rec in _mr_result.get("new_extracted_records", []):
                            if rec.get("value"):
                                rec["value"] = unmask_secrets(rec["value"], secret_mapping)
                            if rec.get("entity"):
                                rec["entity"] = unmask_secrets(rec["entity"], secret_mapping)

                    # Check for changes
                    has_changes = show_merge_preview(existing, _mr_result, display=False)

                    if not has_changes:
                        # No note-level changes, but check if records were never saved.
                        # This happens when a user saves a note, skips the records step,
                        # then re-pastes. The note content matches but todos are missing.
                        saved_todos = db.get_note_todos(existing.id)
                        if not saved_todos:
                            console.print("[dim]No new note content, but checking for unsaved records...[/dim]")
                            console.print("[dim]Re-structuring to extract records...[/dim]")
                            try:
                                re_result = structure_only(
                                    masked_text, space_config_dict, input_size,
                                    user_name=config.user_name,
                                    workspace_path=config.base_dir,
                                )
                                re_items = list(re_result.metadata.action_items)
                                re_records = list(re_result.extracted_records)
                                if re_items or re_records:
                                    preview_and_save_records(
                                        config, db, existing,
                                        action_items=re_items or None,
                                        extracted_records=re_records or None,
                                        console=console,
                                    )
                                    return
                            except Exception:
                                pass  # Fall through to "no changes" message
                        console.print("[dim]No new notes or records to update.[/dim]")
                        return

                    merge_result = _mr_result

                    def _merge_preview():
                        show_merge_preview(existing, merge_result)

                    def _merge_edit():
                        nonlocal merge_result
                        merge_result = edit_merge_result(existing, merge_result)

                    def _merge_revise():
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
                        _merge_preview, verb="merge",
                        edit_fn=_merge_edit, revise_fn=_merge_revise,
                        console=console,
                    ):
                        return

                    new_actions, new_records = apply_merge(config, db, existing, merge_result, raw_text, paste_content)
                    try_vector_sync_note(config, existing)
                    sync_todo_index(config, db)
                    sync_ideas_index(config, db)
                    console.print(f"[green]Merged into:[/green] {existing.file_path}")

                    # Step 2: confirm and save extracted records separately
                    if new_actions or new_records:
                        preview_and_save_records(
                            config, db, existing,
                            action_items=new_actions or None,
                            extracted_records=new_records or None,
                            console=console,
                        )
                    else:
                        console.print("[dim]No new action items or records extracted.[/dim]")
                    return

        # New note — structure with AI
        # If no typed context, try to detect inline instructions in pasted text
        instruction = user_context
        structuring_text = masked_text
        if not instruction:
            detected, remaining = _extract_inline_instruction(masked_text)
            if detected:
                instruction = detected
                structuring_text = remaining
                console.print(f"[dim]Detected instruction: {detected[:80]}{'...' if len(detected) > 80 else ''}[/dim]")

        console.print("[dim]Structuring...[/dim]")
        try:
            result = structure_only(
                structuring_text, space_config_dict, input_size,
                user_name=config.user_name,
                user_instruction=instruction,
                workspace_path=config.base_dir,
            )
        except ListItemResult as e:
            # AI chose list item — unmask and handle
            list_data = e.data
            if secret_mapping:
                for item in list_data.get("items", []):
                    item["text"] = unmask_secrets(item["text"], secret_mapping)
                    if item.get("summary"):
                        item["summary"] = unmask_secrets(item["summary"], secret_mapping)
            _handle_list_result(config, db, list_data)
            return
        except SnippetResult as e:
            snippet_data = e.data
            if secret_mapping:
                # |||...|||  markers = user explicitly said "secret"
                # Use AI's entity/key naming to store in .secrets.toml
                _handle_secret_snippets(config, snippet_data, secret_mapping)
            else:
                _handle_snippet_result(config, db, snippet_data, folder_default, routing=routing)
            return
        except RecordsOnlyResult as e:
            _handle_records_only_result(config, db, e.data, routing, secret_mapping)
            return
        except Exception as e:
            console.print(f"[red]AI structuring failed: {e}[/red]")
            return

        # Unmask secrets
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

        # Apply routing to the result
        result.routing = NoteRouting(
            space=routing.space,
            group_slug=routing.group_slug,
            group_display=routing.group_display,
            group_is_new=routing.group_is_new,
            subgroup_slug=routing.subgroup_slug,
            subgroup_display=routing.subgroup_display,
            subgroup_is_new=routing.subgroup_is_new,
        )

        _handle_note_result(config, db, result, raw_text, input_size,
                            paste_content=paste_content, attachment_path=attachment_path)

    except Exception as e:
        console.print(f"[red]Error: {e}[/red]")
        console.print("[dim]Try again, or use /help.[/dim]")
    finally:
        db.close()


def _handle_note_result(config, db, result, raw_text, input_size, paste_content=None, attachment_path=None):
    """Handle AI returning a full note — build Note, preview, save."""
    import uuid
    from ...files import copy_attachment
    from ...models import Note, Refinement
    from ...storage import generate_file_path, save_and_sync

    routing = result.routing
    meta = result.metadata

    # Compute space config for preview + revise
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

    now = datetime.now(timezone.utc).isoformat()
    note_id = uuid.uuid4().hex[:8]

    # Build space metadata from routing + AI output
    from ...storage import build_space_metadata
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
    note_action_items = list(meta.action_items)
    note_extracted_records = list(result.extracted_records)

    # Preview → edit/revise loop
    def _note_preview():
        lines = [
            f"[bold]Space:[/bold]    {note.space}",
            f"[bold]Title:[/bold]    {note.title}",
            f"[bold]Date:[/bold]     {note.date}",
            f"[bold]Source:[/bold]   {note.source}",
        ]
        for key in ("client", "topic", "category"):
            val = note.space_metadata.get(key)
            if val:
                display = note.space_metadata.get(f"{key}_display", val)
                lines.append(f"[bold]{key.title()}:[/bold]  {display}")
        if note.tags:
            lines.append(f"[bold]Tags:[/bold]     {', '.join(note.tags)}")
        if note.participants:
            lines.append(f"[bold]People:[/bold]   {', '.join(note.participants)}")
        lines.append(f"[bold]Summary:[/bold]  {note.summary}")
        if note.body:
            lines.append("")
            for bl in note.body.strip().splitlines():
                lines.append(f"[dim]{bl}[/dim]")
        if routing.group_is_new and space_cfg:
            lines.append(f"[yellow]NEW {space_cfg.group_by}: {routing.group_display}[/yellow]")
        if routing.subgroup_is_new and routing.subgroup_slug and space_cfg:
            lines.append(f"[yellow]NEW {space_cfg.subgroup_by}: {routing.subgroup_display}[/yellow]")
        console.print(Panel("\n".join(lines), title="Note", border_style="green"))

    def _note_edit():
        from ...storage import edit_note_in_editor
        edited = edit_note_in_editor(note)
        if edited:
            note.file_path = generate_file_path(
                config, routing.space, routing.group_slug, meta.date,
                note.title, routing.subgroup_slug,
            )
        else:
            console.print("[dim]No changes.[/dim]")

    def _note_revise():
        nonlocal note_action_items
        instruction = Prompt.ask("[dim]What should AI change?[/dim]")
        if instruction.strip():
            console.print("[dim]Revising...[/dim]")
            try:
                from ...ai import revise_note
                revised = revise_note(
                    note, instruction.strip(), space_config_dict, input_size,
                    user_name=config.user_name,
                    action_items=note_action_items,
                )
                note.title = revised.metadata.title
                note.summary = revised.metadata.summary
                note.tags = revised.metadata.tags
                note.participants = revised.metadata.participants
                note_action_items = list(revised.metadata.action_items)
                note.body = revised.body_markdown
                note.refinement = Refinement.HUMAN_REVIEWED
                note.file_path = generate_file_path(
                    config, routing.space, routing.group_slug,
                    revised.metadata.date, note.title, routing.subgroup_slug,
                )
            except Exception as e:
                console.print(f"[red]AI revision failed: {e}[/red]")

    confirmed = confirm_action(
        _note_preview, verb="save",
        edit_fn=_note_edit, revise_fn=_note_revise,
        console=console,
    )

    if not confirmed:
        return

    # Copy attachment if present
    if attachment_path:
        rel = copy_attachment(
            attachment_path, config, routing.space, routing.group_slug,
            routing.subgroup_slug,
        )
        note.attachments.append(rel)
        console.print(f"[dim]Attached: {rel}[/dim]")

    # Save note via shared pipeline (records confirmed separately)
    save_and_sync(config, db, note, hash_source=paste_content, routing=routing,
                  source_file=attachment_path)

    console.print(f"[green]Saved:[/green] {note.file_path}")

    # Step 2: confirm and save extracted records separately
    if note_action_items or note_extracted_records:
        preview_and_save_records(
            config, db, note,
            action_items=note_action_items or None,
            extracted_records=note_extracted_records or None,
            console=console,
        )


def _handle_secret_snippets(config, data, secret_mapping):
    """Route |||...|||-marked snippets to .secrets.toml using AI's entity/key naming."""
    from rich.panel import Panel

    from ...ai import unmask_secrets

    items = data.get("items", [])
    if not items:
        console.print("[yellow]AI returned no items.[/yellow]")
        return

    # Build preview lines
    preview_data = []
    for item in items:
        service = item.get("entity", "auto")
        key = item.get("key", "secret")
        desc = f" — {item['description']}" if item.get("description") else ""
        preview_data.append((service, key, desc))

    def _secret_preview():
        lines = [f"[bold]Saving {len(preview_data)} secret(s) to .secrets.toml:[/bold]", ""]
        for i, (service, key, desc) in enumerate(preview_data, 1):
            lines.append(f"  {i}. [cyan]{service}[/cyan].{key} = ********[dim]{desc}[/dim]")
        console.print(Panel("\n".join(lines), title="Secrets", border_style="green"))

    if not confirm_action(_secret_preview, verb="save", console=console):
        return

    # Save to .secrets.toml
    from ...secrets import SecretsStore
    store = SecretsStore(config.secrets_path)
    for item in items:
        real_value = unmask_secrets(item.get("value", ""), secret_mapping)
        store.store(item.get("entity", "auto"), item.get("key", "secret"), real_value)

    console.print(f"[green]Saved {len(items)} secret(s) to .secrets.toml[/green]")


def _handle_snippet_result(config, db, data, working_folder=None, routing=None):
    """Handle AI's decision to save snippets instead of a note."""
    from ...storage import confirm_and_save_snippets
    if working_folder:
        space = working_folder.get("space", "")
        group = working_folder.get("group_slug", "")
    elif routing:
        space = routing.space
        group = routing.group_slug
    else:
        space = ""
        group = ""
    confirm_and_save_snippets(config, db, data, space=space, group_slug=group)


def _handle_list_result(config, db, result):
    """Handle AI returning list items (todos/ideas)."""
    from ...storage import confirm_and_save_list_items
    confirm_and_save_list_items(config, db, result)


def _handle_records_only_result(config, db, data, routing, secret_mapping=None):
    """Handle AI's decision to extract records only (no note)."""
    from ...ai import unmask_secrets
    from ...models import ActionItem

    raw_records = data.get("extracted_records", [])
    if not raw_records:
        console.print("[dim]No records found to extract.[/dim]")
        return

    # Unmask secrets
    if secret_mapping:
        for rec in raw_records:
            if rec.get("value"):
                rec["value"] = unmask_secrets(rec["value"], secret_mapping)
            if rec.get("entity"):
                rec["entity"] = unmask_secrets(rec["entity"], secret_mapping)

    # Split todos from other database records (same as _parse_structure_only_output)
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

    if not items and not records:
        console.print("[dim]No records found to extract.[/dim]")
        return

    # For records-only, we need a note to link to. Try to find existing note
    # from routing (duplicate case) or create a minimal reference.
    existing_note = None
    if routing and routing.existing_note_id:
        from ...storage import read_note
        existing_db_row = db.get_note(routing.existing_note_id)
        if existing_db_row:
            existing_note = read_note(config, existing_db_row["file_path"])
    elif routing and routing.append_to_note:
        from ...storage import read_note
        existing_db_row = db.get_note(routing.append_to_note)
        if existing_db_row:
            existing_note = read_note(config, existing_db_row["file_path"])

    if existing_note:
        preview_and_save_records(
            config, db, existing_note,
            action_items=items or None,
            extracted_records=records or None,
            console=console,
        )
    else:
        # No existing note — save records standalone (scoped to routing folder)
        from ...models import Note
        placeholder = Note(
            id="",
            space=routing.space if routing else "",
            file_path=f"{routing.space}/{routing.group_slug}/" if routing else "",
        )
        preview_and_save_records(
            config, db, placeholder,
            action_items=items or None,
            extracted_records=records or None,
            console=console,
        )


def _clip_url(config: NotelyConfig, arg: str, working_folder: dict) -> None:
    """Clip a web page: fetch via Firecrawl, structure with AI, save."""
    import uuid
    from ...ai import structure_only, mask_secrets, unmask_secrets, ListItemResult, SnippetResult, RecordsOnlyResult
    from ...models import Note, NoteRouting, Refinement
    from ...routing import route_input, ensure_directory_indexed, RoutingDecision
    from ...storage import (
        generate_file_path, save_and_sync,
    )
    from ...vectors import get_vector_store

    parts = arg.strip().split(None, 1)
    if not parts:
        console.print("[yellow]Usage: /clip URL [FOLDER][/yellow]")
        return

    url = parts[0]
    folder_query = parts[1] if len(parts) > 1 else ""

    # Validate URL
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    # Resolve folder: explicit arg > working folder > routing
    folder: dict | None = None
    if folder_query:
        match = _fuzzy_match_folder(config, folder_query)
        if match:
            folder = {"space": match[0], "group_slug": match[1], "display": match[2]}
        else:
            console.print(f"[yellow]No folder matching '{folder_query}'.[/yellow]")
            return
    elif working_folder:
        folder = working_folder

    # Fetch
    try:
        from ...web import fetch_page
    except ImportError:
        console.print("[yellow]firecrawl-py not installed. Install: pip install firecrawl-py[/yellow]")
        return

    console.print(f"[dim]Fetching {url}...[/dim]")
    try:
        markdown, metadata = fetch_page(url)
    except ImportError as e:
        console.print(f"[yellow]{e}[/yellow]")
        return
    except ValueError as e:
        console.print(f"[yellow]{e}[/yellow]")
        return
    except Exception as e:
        console.print(f"[red]Fetch error: {e}[/red]")
        return

    if not markdown.strip():
        console.print("[yellow]No content fetched from URL.[/yellow]")
        return

    word_count = len(markdown.split())
    console.print(f"[dim]Fetched ({word_count:,} words)[/dim]")

    input_size = classify_input_size(markdown)

    db = Database(config.db_path)
    db.initialize()

    try:
        # Determine routing
        if folder:
            from ...routing import _routing_from_folder_default
            routing = _routing_from_folder_default(config, db, folder)
        else:
            # Run normal routing pipeline
            vec_store = get_vector_store(config)
            routing = route_input(config, db, vec_store, markdown)
            if routing is None:
                console.print("[yellow]Cancelled.[/yellow]")
                return

        # Mask secrets
        masked_text, secret_mapping = mask_secrets(markdown)
        if secret_mapping:
            from ...secrets import SecretsStore
            SecretsStore(config.secrets_path).store_mapping(secret_mapping)

        # Build space config
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

        # Structure with AI
        page_title = metadata.get("title", "")
        instruction = f"This is a web page clipped from {url}."
        if page_title:
            instruction += f" Page title: {page_title}."

        console.print("[dim]Structuring...[/dim]")
        try:
            result = structure_only(
                masked_text, space_config_dict, input_size,
                user_name=config.user_name,
                user_instruction=instruction,
                workspace_path=config.base_dir,
            )
        except ListItemResult as e:
            list_data = e.data
            if secret_mapping:
                for item in list_data.get("items", []):
                    item["text"] = unmask_secrets(item["text"], secret_mapping)
                    if item.get("summary"):
                        item["summary"] = unmask_secrets(item["summary"], secret_mapping)
            _handle_list_result(config, db, list_data)
            return
        except SnippetResult as e:
            snippet_data = e.data
            if secret_mapping:
                _handle_secret_snippets(config, snippet_data, secret_mapping)
            else:
                folder_dict = folder if folder else None
                _handle_snippet_result(config, db, snippet_data, folder_dict)
            return
        except RecordsOnlyResult as e:
            _handle_records_only_result(config, db, e.data, routing, secret_mapping)
            return
        except Exception as e:
            console.print(f"[red]AI structuring failed: {e}[/red]")
            return

        # Unmask secrets
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

        # Apply routing
        result.routing = NoteRouting(
            space=routing.space,
            group_slug=routing.group_slug,
            group_display=routing.group_display,
            group_is_new=routing.group_is_new,
            subgroup_slug=routing.subgroup_slug,
            subgroup_display=routing.subgroup_display,
            subgroup_is_new=routing.subgroup_is_new,
        )

        # Build the Note — inline for more control over source_url
        now = datetime.now(timezone.utc).isoformat()
        note_id = uuid.uuid4().hex[:8]
        meta = result.metadata

        from ...storage import build_space_metadata
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
            source=f"web-clip",
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
            raw_text=markdown,
            source_url=url,
            related_contexts=result.related_contexts,
            space_metadata=space_metadata,
        )

        # Action items stored separately — will go to DB via save_and_sync
        clip_action_items = list(meta.action_items)
        clip_extracted_records = list(result.extracted_records)

        # Preview + confirm with edit/revise loop
        def _clip_preview():
            lines = [
                f"[bold]Space:[/bold]    {note.space}",
                f"[bold]Title:[/bold]    {note.title}",
                f"[bold]Date:[/bold]     {note.date}",
                f"[bold]Source:[/bold]   {note.source}",
                f"[bold]URL:[/bold]      {note.source_url}",
            ]
            for key in ("client", "topic", "category"):
                val = note.space_metadata.get(key)
                if val:
                    display = note.space_metadata.get(f"{key}_display", val)
                    lines.append(f"[bold]{key.title()}:[/bold]  {display}")
            if note.tags:
                lines.append(f"[bold]Tags:[/bold]     {', '.join(note.tags)}")
            if note.participants:
                lines.append(f"[bold]People:[/bold]   {', '.join(note.participants)}")
            lines.append(f"[bold]Summary:[/bold]  {note.summary}")
            if note.body:
                lines.append("")
                for bl in note.body.strip().splitlines():
                    lines.append(f"[dim]{bl}[/dim]")
            console.print(Panel("\n".join(lines), title="Web Clip", border_style="green"))

        def _clip_edit():
            from ...storage import edit_note_in_editor
            edited = edit_note_in_editor(note)
            if edited:
                note.file_path = generate_file_path(
                    config, routing.space, routing.group_slug, meta.date,
                    note.title, routing.subgroup_slug,
                )
            else:
                console.print("[dim]No changes.[/dim]")

        def _clip_revise():
            nonlocal clip_action_items
            instruction = Prompt.ask("[dim]What should AI change?[/dim]")
            if instruction.strip():
                console.print("[dim]Revising...[/dim]")
                try:
                    from ...ai import revise_note
                    revised = revise_note(
                        note, instruction.strip(), space_config_dict, input_size,
                        user_name=config.user_name,
                        action_items=clip_action_items,
                    )
                    note.title = revised.metadata.title
                    note.summary = revised.metadata.summary
                    note.tags = revised.metadata.tags
                    note.participants = revised.metadata.participants
                    clip_action_items = list(revised.metadata.action_items)
                    note.body = revised.body_markdown
                    note.refinement = Refinement.HUMAN_REVIEWED
                    note.file_path = generate_file_path(
                        config, routing.space, routing.group_slug,
                        revised.metadata.date, note.title, routing.subgroup_slug,
                    )
                except Exception as e:
                    console.print(f"[red]AI revision failed: {e}[/red]")

        if not confirm_action(
            _clip_preview, verb="save",
            edit_fn=_clip_edit, revise_fn=_clip_revise,
            console=console,
        ):
            return

        # Save note (records confirmed separately)
        save_and_sync(config, db, note, routing=routing)
        console.print(f"[green]Saved:[/green] {note.file_path}")

        # Step 2: confirm and save extracted records separately
        if clip_action_items or clip_extracted_records:
            preview_and_save_records(
                config, db, note,
                action_items=clip_action_items or None,
                extracted_records=clip_extracted_records or None,
                console=console,
            )

    except Exception as e:
        console.print(f"[red]Error: {e}[/red]")
    finally:
        db.close()
