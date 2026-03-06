"""Interactive todo mode — /todo enters a sub-mode for managing action items."""

from __future__ import annotations

from datetime import date

from prompt_toolkit import PromptSession

from ...config import NotelyConfig
from ...db import Database, safe_json_loads

from ._shared import console


def _todo_mode(config: NotelyConfig, completer: "_SlashCompleter") -> None:
    """Interactive todo management mode. Called when user types /todo with no subcommand."""
    from ._completers import _TodoItemCompleter, _SlashCompleter, _TodoCommandCompleter

    today_str = date.today().isoformat()

    # Session-local number→id mapping, rebuilt on each display
    num_to_id: dict[int, int] = {}
    items: list[dict] = []
    today_ids: set[int] = set()

    def _load_items(
        show_all: bool = False, folder_filter: str | None = None
    ) -> list[dict]:
        nonlocal items, today_ids, num_to_id
        with Database(config.db_path) as db:
            db.initialize()
            raw = db.get_open_action_items()

        # Filter by owner unless show_all
        if not show_all:
            owner_lower = (config.user_name or "").lower()
            if owner_lower:
                raw = [i for i in raw if owner_lower in (i.get("owner") or "").lower()]

        # Folder filter
        if folder_filter:
            fl = folder_filter.lower()
            filtered = []
            for i in raw:
                folder_display = _derive_folder_display(i)
                if fl in folder_display.lower() or fl in _derive_folder_key(i).lower():
                    filtered.append(i)
            raw = filtered

        # Add _folder_display to each item
        for i in raw:
            i["_folder_display"] = _derive_folder_display(i)

        today_ids = {
            i["id"] for i in raw
            if i.get("flagged_date") == today_str
        }

        items = raw
        num_to_id.clear()
        return raw

    def _display(show_all: bool = False, folder_filter: str | None = None) -> None:
        loaded = _load_items(show_all=show_all, folder_filter=folder_filter)
        if not loaded:
            if folder_filter:
                console.print(f"[dim]No open todos matching '{folder_filter}'.[/dim]")
            elif show_all:
                console.print("[dim]No open todos.[/dim]")
            else:
                console.print("[dim]No open todos for you.[/dim]")
            return
        _render_grouped(loaded, show_all=show_all)

    def _render_grouped(items_list: list[dict], show_all: bool = False) -> None:
        from ...timer import get_running_timer_for_todo, elapsed_since

        # Separate today items from rest
        today_items = [i for i in items_list if i["id"] in today_ids]
        rest = [i for i in items_list if i["id"] not in today_ids]

        # Group rest by folder
        by_folder: dict[str, list[dict]] = {}
        for i in rest:
            key = i["_folder_display"] or "Unfiled"
            by_folder.setdefault(key, []).append(i)

        num = 1
        console.print()

        # Today section
        if today_items:
            console.print("  [bold yellow]★ Today[/bold yellow]")
            console.print("  [dim]─────────────────────────────────────[/dim]")
            for i in today_items:
                num_to_id[num] = i["id"]
                meta_parts = _build_meta_parts(i, config, show_all)
                # Check running timer
                running = get_running_timer_for_todo(config, i["id"])
                if running:
                    meta_parts.append(f"[green]⏱ {elapsed_since(running['start'])}[/green]")
                meta_str = f"  {' · '.join(meta_parts)}" if meta_parts else ""
                console.print(f"    [bold]{num}.[/bold] {i['task']}{meta_str}")
                num += 1
            console.print()

        # Folder groups
        for folder_name in sorted(by_folder.keys()):
            folder_items = by_folder[folder_name]
            console.print(f"  [bold]{folder_name}[/bold]")
            console.print("  [dim]─────────────────────────────────────[/dim]")
            for i in folder_items:
                num_to_id[num] = i["id"]
                meta_parts = _build_meta_parts(i, config, show_all)
                running = get_running_timer_for_todo(config, i["id"])
                if running:
                    meta_parts.append(f"[green]⏱ {elapsed_since(running['start'])}[/green]")
                if i["id"] in today_ids:
                    meta_parts.insert(0, "[yellow]today[/yellow]")
                meta_str = f"  {' · '.join(meta_parts)}" if meta_parts else ""
                console.print(f"    [bold]{num}.[/bold] {i['task']}{meta_str}")
                num += 1
            console.print()

        label = "open" if show_all else "open for you"
        console.print(
            f"[dim]  {len(items_list)} {label} — "
            "done · add · today · due · timer · assign · move · plan · all · q[/dim]"
        )

    # Initial display
    _display()

    if not items:
        return

    # Build completer for item selection
    todo_completer = _TodoItemCompleter(items, today_ids)

    import time
    last_interrupt = 0.0

    from ._shared import _get_all_folders
    folders = _get_all_folders(config)
    cmd_completer = _TodoCommandCompleter(folders=folders)
    session: PromptSession = PromptSession(
        completer=cmd_completer, complete_while_typing=True,
    )

    while True:
        try:
            text = session.prompt("\nnotely-todo> ")
        except EOFError:
            break
        except KeyboardInterrupt:
            now = time.monotonic()
            if last_interrupt > 0 and now - last_interrupt < 5.0:
                break
            last_interrupt = now
            console.print("\n[dim]Ctrl+C again to exit todo mode.[/dim]")
            continue

        cmd = text.strip()
        if not cmd:
            continue

        cmd_lower = cmd.lower()

        if cmd_lower in ("q", "/back", "/quit", "/exit"):
            break

        elif cmd_lower == "done":
            _todo_done(config, items, today_ids, num_to_id, completer)
            _display()

        elif cmd_lower.startswith("done "):
            _todo_done_direct(config, cmd[5:].strip(), num_to_id, completer)
            _display()

        elif cmd_lower == "add":
            _todo_add(config)
            _display()

        elif cmd_lower.startswith("add "):
            _todo_add(config, inline_args=cmd[4:].strip())
            _display()

        elif cmd_lower == "today":
            _todo_today(config, items, today_ids, num_to_id, today_str)
            _display()

        elif cmd_lower == "due":
            _todo_show_due(items, num_to_id, config)

        elif cmd_lower == "timer":
            _todo_timer(config, items, today_ids, num_to_id)
            _display()

        elif cmd_lower.startswith("timer "):
            _todo_timer_direct(config, cmd[6:].strip(), num_to_id)
            _display()

        elif cmd_lower == "plan":
            _todo_plan(config, items, num_to_id, today_str)
            _display()

        elif cmd_lower == "all":
            _display(show_all=True)

        elif cmd_lower.startswith("assign "):
            _todo_assign_direct(config, cmd[7:].strip(), num_to_id, items, completer)
            _display()

        elif cmd_lower.startswith("move "):
            _todo_move_direct(config, cmd[5:].strip(), num_to_id, items, completer)
            _display()

        elif cmd_lower == "refresh":
            _display()

        elif cmd.isdigit():
            # Bare number — quick actions for that item
            _todo_item_actions(config, cmd, items, today_ids, num_to_id, today_str, completer)
            _display()

        else:
            # Try as folder filter
            _display(folder_filter=cmd)

        # Refresh completer with latest items
        todo_completer = _TodoItemCompleter(items, today_ids)


