"""Agent mode, chat mode, and service connections."""

from __future__ import annotations

import sys

from rich.panel import Panel
from rich.prompt import Prompt

from ...config import NotelyConfig
from ...db import Database

from ._shared import console, _get_all_folders, _fuzzy_match_folder, _working_folder_query
from ._completers import _ConnectFolderCompleter, _make_agent_note_completer


def _agent_dispatch(config: NotelyConfig, arg: str, working_folder: dict | None = None) -> None:
    """Route /agent subcommands.

    Subcommands:
    - /agent              → show help + connected services
    - /agent chat [FOLDER] → conversational agent mode
    - /agent run FOLDER [SERVICE] request → one-shot action
    - /agent connect [FOLDER] → connect services
    - /agent disconnect FOLDER SERVICE → remove a service

    Backwards compat: /agent FOLDER (bare folder name) → /agent chat FOLDER
    """
    working_folder = working_folder or {}
    parts = arg.strip().split(None, 1)
    subcmd = parts[0].lower() if parts else ""
    rest = parts[1] if len(parts) > 1 else ""

    if subcmd == "chat":
        _agent_chat(config, rest or _working_folder_query(working_folder))
    elif subcmd == "run":
        _agent_run(config, rest)
    elif subcmd == "connect":
        _agent_connect(config, rest)
    elif subcmd == "disconnect":
        _agent_disconnect(config, rest)
    elif not subcmd:
        _agent_help(config)
    else:
        # Backwards compat: bare folder name → treat as /agent chat FOLDER
        _agent_chat(config, arg)


def _agent_help(config: NotelyConfig) -> None:
    """Show /agent subcommands and connected services overview."""
    console.print("[bold]Agent Commands:[/bold]")
    console.print("  [cyan]/agent chat[/cyan] [dim]\\[FOLDER][/dim]      Conversational agent mode")
    console.print("  [cyan]/agent run[/cyan] [dim]FOLDER request[/dim]   One-shot agent action")
    console.print("  [cyan]/agent connect[/cyan] [dim]\\[FOLDER][/dim]   Connect services")
    console.print("  [cyan]/agent disconnect[/cyan] [dim]FOLDER SERVICE[/dim]  Remove a service")
    console.print()

    # Show connected services overview
    try:
        from notely_agent.api import AgentConfig
        agent_config = AgentConfig()
        accounts = agent_config.accounts
        if accounts:
            console.print("[bold]Connected Services:[/bold]")
            for acct in accounts.values():
                services = ", ".join(sorted(acct.services.keys())) or "none"
                spaces = ", ".join(acct.spaces) if acct.spaces else "all folders"
                console.print(f"  [cyan]{acct.name}[/cyan] ({spaces}): {services}")
        else:
            console.print("[dim]No services connected. Run /agent connect to set up.[/dim]")
    except ImportError:
        console.print("[dim]notely-agent not installed. Install: pip install -e path/to/notely-agent[/dim]")
    except Exception:
        console.print("[dim]Could not load agent config.[/dim]")


def _agent_chat(config: NotelyConfig, arg: str) -> None:
    """Conversational agent mode.

    /agent chat           → global default services
    /agent chat all       → same
    /agent chat FOLDER    → folder-scoped
    """
    try:
        from notely_agent.api import AgentConfig
    except ImportError:
        console.print("[yellow]notely-agent not installed.[/yellow]")
        console.print("[dim]Install: pip install -e path/to/notely-agent[/dim]")
        return

    raw = arg.strip()

    # /agent chat or /agent chat all → global default
    if not raw or raw.lower() == "all":
        agent_config = AgentConfig()
        errors = agent_config.validate()
        if errors:
            console.print("[red]Configuration errors:[/red]")
            for e in errors:
                console.print(f"  [red]- {e}[/red]")
            console.print("\n[dim]Run /agent connect to set up services, or add missing keys to .env[/dim]")
            return
        _agent_mode(config, agent_config, space="", group_slug="", display_name="All Services")
        return

    # Split: FOLDER [SERVICE ...] — try progressively shorter prefixes as folder
    words = raw.split()
    match = None
    services_filter = None

    # Try "word1/word2" first (full path), then just "word1"
    for n in range(min(len(words), 2), 0, -1):
        folder_query = " ".join(words[:n])
        match = _fuzzy_match_folder(config, folder_query)
        if match:
            remaining = words[n:]
            if remaining:
                services_filter = remaining
            break

    if not match:
        console.print(f"[yellow]No folder matching '{raw}'.[/yellow]")
        return

    space, group_slug, display_name, _subgroup = match

    agent_config = AgentConfig()
    errors = agent_config.validate()
    if errors:
        console.print("[red]Configuration errors:[/red]")
        for e in errors:
            console.print(f"  [red]- {e}[/red]")
        console.print("\n[dim]Run /agent connect to set up services, or add missing keys to .env[/dim]")
        return

    _agent_mode(config, agent_config, space, group_slug, display_name,
                services_filter=services_filter)


