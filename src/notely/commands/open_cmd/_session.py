"""notely open — persistent interactive session for continuous note capture."""

from __future__ import annotations

import click
from rich.panel import Panel

from ...config import NotelyConfig
from ...db import Database

from ._shared import (
    console,
    logger,
    _fuzzy_match_folder,
    _working_folder_query,
    _ensure_vectors,
    _resync,
    _confirm_new_database,
)

HELP_TEXT = """
[bold]Capture:[/bold]
  Paste or type anything — AI structures it into notes.
  [bold]Enter[/bold] for new lines, [bold]Enter twice[/bold] to submit.
  [cyan]/clip URL[/cyan] [dim]\\[FOLDER][/dim]              Clip a web page as a note
  [cyan]/folder FOLDER[/cyan]                  Set working folder (clears with [cyan]/folder[/cyan])

[bold]Chat & Agent:[/bold]
  [cyan]/chat[/cyan] [dim]\\[FOLDER][/dim]                   Q&A about a folder's notes
  [cyan]/agent chat[/cyan] [dim]\\[FOLDER][/dim]             Conversational agent (external services)
  [cyan]/agent run FOLDER request[/cyan]        One-shot agent action
  [cyan]/agent connect[/cyan] [dim]\\[FOLDER][/dim]          Connect services
  [cyan]/agent disconnect FOLDER SERVICE[/cyan] Remove a service

[bold]Workflows & Inbox:[/bold]
  [cyan]/workflow create[/cyan]  Create a new workflow with AI assistance
  [cyan]/workflow pull[/cyan]    Pull from all workflows ([cyan]/workflow pull NAME[/cyan] for one)
  [cyan]/workflow list[/cyan]    Show available workflows
  [cyan]/inbox[/cyan]            Review pending inbox items
  [cyan]/inbox count[/cyan]      Show inbox count
  [cyan]/inbox history[/cyan]    Recently filed items

[bold]Notes:[/bold]
  [cyan]/list[/cyan] [dim]\\[FOLDER][/dim]               Recent notes (folder-scoped)
  [cyan]/search[/cyan] [dim]\\[FOLDER][/dim] [cyan]TEXT[/cyan]      Search notes (folder-scoped)
  [cyan]/edit ID[/cyan]         Open note in $EDITOR
  [cyan]/delete ID[/cyan]       Delete a note

[bold]Tasks & Time:[/bold]
  [cyan]/todo[/cyan]            Interactive todo mode (done, add, today, timer, plan)
  [cyan]/todo done ID[/cyan]    Quick mark done from main prompt
  [cyan]/ideas[/cyan] [dim]\\[FOLDER][/dim]              Ideas pipeline
  [cyan]/timer[/cyan]           Time tracking ([cyan]/timer start[/cyan], [cyan]stop[/cyan], [cyan]add[/cyan], [cyan]log[/cyan])
  [cyan]/secret[/cyan]          Secrets ([cyan]/secret service key[/cyan] to view)

[bold]Databases:[/bold]
  [cyan]/<name>[/cyan]                         Enter database mode (contacts, references, or custom)
  [cyan]/<name> add ENTITY key value[/cyan]    Quick add a record
  [cyan]/<name> delete ID[/cyan]               Delete a record
  [cyan]/<name> show ENTITY[/cyan]             Show entity details

[bold]Workspace:[/bold]
  [cyan]/spaces[/cyan]          Show spaces overview
  [cyan]/mkdir PATH[/cyan]      Add a folder
  [cyan]/rmdir PATH[/cyan]      Remove an empty folder
  [cyan]/sync[/cyan]            Re-sync DB from files
""".strip()