def _derive_folder_key(item: dict) -> str:
    """Derive a folder key from file_path or standalone fields."""
    fp = item.get("file_path", "")
    if fp:
        parts = fp.split("/")
        if len(parts) >= 2:
            return "/".join(parts[:2])  # space/group
        return parts[0] if parts else ""
    space = item.get("space") or ""
    group = item.get("group_name") or ""
    if space and group:
        return f"{space}/{group}"
    return space


def _derive_folder_display(item: dict) -> str:
    """Derive a human-readable folder path from an action item.

    Returns 'space/group' format for clarity (e.g. 'clients/sanity').
    """
    # Derive from file_path first (most reliable)
    fp = item.get("file_path", "")
    if fp:
        parts = fp.split("/")
        if len(parts) >= 2:
            return "/".join(parts[:2])  # space/group
        return parts[0] if parts else ""

    # Standalone items — use space/group_name
    space = item.get("space") or ""
    group = item.get("group_name") or ""
    if space and group:
        return f"{space}/{group}"
    return space if space else ""


def _build_meta_parts(
    item: dict, config: NotelyConfig, show_all: bool
) -> list[str]:
    """Build metadata parts for display (owner, due, etc.)."""
    parts: list[str] = []
    owner = item.get("owner") or ""
    if show_all and owner:
        parts.append(f"[cyan]{owner}[/cyan]")
    if item.get("due"):
        parts.append(f"due [yellow]{item['due']}[/yellow]")
    return parts