def _agent_run(config: NotelyConfig, arg: str) -> None:
    """One-shot agent action.

    /agent run FOLDER [SERVICE] request
    /agent run  (no args → guided flow)
    """
    try:
        from notely_agent.api import AgentConfig
    except ImportError:
        console.print("[yellow]notely-agent not installed.[/yellow]")
        console.print("[dim]Install: pip install -e path/to/notely-agent[/dim]")
        return

    parts = arg.strip().split(None, 1)

    # Guided flow when called with no args or only a folder
    if not parts:
        _agent_run_guided(config)
        return

    folder_query = parts[0]
    rest = parts[1] if len(parts) > 1 else ""

    match = _fuzzy_match_folder(config, folder_query)
    if not match:
        console.print(f"[yellow]No folder matching '{folder_query}'.[/yellow]")
        return

    space, group_slug, display_name, _subgroup = match

    if not rest.strip():
        # Have folder but no request → prompt for it (multi-line)
        console.print(f"[dim]What do you want to do in {display_name}?[/dim]")
        try:
            read_input = _make_agent_input(display_name[:15])
            rest = read_input()
        except (EOFError, KeyboardInterrupt):
            return
        if not rest.strip():
            return

    agent_config = AgentConfig()
    errors = agent_config.validate()
    if errors:
        console.print("[red]Configuration errors:[/red]")
        for e in errors:
            console.print(f"  [red]- {e}[/red]")
        console.print("\n[dim]Run /agent connect to set up services, or add missing keys to .env[/dim]")
        return

    _execute_one_shot(config, agent_config, space, group_slug, display_name, rest)


def _agent_run_guided(config: NotelyConfig) -> None:
    """Guided flow: pick folder, then type request."""
    try:
        from notely_agent.api import AgentConfig
    except ImportError:
        return

    # Step 1: Pick folder
    answer = _prompt_connect_folder(config)
    if answer is None:
        console.print("[dim]Cancelled.[/dim]")
        return
    if answer == "all":
        console.print("[yellow]One-shot mode requires a specific folder.[/yellow]")
        console.print("[dim]Use /agent chat for global mode.[/dim]")
        return

    space, group_slug, display_name = answer

    # Step 2: Ask what to do (multi-line)
    console.print(f"[dim]What do you want to do in {display_name}?[/dim]")
    try:
        read_input = _make_agent_input(display_name[:15])
        request = read_input()
    except (EOFError, KeyboardInterrupt):
        return
    if not request.strip():
        return

    agent_config = AgentConfig()
    errors = agent_config.validate()
    if errors:
        console.print("[red]Configuration errors:[/red]")
        for e in errors:
            console.print(f"  [red]- {e}[/red]")
        console.print("\n[dim]Run /agent connect to set up services, or add missing keys to .env[/dim]")
        return

    _execute_one_shot(config, agent_config, space, group_slug, display_name, request)