@click.command("open")
@click.pass_context
def open_cmd(ctx: click.Context) -> None:
    """Open a persistent session for continuous note capture.

    Stay in notely and keep working — paste notes, check todos,
    search, all without leaving. Type /help for commands.
    """
    config: NotelyConfig = ctx.obj["config"]

    config.ensure_initialized()

    # Sync DB on startup — prune entries for deleted files
    db = Database(config.db_path)
    db.initialize()

    # Migrate references.toml to DB (one-time, idempotent)
    db.migrate_references_toml(config)

    # Migrate action_items to snippets table (one-time, idempotent)
    db.migrate_action_items_to_snippets()

    pruned = db.prune_missing(config)
    if pruned:
        from ...storage import sync_todo_index, sync_ideas_index
        sync_todo_index(config, db)
        sync_ideas_index(config, db)
        console.print(f"[dim]Synced: removed {pruned} deleted note(s) from index.[/dim]")

    # Backfill raw_hash for notes saved before hash was added
    db.backfill_raw_hashes(config)

    # Auto-build vectors on first run if notes exist
    _ensure_vectors(config, db)
    db.close()

    # Build tab completer for slash commands + folder names
    completer = _SlashCompleter(config)

    _BANNER = (
        "███╗   ██╗ ██████╗ ████████╗███████╗██╗  ██╗   ██╗\n"
        "████╗  ██║██╔═══██╗╚══██╔══╝██╔════╝██║  ╚██╗ ██╔╝\n"
        "██╔██╗ ██║██║   ██║   ██║   █████╗  ██║   ╚████╔╝\n"
        "██║╚██╗██║██║   ██║   ██║   ██╔══╝  ██║    ╚██╔╝\n"
        "██║ ╚████║╚██████╔╝   ██║   ███████╗███████╗██║\n"
        "╚═╝  ╚═══╝ ╚═════╝    ╚═╝   ╚══════╝╚══════╝╚═╝"
    )
    console.print(Panel(
        f"[bold cyan]{_BANNER}[/bold cyan]\n\n"
        "Paste or type anything to capture it. The AI will figure out\n"
        "whether it's a note, a todo, or an idea.\n\n"
        "[bold]Enter[/bold] for new lines, [bold]Enter twice[/bold] to submit.\n"
        "Type [cyan]/help[/cyan] for commands, [cyan]/quit[/cyan] to exit.",
        border_style="blue",
        width=65,
    ))
    console.print()

    # Show running timers from previous session
    from ...timer import get_running_timers, elapsed_since
    running = get_running_timers(config)
    for t in running:
        desc = t.get("description", "untitled")
        folder = t.get("folder", "")
        elapsed = elapsed_since(t["start"])
        console.print(f"[yellow]Timer running:[/yellow] {desc} ({folder}) — {elapsed}")

    # Run startup agents (e.g., granola-sync) — fire-and-forget
    try:
        from notely_agent.api import run_startup_agents
        import asyncio
        new_items = asyncio.run(run_startup_agents())
        if new_items:
            console.print(f"[cyan]{len(new_items)} new item(s) added to inbox[/cyan]")
    except ImportError:
        pass  # notely-agent not installed
    except Exception as e:
        logger.debug("Startup agents failed: %s", e)

    # Show pending inbox items
    with Database(config.db_path) as inbox_db:
        inbox_db.initialize()
        pending = inbox_db.count_inbox("pending")
        if pending:
            console.print(f"[cyan]{pending} item(s) in inbox[/cyan] — type /inbox to review")

    import time
    last_interrupt = 0.0

    # Working folder — persists across commands in this session
    working_folder: dict = {}  # {"space": ..., "group_slug": ..., "display": ...} or empty

    while True:
        if working_folder:
            prompt_label = f"notely ({working_folder['display']})> "
        else:
            prompt_label = "notely-notetaker> "

        try:
            text = _read_block(completer=completer, prompt=prompt_label)
        except EOFError:
            console.print("\n[dim]Bye.[/dim]")
            break
        except KeyboardInterrupt:
            now = time.monotonic()
            if last_interrupt > 0 and now - last_interrupt < 5.0:
                console.print("\n[dim]Bye.[/dim]")
                break
            last_interrupt = now
            console.print("\n[dim]Ctrl+C again to quit.[/dim]")
            continue

        if not text.strip():
            continue

        stripped = text.strip()

        # Handle slash commands
        if stripped.startswith("/"):
            parts = stripped.split(None, 1)
            cmd = parts[0].lower()
            arg = parts[1] if len(parts) > 1 else ""

            if cmd in ("/quit", "/exit", "/q"):
                console.print("[dim]Bye.[/dim]")
                break
            elif cmd == "/help":
                console.print(HELP_TEXT)
            elif cmd == "/folder":
                if arg:
                    match = _fuzzy_match_folder(config, arg)
                    if match:
                        space, group_slug, display_name, _ = match
                        working_folder.clear()
                        working_folder.update(space=space, group_slug=group_slug, display=display_name)
                        console.print(f"[green]Now working in:[/green] {display_name} ({space}/{group_slug})" if group_slug else f"[green]Now working in:[/green] {display_name} ({space})")
                    else:
                        console.print(f"[yellow]No folder matching '{arg}'.[/yellow]")
                else:
                    working_folder.clear()
                    console.print("[dim]Cleared working folder.[/dim]")
            elif cmd == "/clip":
                _clip_url(config, arg, working_folder)
            elif cmd == "/todo":
                _handle_todo(config, arg, completer)
            elif cmd == "/ideas":
                _show_ideas(config, arg)
            elif cmd == "/list":
                _show_list(config, arg)
            elif cmd == "/search":
                if arg:
                    _show_search(config, arg)
                else:
                    console.print("[yellow]Usage: /search TEXT[/yellow]")
            elif cmd == "/spaces":
                _show_spaces(config)
            elif cmd == "/mkdir":
                if arg:
                    _mkdir(config, arg)
                    completer.invalidate()
                else:
                    console.print("[yellow]Usage: /mkdir space/group[/subgroup][/yellow]")
            elif cmd == "/rmdir":
                if arg:
                    _rmdir(config, arg)
                    completer.invalidate()
                else:
                    console.print("[yellow]Usage: /rmdir space/group[/subgroup][/yellow]")
            elif cmd == "/delete":
                if arg:
                    # Support both "/delete NOTE_ID" and "/delete FOLDER NOTE_ID"
                    parts = arg.split(None, 1)
                    if len(parts) == 1:
                        # Single word — could be a note ID or a folder
                        # Check if it resolves to a folder
                        match = _fuzzy_match_folder(config, parts[0])
                        if match:
                            console.print(f"[yellow]That's a folder. Use tab to pick a note: /delete {parts[0]} [tab][/yellow]")
                        else:
                            _delete_note(config, parts[0].strip())
                            completer.invalidate_notes()
                    else:
                        note_id = parts[-1].strip()
                        _delete_note(config, note_id)
                        completer.invalidate_notes()
                else:
                    console.print("[yellow]Usage: /delete FOLDER NOTE_ID[/yellow]")
            elif cmd == "/chat":
                _chat_mode(config, arg or _working_folder_query(working_folder))
            elif cmd == "/sync":
                _resync(config)
            elif cmd == "/timer":
                try:
                    _timer_dispatch(config, arg)
                except KeyboardInterrupt:
                    console.print("\n[yellow]Cancelled.[/yellow]")
            elif cmd == "/workflow":
                _handle_workflow(config, arg)
            elif cmd == "/inbox":
                _handle_inbox(config, arg, working_folder, completer)
            elif cmd == "/secret":
                _show_secrets(config, arg)
            elif cmd == "/agent":
                _agent_dispatch(config, arg, working_folder)
            elif cmd == "/edit":
                if arg:
                    # Support both "/edit NOTE_ID" and "/edit FOLDER NOTE_ID"
                    parts = arg.split(None, 1)
                    if len(parts) == 1:
                        match = _fuzzy_match_folder(config, parts[0])
                        if match:
                            console.print(f"[yellow]That's a folder. Use tab to pick a note: /edit {parts[0]} [tab][/yellow]")
                        else:
                            _edit_note_inline(config, parts[0].strip())
                    else:
                        note_id = parts[-1].strip()
                        _edit_note_inline(config, note_id)
                else:
                    console.print("[yellow]Usage: /edit FOLDER NOTE_ID[/yellow]")
            elif cmd == "/connect":
                console.print("[dim]Tip: use /agent connect[/dim]")
                _agent_connect(config, arg)
            elif cmd == "/disconnect":
                console.print("[dim]Tip: use /agent disconnect[/dim]")
                _agent_disconnect(config, arg)
            else:
                # Check if it's a user-defined database name
                db_name = cmd.lstrip("/")
                if _is_known_database(config, db_name):
                    _handle_database_command(config, db_name, arg, working_folder or None)
                elif arg.strip():
                    # User typed /<name> <something> — likely a new database add
                    if _confirm_new_database(config, db_name):
                        _known_db_cache_invalidate()
                        _handle_database_command(config, db_name, arg, working_folder or None)
                else:
                    console.print(f"[yellow]Unknown command: {cmd}. Type /help[/yellow]")

            console.print()
            continue

        # It's note content — process with AI
        try:
            _process_input(config, text, folder_default=working_folder or None)
        except KeyboardInterrupt:
            console.print("\n[yellow]Aborted.[/yellow]")
        console.print()


from ._completers import _SlashCompleter
from ._input import _read_block, _process_input, _clip_url
from ._handlers import (
    _handle_todo, _show_ideas,
    _show_list, _show_search, _show_spaces, _timer_dispatch,
    _handle_database_command, _show_secrets, _handle_workflow,
    _mkdir, _rmdir, _delete_note, _edit_note_inline,
)
from ._inbox import _handle_inbox
from ._agent import _agent_dispatch, _agent_connect, _agent_disconnect, _chat_mode


_known_db_cache: set[str] | None = None


def _is_known_database(config: NotelyConfig, name: str) -> bool:
    """Check if name is a known database (has records)."""
    global _known_db_cache
    if _known_db_cache is not None and name in _known_db_cache:
        return True
    # Check DB for databases with records
    try:
        with Database(config.db_path) as db:
            db.initialize()
            names = set(db.get_database_names())
        _known_db_cache = names
        return name in _known_db_cache
    except Exception:
        return False


def _known_db_cache_invalidate() -> None:
    """Clear the database name cache after creating a new database."""
    global _known_db_cache
    _known_db_cache = None