def _resolve_num_or_id(text: str, num_to_id: dict[int, int]) -> int | None:
    """Resolve a session number or DB id to a DB id."""
    try:
        n = int(text)
    except ValueError:
        return None
    # Check session number first
    if n in num_to_id:
        return num_to_id[n]
    # Check if it's a direct DB id
    return n


def _todo_done(
    config: NotelyConfig,
    items: list[dict],
    today_ids: set[int],
    num_to_id: dict[int, int],
    completer: "_SlashCompleter",
) -> None:
    """Prompt to mark a todo done."""
    from ._completers import _TodoItemCompleter

    if not items:
        console.print("[dim]No items to complete.[/dim]")
        return

    todo_completer = _TodoItemCompleter(items, today_ids)
    session: PromptSession = PromptSession(completer=todo_completer)
    try:
        text = session.prompt("  Mark done: ")
    except (EOFError, KeyboardInterrupt):
        return

    text = text.strip()
    if not text:
        return

    item_id = _resolve_num_or_id(text, num_to_id)
    if item_id is None:
        console.print(f"[yellow]Could not resolve '{text}'.[/yellow]")
        return

    _do_mark_done(config, item_id, completer)


def _todo_done_direct(
    config: NotelyConfig,
    text: str,
    num_to_id: dict[int, int],
    completer: "_SlashCompleter",
) -> None:
    """Mark done with inline number/id: 'done 3'."""
    item_id = _resolve_num_or_id(text, num_to_id)
    if item_id is None:
        console.print(f"[yellow]Could not resolve '{text}'.[/yellow]")
        return
    _do_mark_done(config, item_id, completer)


def _do_mark_done(config: NotelyConfig, item_id: int, completer: "_SlashCompleter") -> None:
    """Actually mark an item done."""
    from ...storage import update_action_status, sync_todo_index

    with Database(config.db_path) as db:
        db.initialize()
        row = db.get_action_item(item_id)
        if not row:
            console.print(f"[red]No todo with ID {item_id}.[/red]")
            return
        if row["status"] == "done":
            console.print(f"[yellow]Already done:[/yellow] {row['task']}")
            return
        update_action_status(config, db, item_id, "done")
        sync_todo_index(config, db)
        console.print(f"[green]✓ Done:[/green] {row['task']}")
    completer.invalidate_todos()


def _todo_add(config: NotelyConfig, inline_args: str = "") -> None:
    """Add a new todo. Supports inline: 'add FOLDER TASK' or stepped prompts."""
    from prompt_toolkit import PromptSession
    from ...dates import parse_due_date
    from ._shared import _get_all_folders, _fuzzy_match_folder

    space = None
    group_name = None
    folder_display = ""
    task = ""

    if inline_args:
        # Try first word as folder, rest as task
        parts = inline_args.split(None, 1)
        match = _fuzzy_match_folder(config, parts[0])
        if match:
            space = match[0]
            group_name = match[1] or None
            folder_display = match[2]
            task = parts[1].strip() if len(parts) > 1 else ""
        else:
            # Not a folder — treat the whole thing as the task
            task = inline_args

    if not task:
        try:
            task = PromptSession().prompt("  Task: ")
        except (EOFError, KeyboardInterrupt):
            return
        task = task.strip()
        if not task:
            return

    if not space:
        # Folder (optional, with hierarchical drill-down)
        from ._completers import _FolderPathCompleter
        folders = _get_all_folders(config)
        folder_completer = _FolderPathCompleter(folders)
        try:
            folder_input = PromptSession(completer=folder_completer).prompt(
                "  Folder: ", default=""
            )
        except (EOFError, KeyboardInterrupt):
            folder_input = ""
        folder_input = folder_input.strip()

        if folder_input:
            match = _fuzzy_match_folder(config, folder_input)
            if match:
                space = match[0]
                group_name = match[1] or None
                folder_display = match[2]

    # Due date (optional)
    try:
        due_input = PromptSession().prompt("  Due: ", default="")
    except (EOFError, KeyboardInterrupt):
        due_input = ""
    due = parse_due_date(due_input) if due_input.strip() else None

    # Create
    with Database(config.db_path) as db:
        db.initialize()
        from ...storage import sync_todo_index
        item_id = db.add_standalone_action_item(
            owner=config.user_name or "",
            task=task,
            due=due,
            space=space,
            group_name=group_name,
        )
        sync_todo_index(config, db)

    parts = [f"[green]Added:[/green] {task}"]
    if folder_display:
        parts.append(f"({folder_display})")
    if due:
        parts.append(f"due {due}")
    console.print(" ".join(parts))