def _execute_one_shot(
    config: NotelyConfig,
    agent_config,
    space: str,
    group_slug: str,
    display_name: str,
    rest: str,
) -> None:
    """Execute a one-shot agent action — just agent chat that auto-sends first message.

    Parses SERVICE from rest if present, filters to that service only.
    """
    folder_path = f"{space}/{group_slug}"
    connected = agent_config.get_connected_services(space=space, folder=folder_path)

    # Parse: rest might be "SERVICE request" or just "request"
    rest_parts = rest.split(None, 1)
    first_word = rest_parts[0].lower()
    service = None
    request = rest

    if first_word in connected:
        service = first_word
        if len(rest_parts) > 1:
            request = rest_parts[1].strip()
        else:
            # FOLDER SERVICE with no request — prompt for it (multi-line)
            console.print(f"[dim]What do you want to do with {service} in {display_name}?[/dim]")
            try:
                read_input = _make_agent_input(f"{service}")
                request = read_input()
            except (EOFError, KeyboardInterrupt):
                return
            if not request.strip():
                return

    services_filter = [service] if service else None

    # Reuse agent chat mode — auto-send the first message, exit when done
    _agent_mode(
        config, agent_config, space, group_slug, display_name,
        first_message=request, services_filter=services_filter,
    )


def _print_agent_error(exc: Exception, services_str: str = "") -> None:
    """Print a clean error message for agent failures."""
    msg = str(exc)
    # MCP connection errors — show a clean message with reconnect hint
    if "ConnectError" in msg or "TaskGroup" in msg or "MCP" in msg.lower():
        console.print(
            f"\n[red]Connection error — could not reach the service.[/red]"
        )
        console.print(
            "[dim]The MCP URL may be stale. Try: /agent connect to refresh.[/dim]"
        )
    elif msg.strip():
        console.print(f"\n[red]Agent error: {msg}[/red]")
    else:
        # Empty error message — dig into the exception chain
        cause = exc.__cause__ or exc.__context__
        if cause:
            cause_msg = str(cause)
            if "ConnectError" in type(cause).__name__ or "ConnectError" in cause_msg:
                console.print(
                    f"\n[red]Connection error — could not reach the service.[/red]"
                )
                console.print(
                    "[dim]The MCP URL may be stale. Try: /agent connect to refresh.[/dim]"
                )
                return
        console.print(f"\n[red]Agent error (see logs for details).[/red]")


def _make_agent_input(label: str, completer=None):
    """Create a multi-line input function for agent/chat modes.

    Enter = new line, Enter on empty line = submit, slash commands submit immediately.
    """
    from prompt_toolkit import PromptSession
    from prompt_toolkit.document import Document
    from prompt_toolkit.key_binding import KeyBindings

    bindings = KeyBindings()

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

    session = PromptSession()

    def read_input() -> str:
        return session.prompt(
            f"\nnotely-agent ({label})> ",
            multiline=True,
            key_bindings=bindings,
            prompt_continuation='. ',
            completer=completer,
            complete_while_typing=True,
        )

    return read_input


def _agent_mode(
    config: NotelyConfig,
    agent_config,
    space: str,
    group_slug: str,
    display_name: str,
    first_message: str | None = None,
    services_filter: list[str] | None = None,
) -> None:
    """Enter conversational agent mode.

    Runs the async event loop in a background thread so that input stays
    synchronous in the main thread — Ctrl+C works naturally (same as /chat).

    Args:
        first_message: If provided, auto-sent as the first user message
            (used by /agent run to skip the first input prompt).
        services_filter: If provided, only connect to these services
            (used by /agent run when a specific service is specified).
    """
    import asyncio
    import threading
    import time

    from rich.markdown import Markdown

    try:
        from notely_agent.api import AgentSession
    except ImportError:
        console.print("[yellow]notely-agent not installed.[/yellow]")
        return

    # Create a dedicated event loop in a background thread
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()

    session = AgentSession(
        agent_config, space=space, folder=group_slug,
        services_filter=services_filter,
    )

    # Start session (async) — opens MCP connections, loads context
    console.print("[dim]Connecting to services...[/dim]")
    try:
        future = asyncio.run_coroutine_threadsafe(session.__aenter__(), loop)
        future.result(timeout=60)
    except ValueError as e:
        console.print(f"[yellow]{e}[/yellow]")
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=5)
        return
    except Exception as e:
        console.print(f"[red]Agent session error: {e}[/red]")
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=5)
        return

    # Show banner
    services_str = ", ".join(session.services)
    console.print(Panel(
        f"[bold]Agent: {display_name}[/bold]\n"
        f"Services: {services_str}\n\n"
        f"Ask questions or request actions.\n"
        f"Writes will be described before executing.\n"
        f"Type [cyan]/back[/cyan] to return to capture mode.",
        border_style="green",
        width=60,
    ))

    prompt_label = group_slug[:15] or space[:15] or "agent"
    last_interrupt = 0.0

    # Multi-line input with @note autocomplete
    completer = _make_agent_note_completer(config, space, group_slug)
    agent_input = _make_agent_input(prompt_label, completer=completer)

    try:
        # If first_message provided (from /agent run), auto-send it
        if first_message:
            console.print(f"\n[cyan]> {first_message}[/cyan]")
            console.print("[dim]Thinking...[/dim]")
            try:
                future = asyncio.run_coroutine_threadsafe(
                    session.send(first_message), loop,
                )
                response = future.result(timeout=120)
                console.print()
                console.print(Markdown(response))
            except KeyboardInterrupt:
                future.cancel()
                console.print("\n[yellow]Interrupted.[/yellow]")
            except Exception as e:
                _print_agent_error(e, services_str)

        while True:
            # Synchronous input — Ctrl+C works naturally
            try:
                user_input = agent_input()
            except EOFError:
                console.print("\n[dim]Exiting agent.[/dim]")
                break
            except KeyboardInterrupt:
                now = time.monotonic()
                if last_interrupt > 0 and now - last_interrupt < 5.0:
                    console.print("\n[dim]Exiting agent.[/dim]")
                    break
                last_interrupt = now
                console.print("\n[dim]Ctrl+C again to exit agent.[/dim]")
                continue

            if not user_input.strip():
                continue

            stripped = user_input.strip().lower()
            if stripped in ("/back", "/exit", "/quit", "/q", "/done"):
                console.print("[dim]Back to notely.[/dim]")
                break

            # Send to agent (async, but with sync wait)
            console.print("[dim]Thinking...[/dim]")
            try:
                future = asyncio.run_coroutine_threadsafe(
                    session.send(user_input), loop,
                )
                response = future.result(timeout=120)
                console.print()
                console.print(Markdown(response))
            except KeyboardInterrupt:
                future.cancel()
                console.print("\n[yellow]Interrupted.[/yellow]")
            except Exception as e:
                _print_agent_error(e, services_str)
    finally:
        # Clean up: close MCP connections
        try:
            future = asyncio.run_coroutine_threadsafe(
                session.__aexit__(None, None, None), loop,
            )
            future.result(timeout=10)
        except Exception:
            pass
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=5)


def _prompt_connect_folder(
    config: NotelyConfig,
) -> tuple[str, str, str] | str | None:
    """Prompt user to pick a folder with tab autocomplete.

    Returns:
        (space, group_slug, display_name) for a specific folder,
        "all" for all folders, or None if cancelled.
    """
    folders = _get_all_folders(config)
    if not folders:
        console.print("[dim]No folders yet. Run /mkdir to create one.[/dim]")
        return None

    completer = _ConnectFolderCompleter(folders)

    from prompt_toolkit import PromptSession
    session: PromptSession = PromptSession(completer=completer)

    try:
        answer = session.prompt("Folder (Enter for all, tab to complete): ").strip().lower()
    except (KeyboardInterrupt, EOFError):
        return None

    if not answer or answer == "all":
        return "all"

    # Match by slug or display name
    for slug, display, space in folders:
        if answer == slug.lower() or answer == display.lower():
            return (space, slug, display)

    # Substring match
    matches = [
        (slug, display, space) for slug, display, space in folders
        if answer in slug.lower() or answer in display.lower()
    ]
    if len(matches) == 1:
        slug, display, space = matches[0]
        return (space, slug, display)
    if len(matches) > 1:
        names = ", ".join(d for _, d, _ in matches)
        console.print(f"[yellow]Ambiguous — did you mean: {names}?[/yellow]")
        return None

    console.print(f"[yellow]No folder matching '{answer}'.[/yellow]")
    return None