def _todo_today(
    config: NotelyConfig,
    items: list[dict],
    today_ids: set[int],
    num_to_id: dict[int, int],
    today_str: str,
) -> None:
    """Flag items for today, or show today's items."""
    if not items:
        console.print("[dim]No items to flag.[/dim]")
        return

    # If there are already today items, show just those and offer to add more
    if today_ids:
        console.print("\n[bold yellow]★ Today's focus:[/bold yellow]")
        for i in items:
            if i["id"] in today_ids:
                console.print(f"  • {i['task']}")
        console.print()

    # Prompt to flag more
    try:
        text = PromptSession().prompt(
            "  Flag for today (numbers, comma-separated, Enter to skip): "
        )
    except (EOFError, KeyboardInterrupt):
        return

    text = text.strip()
    if not text:
        return

    # Parse comma-separated numbers
    flagged = []
    with Database(config.db_path) as db:
        db.initialize()
        for part in text.split(","):
            part = part.strip()
            item_id = _resolve_num_or_id(part, num_to_id)
            if item_id is not None:
                db.flag_today(item_id, today_str)
                task = next((i["task"] for i in items if i["id"] == item_id), part)
                flagged.append(task)

    if flagged:
        console.print(f"[green]✓ Flagged:[/green] {', '.join(flagged)}")


def _todo_show_due(
    items: list[dict], num_to_id: dict[int, int], config: NotelyConfig
) -> None:
    """Display items sorted by due date."""
    from ...timer import get_running_timer_for_todo, elapsed_since

    if not items:
        console.print("[dim]No open todos.[/dim]")
        return

    with_due = sorted(
        [i for i in items if i.get("due")],
        key=lambda i: i["due"],
    )
    without_due = [i for i in items if not i.get("due")]

    num = 1
    num_to_id.clear()
    console.print()

    if with_due:
        console.print("  [bold]Upcoming[/bold]")
        console.print("  [dim]─────────────────────────────────────[/dim]")
        for i in with_due:
            num_to_id[num] = i["id"]
            meta_parts = [f"[cyan]{i.get('owner', '')}[/cyan]"]
            meta_parts.append(f"[yellow]{i['due']}[/yellow]")
            folder = i.get("_folder_display", "")
            if folder:
                meta_parts.append(f"[dim]{folder}[/dim]")
            running = get_running_timer_for_todo(config, i["id"])
            if running:
                meta_parts.append(f"[green]⏱ {elapsed_since(running['start'])}[/green]")
            console.print(f"    [bold]{num}.[/bold] {i['task']}  {' · '.join(meta_parts)}")
            num += 1
        console.print()

    if without_due:
        console.print("  [bold]No due date[/bold]")
        console.print("  [dim]─────────────────────────────────────[/dim]")
        for i in without_due:
            num_to_id[num] = i["id"]
            meta_parts = [f"[cyan]{i.get('owner', '')}[/cyan]"]
            folder = i.get("_folder_display", "")
            if folder:
                meta_parts.append(f"[dim]{folder}[/dim]")
            running = get_running_timer_for_todo(config, i["id"])
            if running:
                meta_parts.append(f"[green]⏱ {elapsed_since(running['start'])}[/green]")
            console.print(f"    [bold]{num}.[/bold] {i['task']}  {' · '.join(meta_parts)}")
            num += 1
        console.print()


def _todo_timer(
    config: NotelyConfig,
    items: list[dict],
    today_ids: set[int],
    num_to_id: dict[int, int],
) -> None:
    """Start a timer against a todo item."""
    from ._completers import _TodoItemCompleter

    if not items:
        console.print("[dim]No items to time.[/dim]")
        return

    todo_completer = _TodoItemCompleter(items, today_ids)
    try:
        text = PromptSession(completer=todo_completer).prompt("  Start timer: ")
    except (EOFError, KeyboardInterrupt):
        return

    text = text.strip()
    if not text:
        return

    item_id = _resolve_num_or_id(text, num_to_id)
    if item_id is None:
        console.print(f"[yellow]Could not resolve '{text}'.[/yellow]")
        return

    _start_timer_for_item(config, item_id, items)