def _agent_connect(config: NotelyConfig, folder_query: str = "") -> None:
    """Connect services to a folder (or globally).

    /connect           → prompt for folder, Enter = global
    /connect all       → global (all folders)
    /connect sanity    → resolve folder, connect service
    """
    try:
        from notely_agent.api import run_connect_flow, run_connect_global
    except ImportError:
        console.print("[yellow]notely-agent not installed.[/yellow]")
        console.print("[dim]Install: pip install -e path/to/notely-agent[/dim]")
        return

    try:
        stripped = folder_query.strip()
        if not stripped:
            # No arg — prompt with folder autocomplete, Enter = global
            answer = _prompt_connect_folder(config)
            if answer is None:
                console.print("[dim]Cancelled.[/dim]")
            elif answer == "all":
                run_connect_global()
            else:
                space, group_slug, display_name = answer
                run_connect_flow(space, group_slug, display_name)
            return
        if stripped.lower() == "all":
            run_connect_global()
            return
        # Direct folder arg — resolve and connect
        match = _fuzzy_match_folder(config, stripped)
        if not match:
            console.print(f"[yellow]No folder matching '{stripped}'.[/yellow]")
            return
        space, group_slug, display_name, _ = match
        run_connect_flow(space, group_slug, display_name)
    except KeyboardInterrupt:
        # Restore terminal state — Composio SDK may leave it dirty on interrupt
        try:
            import subprocess
            subprocess.run(["stty", "sane"], stdin=sys.stdin, check=False)
        except Exception:
            pass
        console.print("\n[yellow]Aborted.[/yellow]")


def _agent_disconnect(config: NotelyConfig, arg: str) -> None:
    """Disconnect a service from a folder.

    /disconnect sanity linear  → remove linear from sanity account
    """
    try:
        from notely_agent.api import AgentConfig, run_disconnect
    except ImportError:
        console.print("[yellow]notely-agent not installed.[/yellow]")
        return

    parts = arg.strip().split()
    if len(parts) != 2:
        console.print("[yellow]Usage: /disconnect FOLDER SERVICE[/yellow]")
        console.print("[dim]  e.g. /disconnect sanity linear[/dim]")
        return

    folder_query, service = parts[0], parts[1].lower()

    # Resolve folder to account
    match = _fuzzy_match_folder(config, folder_query)
    if not match:
        console.print(f"[yellow]No folder matching '{folder_query}'.[/yellow]")
        return

    space, group_slug, display_name, _ = match

    agent_config = AgentConfig()
    folder_path = f"{space}/{group_slug}"
    acct = agent_config.resolve_account(space=space, folder=folder_path)
    if not acct:
        console.print(f"[yellow]No account found for {display_name}.[/yellow]")
        return

    if service not in acct.services:
        connected = ", ".join(sorted(acct.services.keys())) or "none"
        console.print(f"[yellow]{service} is not connected for {display_name}.[/yellow]")
        console.print(f"[dim]Connected: {connected}[/dim]")
        return

    if run_disconnect(acct.name, service):
        console.print(f"[green]{service} disconnected from {display_name}.[/green]")
    else:
        console.print(f"[red]Failed to disconnect {service}.[/red]")


def _make_chat_tool_handler(
    config: NotelyConfig,
    db: Database,
    space: str,
    group_slug: str,
):
    """Create a tool handler closure for chat_about_notes.

    Executes search_notes (hybrid FTS + vector) and get_note_body
    (reads full note from markdown).
    """
    def handler(tool_name: str, tool_input: dict) -> dict:
        if tool_name == "search_notes":
            query = tool_input["query"]
            results = []

            # FTS search scoped to group
            try:
                fts_results = db.search_notes_in_group(
                    query, space, group_slug, limit=5
                )
                for r in fts_results:
                    results.append({
                        "id": r["id"], "title": r["title"],
                        "date": r["date"], "summary": r["summary"],
                        "source": "keyword",
                    })
            except Exception:
                pass

            # Vector search scoped to group
            try:
                from ...vectors import get_vector_store
                vec = get_vector_store(config)
                vec_results = vec.search_notes(
                    query, limit=5, space=space, group_slug=group_slug,
                )
                seen_ids = {r["id"] for r in results}
                for r in vec_results:
                    if r["note_id"] not in seen_ids:
                        results.append({
                            "id": r["note_id"], "title": r["title"],
                            "date": r["date"], "summary": r["summary"],
                            "source": "semantic",
                        })
            except Exception:
                pass

            return {"results": results[:8]}

        elif tool_name == "get_note_body":
            note_id = tool_input["note_id"]
            row = db.get_note(note_id)
            if not row:
                return {"error": f"Note not found: {note_id}"}
            from ...storage import read_note
            note = read_note(config, row["file_path"])
            if not note:
                return {"error": f"Note file not found: {row['file_path']}"}
            # Load action items from DB
            note_actions = db.get_note_todos(note_id)
            return {
                "id": note.id,
                "title": note.title,
                "date": note.date,
                "body": note.body,
                "action_items": [
                    {"owner": a["owner"], "task": a["task"], "due": a["due"],
                     "status": a["status"]}
                    for a in note_actions
                ],
            }

        return {"error": f"Unknown tool: {tool_name}"}

    return handler