def _todo_timer_direct(
    config: NotelyConfig, text: str, num_to_id: dict[int, int]
) -> None:
    """Start timer with inline number: 'timer 3'."""
    item_id = _resolve_num_or_id(text, num_to_id)
    if item_id is None:
        console.print(f"[yellow]Could not resolve '{text}'.[/yellow]")
        return

    # Need items list for task text — reload from DB
    with Database(config.db_path) as db:
        db.initialize()
        items = db.get_open_action_items()
    for i in items:
        i["_folder_display"] = _derive_folder_display(i)

    _start_timer_for_item(config, item_id, items)


def _start_timer_for_item(
    config: NotelyConfig, item_id: int, items: list[dict]
) -> None:
    """Start a timer linked to a todo item."""
    from ...timer import start_timer, get_running_timer_for_todo

    # Check if already running
    running = get_running_timer_for_todo(config, item_id)
    if running:
        from ...timer import elapsed_since
        console.print(
            f"[yellow]Timer already running:[/yellow] {running['description']} "
            f"({elapsed_since(running['start'])})"
        )
        return

    # Find item details
    item = next((i for i in items if i["id"] == item_id), None)
    if item is None:
        with Database(config.db_path) as db:
            db.initialize()
            row = db.get_action_item(item_id)
        if not row:
            console.print(f"[red]No todo with ID {item_id}.[/red]")
            return
        task = row["task"]
        folder = ""
    else:
        task = item["task"]
        folder = _derive_folder_key(item)

    start_timer(config, folder, task, todo_id=item_id)
    console.print(f"[green]⏱ Timer started:[/green] {task}")


def _todo_plan(
    config: NotelyConfig,
    items: list[dict],
    num_to_id: dict[int, int],
    today_str: str,
) -> None:
    """Pick items for today's focus from the full list."""
    if not items:
        console.print("[dim]No items to plan.[/dim]")
        return

    console.print("\n  [bold]Pick today's focus[/bold] (numbers, comma-separated):")
    for num, item_id in sorted(num_to_id.items()):
        item = next((i for i in items if i["id"] == item_id), None)
        if item:
            folder = item.get("_folder_display", "")
            due_str = f" · due {item['due']}" if item.get("due") else ""
            folder_str = f" · [dim]{folder}[/dim]" if folder else ""
            console.print(f"    {num}. {item['task']}{due_str}{folder_str}")

    try:
        text = PromptSession().prompt("  > ")
    except (EOFError, KeyboardInterrupt):
        return

    text = text.strip()
    if not text:
        return

    flagged = []
    with Database(config.db_path) as db:
        db.initialize()
        for part in text.split(","):
            part = part.strip()
            item_id = _resolve_num_or_id(part, num_to_id)
            if item_id is not None:
                db.flag_today(item_id, today_str)
                task = next((i["task"] for i in items if i["id"] == item_id), part)
                flagged.append(task)

    if flagged:
        console.print(f"[green]✓ Today:[/green] {', '.join(flagged)}")


def _todo_item_actions(
    config: NotelyConfig,
    text: str,
    items: list[dict],
    today_ids: set[int],
    num_to_id: dict[int, int],
    today_str: str,
    completer: "_SlashCompleter",
) -> None:
    """Show quick actions for a numbered item: done, today, timer."""
    item_id = _resolve_num_or_id(text, num_to_id)
    if item_id is None:
        console.print(f"[yellow]No item #{text}.[/yellow]")
        return

    item = next((i for i in items if i["id"] == item_id), None)
    if item is None:
        console.print(f"[yellow]No item #{text}.[/yellow]")
        return

    task = item["task"]
    folder = item.get("_folder_display", "")
    is_today = item_id in today_ids

    console.print(f"\n  [bold]{task}[/bold]")
    meta = []
    if folder:
        meta.append(folder)
    if item.get("due"):
        meta.append(f"due {item['due']}")
    if is_today:
        meta.append("★ today")
    if meta:
        console.print(f"  [dim]{' · '.join(meta)}[/dim]")

    from ...timer import get_running_timer_for_todo, elapsed_since
    running = get_running_timer_for_todo(config, item_id)
    if running:
        console.print(f"  [green]⏱ Timer running: {elapsed_since(running['start'])}[/green]")

    # Build action prompt
    actions = r"\[d]one / \[r]eassign"
    if not is_today:
        actions += r" / \[t]oday"
    if running:
        actions += r" / \[s]top timer"
    else:
        actions += r" / t\[i]mer"
    actions += r" / Enter to cancel"

    from rich.prompt import Prompt
    try:
        choice = Prompt.ask(f"  {actions}", default="")
    except (EOFError, KeyboardInterrupt):
        return

    choice = choice.strip().lower()
    if choice == "d":
        _do_mark_done(config, item_id, completer)
    elif choice == "r":
        _do_assign(config, item_id, task, completer)
    elif choice == "t" and not is_today:
        with Database(config.db_path) as db:
            db.initialize()
            db.flag_today(item_id, today_str)
        today_ids.add(item_id)
        console.print(f"[green]✓ Flagged for today[/green]")
    elif choice == "i" and not running:
        _start_timer_for_item(config, item_id, items)
    elif choice == "s" and running:
        from ...timer import stop_timer, format_duration
        stopped = stop_timer(config, running["id"])
        if stopped:
            mins = int(stopped.get("duration_minutes", 0))
            console.print(f"[green]⏱ Stopped:[/green] {format_duration(mins)}")