def _chat_mode(config: NotelyConfig, query: str) -> None:
    """Enter chat mode for a specific folder."""
    from ...ai import chat_about_notes, CHAT_SMALL_FOLDER_THRESHOLD

    # Step 1: Fuzzy match folder
    match = _fuzzy_match_folder(config, query)
    if not match:
        if query.strip():
            console.print(f"[yellow]No folder matching '{query}'.[/yellow]")
        else:
            console.print("[yellow]No folders found. Add notes first.[/yellow]")
        return

    space, group_slug, display_name, subgroup_field = match

    with Database(config.db_path) as db:
        db.initialize()

        # Step 2: Load folder context
        folder_context = db.get_folder_context(space, group_slug)

        note_count = len(folder_context["notes"])
        todo_count = len(folder_context["open_todos"])

        if note_count == 0:
            console.print(f"[yellow]No notes in {display_name}.[/yellow]")
            return

        # For small folders, pre-load all note bodies
        if note_count < CHAT_SMALL_FOLDER_THRESHOLD:
            from ...storage import read_note
            for note_info in folder_context["notes"]:
                note = read_note(config, note_info["file_path"])
                if note:
                    note_info["body"] = note.body
                    # Load action items from DB
                    note_actions = db.get_note_todos(note_info["id"])
                    note_info["action_items"] = [
                        {"owner": a["owner"], "task": a["task"], "due": a["due"],
                         "status": a["status"]}
                        for a in note_actions
                    ]

        # Step 3: Show banner
        sub_str = ""
        subfolders = folder_context.get("subfolders", [])
        if subfolders:
            sub_names = [s.get("display_name") or s.get("path", "") for s in subfolders]
            sub_str = f"\nFolders: {', '.join(sub_names)}"

        console.print(Panel(
            f"[bold]Chat: {display_name}[/bold]\n"
            f"{note_count} note(s), {todo_count} open todo(s){sub_str}\n\n"
            f"Ask questions or request deliverables.\n"
            f"Type [cyan]/back[/cyan] to return to capture mode.",
            border_style="cyan",
            width=60,
        ))

        # References from DB (loaded by get_folder_context)
        folder_refs = folder_context.get("references", [])

        # Step 4: Chat loop
        conversation_history: list[dict] = []
        tool_handler = _make_chat_tool_handler(
            config, db, space, group_slug
        )
        prompt_label = group_slug[:15]

        import time
        last_interrupt = 0.0

        while True:
            try:
                user_input = Prompt.ask(f"\n[cyan]notely-chat ({prompt_label})[/cyan]")
            except EOFError:
                console.print("\n[dim]Exiting chat.[/dim]")
                break
            except KeyboardInterrupt:
                now = time.monotonic()
                if last_interrupt > 0 and now - last_interrupt < 5.0:
                    console.print("\n[dim]Exiting chat.[/dim]")
                    break
                last_interrupt = now
                console.print("\n[dim]Ctrl+C again to exit chat.[/dim]")
                continue

            if not user_input.strip():
                continue

            stripped = user_input.strip().lower()
            if stripped in ("/back", "/exit", "/quit", "/q"):
                console.print("[dim]Back to notely.[/dim]")
                break

            # Send to AI
            console.print("[dim]Thinking...[/dim]")
            try:
                response_text, conversation_history = chat_about_notes(
                    user_input,
                    conversation_history,
                    folder_context,
                    display_name,
                    tool_handler,
                    user_name=config.user_name,
                    references=folder_refs,
                )
                console.print()
                from rich.markdown import Markdown
                console.print(Markdown(response_text))
            except KeyboardInterrupt:
                console.print("\n[yellow]Interrupted.[/yellow]")
            except Exception as e:
                console.print(f"\n[red]Chat error: {e}[/red]")