def _todo_assign_direct(
    config: NotelyConfig,
    text: str,
    num_to_id: dict[int, int],
    items: list[dict],
    completer: "_SlashCompleter",
) -> None:
    """Inline assign: 'assign 5 Jake'."""
    parts = text.split(None, 1)
    if len(parts) < 2:
        console.print("[yellow]Usage: assign NUMBER NAME[/yellow]")
        return

    item_id = _resolve_num_or_id(parts[0], num_to_id)
    if item_id is None:
        console.print(f"[yellow]No item #{parts[0]}.[/yellow]")
        return

    new_owner = parts[1].strip()
    item = next((i for i in items if i["id"] == item_id), None)
    task = item["task"] if item else f"#{item_id}"

    from ...storage import update_action_owner, sync_todo_index
    with Database(config.db_path) as db:
        db.initialize()
        update_action_owner(config, db, item_id, new_owner)
        sync_todo_index(config, db)

    console.print(f"[green]✓ Assigned to {new_owner}:[/green] {task}")
    completer.invalidate_todos()


def _todo_move_direct(
    config: NotelyConfig,
    text: str,
    num_to_id: dict[int, int],
    items: list[dict],
    completer: "_SlashCompleter",
) -> None:
    """Inline move: 'move 1 clients/sanity'."""
    from ._shared import _fuzzy_match_folder
    from ...storage import sync_todo_index

    parts = text.split(None, 1)
    if len(parts) < 2:
        console.print("[yellow]Usage: move NUMBER FOLDER[/yellow]")
        return

    item_id = _resolve_num_or_id(parts[0], num_to_id)
    if item_id is None:
        console.print(f"[yellow]No item #{parts[0]}.[/yellow]")
        return

    folder_query = parts[1].strip()
    match = _fuzzy_match_folder(config, folder_query)
    if not match:
        console.print(f"[yellow]No folder matching '{folder_query}'.[/yellow]")
        return

    space, group_slug, display_name, _ = match
    item = next((i for i in items if i["id"] == item_id), None)
    task = item["task"] if item else f"#{item_id}"

    # Check if note-linked
    if item and item.get("note_id"):
        console.print("[yellow]Can't move note-linked todos — move the note instead.[/yellow]")
        return

    with Database(config.db_path) as db:
        db.initialize()
        db.update_action_item_folder(item_id, space, group_slug or None)
        sync_todo_index(config, db)

    folder_path = f"{space}/{group_slug}" if group_slug else space
    console.print(f"[green]✓ Moved to {folder_path}:[/green] {task}")
    completer.invalidate_todos()


def _do_assign(
    config: NotelyConfig, item_id: int, task: str, completer: "_SlashCompleter"
) -> None:
    """Assign a todo to another owner."""
    from ...storage import update_action_owner, sync_todo_index

    try:
        new_owner = PromptSession().prompt("  Assign to: ")
    except (EOFError, KeyboardInterrupt):
        return

    new_owner = new_owner.strip()
    if not new_owner:
        return

    with Database(config.db_path) as db:
        db.initialize()
        update_action_owner(config, db, item_id, new_owner)
        sync_todo_index(config, db)

    console.print(f"[green]✓ Assigned to {new_owner}:[/green] {task}")
    completer.invalidate_todos()


# Avoid circular import — used only for type hints
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from ._completers import _SlashCompleter
    from prompt_toolkit import PromptSession
