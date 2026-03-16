"""Interactive todo mode — /todo enters a sub-mode for managing action items."""

from __future__ import annotations

from datetime import date

from prompt_toolkit import PromptSession

from ...config import NotelyConfig
from ...db import Database, safe_json_loads
from ._shared import console


def _todo_mode(
    config: NotelyConfig,
    completer: "_SlashCompleter",
    initial_folder: str | None = None,
) -> None:
    """Interactive todo management mode. Called when user types /todo with no subcommand."""
    from ._completers import _SlashCompleter, _TodoCommandCompleter, _TodoItemCompleter

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
            raw = db.get_open_todos()

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

    def _display(
        show_all: bool = False, folder_filter: str | None = None,
        quiet: bool = False,
    ) -> None:
        loaded = _load_items(show_all=show_all, folder_filter=folder_filter)
        if not loaded:
            if not quiet:
                if show_all:
                    console.print("[dim]No open todos.[/dim]")
                else:
                    console.print("[dim]No open todos for you.[/dim]")
            return
        _render_grouped(loaded, show_all=show_all)

    def _render_grouped(items_list: list[dict], show_all: bool = False) -> None:
        from ...timer import elapsed_since, get_running_timer_for_todo

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
            "done · add · edit · today · show · timer · move · all · # to view · q[/dim]"
        )

    # Initial display — try your todos first
    if initial_folder:
        # Check how many total todos exist in this folder
        _load_items(show_all=True, folder_filter=initial_folder)
        all_count = len(items)
        # Now load just yours
        _display(folder_filter=initial_folder, quiet=True)
        user = config.user_name or "you"
        if items:
            # You have todos here — show them
            if all_count > len(items):
                console.print(f"[dim]  {all_count - len(items)} more assigned to others. 'all' to see.[/dim]")
        elif all_count > 0:
            # No personal todos but others exist
            console.print(f"[dim]  No open todos for {user}. {all_count} assigned to others.[/dim]")
        else:
            console.print("[dim]  No open todos here. Try 'add' to create one.[/dim]")
    else:
        _display()
        if not items:
            console.print("[dim]  Try 'all' to see everyone's, or 'add' to create one.[/dim]")

    # Build prompt label
    if initial_folder:
        from ._shared import _fuzzy_match_folder
        match = _fuzzy_match_folder(config, initial_folder)
        prompt_label = f"notely-todo ({match[2]})" if match else f"notely-todo ({initial_folder})"
    else:
        prompt_label = "notely-todo"

    # Build completer for item selection
    todo_completer = _TodoItemCompleter(items, today_ids)

    import time
    last_interrupt = 0.0

    from ._shared import _get_all_folders
    folders = _get_all_folders(config)
    cmd_completer = _TodoCommandCompleter(
        folders=folders, has_default_folder=bool(initial_folder),
    )
    session: PromptSession = PromptSession(
        completer=cmd_completer, complete_while_typing=True,
    )

    while True:
        try:
            text = session.prompt(f"\n{prompt_label}> ")
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
            _display(folder_filter=initial_folder)

        elif cmd_lower.startswith("done "):
            _todo_done_direct(config, cmd[5:].strip(), num_to_id, completer)
            _display(folder_filter=initial_folder)

        elif cmd_lower == "add":
            _todo_add(config, default_folder=initial_folder)
            _display(folder_filter=initial_folder)

        elif cmd_lower.startswith("add "):
            _todo_add(config, inline_args=cmd[4:].strip(), default_folder=initial_folder)
            _display(folder_filter=initial_folder)

        elif cmd_lower.startswith("edit "):
            _todo_edit(config, cmd[5:].strip(), num_to_id, items, completer)
            _display(folder_filter=initial_folder)

        elif cmd_lower == "today":
            _todo_today(config, items, today_ids, num_to_id, today_str)
            _display(folder_filter=initial_folder)

        elif cmd_lower == "timer":
            _todo_timer(config, items, today_ids, num_to_id)
            _display(folder_filter=initial_folder)

        elif cmd_lower.startswith("timer "):
            _todo_timer_direct(config, cmd[6:].strip(), num_to_id)
            _display(folder_filter=initial_folder)

        elif cmd_lower == "all":
            _display(show_all=True)

        elif cmd_lower.startswith("move "):
            _todo_move_direct(config, cmd[5:].strip(), num_to_id, items, completer)
            _display(folder_filter=initial_folder)

        elif cmd_lower.startswith("delete "):
            _todo_delete(config, cmd[7:].strip(), num_to_id, completer)
            _display(folder_filter=initial_folder)

        elif cmd_lower == "refresh":
            _display(folder_filter=initial_folder)

        elif cmd_lower.startswith(("show ", "filter ")) and "=" in cmd:
            # Filter by field=value: show owner=jake, filter due=2026-03
            arg = cmd.split(None, 1)[1]
            fkey, _, fval = arg.partition("=")
            fkey, fval = fkey.strip().lower(), fval.strip().lower()
            if not fval:
                console.print("[yellow]Usage: show field=value (e.g. show owner=jake)[/yellow]")
            else:
                filtered = []
                for i in items:
                    if fkey == "owner":
                        if fval in (i.get("owner") or "").lower():
                            filtered.append(i)
                    elif fkey == "due":
                        if fval in (i.get("due") or "").lower():
                            filtered.append(i)
                    elif fkey == "folder":
                        fd = i.get("_folder_display", "")
                        fk = _derive_folder_key(i)
                        if fval in fd.lower() or fval in fk.lower():
                            filtered.append(i)
                    elif fkey == "task":
                        if fval in (i.get("task") or "").lower():
                            filtered.append(i)
                if not filtered:
                    console.print(f"[yellow]No todos matching {fkey}={fval}[/yellow]")
                else:
                    _render_grouped(filtered, show_all=True)

        elif cmd.isdigit():
            # Bare number — quick actions for that item
            _todo_item_actions(config, cmd, items, today_ids, num_to_id, today_str, completer)
            _display(folder_filter=initial_folder)

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
    group = item.get("group_slug") or ""
    if space and group:
        return f"{space}/{group}"
    return space


def _derive_folder_display(item: dict) -> str:
    """Derive a human-readable folder path from a todo item.

    Returns 'space/group' format for clarity (e.g. 'clients/sanity').
    """
    # Derive from file_path first (most reliable)
    fp = item.get("file_path", "")
    if fp:
        parts = fp.split("/")
        if len(parts) >= 2:
            return "/".join(parts[:2])  # space/group
        return parts[0] if parts else ""

    # Standalone items — use space/group_slug
    space = item.get("space") or ""
    group = item.get("group_slug") or ""
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
    """Mark done with inline numbers: 'done 3', 'done 1 2 4 7', 'done 1-5'."""
    nums: list[int] = []
    for tok in text.split():
        if "-" in tok:
            try:
                lo, hi = tok.split("-", 1)
                nums.extend(range(int(lo), int(hi) + 1))
            except ValueError:
                pass
        else:
            try:
                nums.append(int(tok))
            except ValueError:
                pass
    if not nums:
        console.print(f"[yellow]Could not resolve '{text}'.[/yellow]")
        return
    for n in nums:
        item_id = num_to_id.get(n) or n
        _do_mark_done(config, item_id, completer)


def _do_mark_done(config: NotelyConfig, item_id: int, completer: "_SlashCompleter") -> None:
    """Actually mark an item done."""
    from ...storage import sync_todo_index, update_action_status

    with Database(config.db_path) as db:
        db.initialize()
        row = db.get_todo(item_id)
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


def _todo_delete(
    config: NotelyConfig,
    text: str,
    num_to_id: dict[int, int],
    completer: "_SlashCompleter",
) -> None:
    """Delete todos by number. Supports 'delete 1 3 5', 'delete 1-5', mixed."""
    from rich.prompt import Prompt

    from ...storage import sync_todo_index

    # Parse numbers: support "1 3 5", "1-5", or mixed "1-3 5 7"
    nums: list[int] = []
    for tok in text.split():
        if "-" in tok:
            try:
                lo, hi = tok.split("-", 1)
                nums.extend(range(int(lo), int(hi) + 1))
            except ValueError:
                pass
        else:
            try:
                nums.append(int(tok))
            except ValueError:
                pass
    if not nums:
        console.print("[yellow]Usage: delete N [N2 N3 ...] or delete N-M[/yellow]")
        return

    valid = [(n, num_to_id[n]) for n in nums if n in num_to_id]
    invalid = [n for n in nums if n not in num_to_id]
    if invalid:
        console.print(f"[yellow]No items for: {', '.join(str(n) for n in invalid)}[/yellow]")
    if not valid:
        return

    label = ", ".join(f"#{n}" for n, _ in valid)
    confirm = Prompt.ask(
        f"Delete {len(valid)} todo(s) ({label})? This cannot be undone",
        choices=["y", "n"], default="n",
    )
    if confirm != "y":
        console.print("[dim]Cancelled.[/dim]")
        return

    deleted = 0
    with Database(config.db_path) as db:
        db.initialize()
        for _, row_id in valid:
            if db.delete_reference(row_id):
                deleted += 1
        if deleted:
            sync_todo_index(config, db)
    console.print(f"[green]Deleted {deleted} todo(s).[/green]")
    completer.invalidate_todos()


def _todo_add(
    config: NotelyConfig,
    inline_args: str = "",
    default_folder: str | None = None,
) -> None:
    """Add a new todo via universal_add. Resolves folder first, then delegates.

    Supports: 'add FOLDER free-form text' or 'add free-form text' (uses default_folder).
    """
    from ._shared import _fuzzy_match_folder

    space = ""
    group_slug = ""
    raw_input = inline_args

    if inline_args:
        # Try first word as folder, rest as raw input
        parts = inline_args.split(None, 1)
        match = _fuzzy_match_folder(config, parts[0])
        if match:
            space = match[0]
            group_slug = match[1] or ""
            raw_input = parts[1].strip() if len(parts) > 1 else ""
        # else: whole string is task input — folder resolved below

    # Fall back to scoped folder
    if not space and default_folder:
        match = _fuzzy_match_folder(config, default_folder)
        if match:
            space = match[0]
            group_slug = match[1] or ""

    # Prompt for folder if still unresolved
    if not space:
        from prompt_toolkit import PromptSession

        from ._completers import _FolderPathCompleter
        from ._shared import _get_all_folders
        folders = _get_all_folders(config)
        try:
            folder_input = PromptSession(
                completer=_FolderPathCompleter(folders),
            ).prompt("  Folder: ", default="")
        except (EOFError, KeyboardInterrupt):
            folder_input = ""
        if folder_input.strip():
            match = _fuzzy_match_folder(config, folder_input.strip())
            if match:
                space = match[0]
                group_slug = match[1] or ""

    # Delegate to universal_add
    from ...storage import universal_add
    with Database(config.db_path) as db:
        db.initialize()
        universal_add(
            config, db, "todo",
            raw_input=raw_input,
            space=space, group_slug=group_slug,
            default_owner=config.user_name or "",
        )


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
                db.flag_todo_today(item_id, today_str)
                task = next((i["task"] for i in items if i["id"] == item_id), part)
                flagged.append(task)

    if flagged:
        console.print(f"[green]✓ Flagged:[/green] {', '.join(flagged)}")


def _todo_show_due(
    items: list[dict], num_to_id: dict[int, int], config: NotelyConfig
) -> None:
    """Display items sorted by due date."""
    from ...timer import elapsed_since, get_running_timer_for_todo

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
        items = db.get_open_todos()
    for i in items:
        i["_folder_display"] = _derive_folder_display(i)

    _start_timer_for_item(config, item_id, items)


def _start_timer_for_item(
    config: NotelyConfig, item_id: int, items: list[dict]
) -> None:
    """Start a timer linked to a todo item."""
    from ...timer import get_running_timer_for_todo, start_timer

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
            row = db.get_todo(item_id)
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
                db.flag_todo_today(item_id, today_str)
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
    """Show current record and actions for a numbered item."""
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

    # Show current record fields
    console.print()
    console.print(f"  [bold]task:[/bold] {task}")
    if item.get("owner"):
        console.print(f"  [bold]owner:[/bold] {item['owner']}")
    if item.get("due"):
        console.print(f"  [bold]due:[/bold] {item['due']}")
    if folder:
        console.print(f"  [dim]folder: {folder}[/dim]")
    if is_today:
        console.print(f"  [yellow]★ today[/yellow]")

    from ...timer import elapsed_since, get_running_timer_for_todo
    running = get_running_timer_for_todo(config, item_id)
    if running:
        console.print(f"  [green]⏱ Timer running: {elapsed_since(running['start'])}[/green]")

    # Build action prompt
    actions = r"\[e]dit / \[r]evise / \[d]one / \[x] delete"
    if not is_today:
        actions += r" / \[t]oday"
    if running:
        actions += r" / \[s]top timer"
    else:
        actions += r" / t\[i]mer"

    from rich.prompt import Prompt
    try:
        choice = Prompt.ask(f"  {actions}", default="")
    except (EOFError, KeyboardInterrupt):
        return

    choice = choice.strip().lower()
    if choice == "e":
        _do_edit_todo(config, item_id, item, completer)
    elif choice == "r":
        _do_revise_todo(config, item_id, item, completer)
    elif choice == "d":
        _do_mark_done(config, item_id, completer)
    elif choice == "x":
        confirm = Prompt.ask(
            f"  [red]Delete this todo? This cannot be undone[/red]",
            choices=["y", "n"], default="n",
        )
        if confirm == "y":
            from ...storage import sync_todo_index
            with Database(config.db_path) as db:
                db.initialize()
                db.delete_reference(item_id)
                sync_todo_index(config, db)
            console.print(f"[green]Deleted.[/green]")
            completer.invalidate_todos()
    elif choice == "t" and not is_today:
        with Database(config.db_path) as db:
            db.initialize()
            db.flag_todo_today(item_id, today_str)
        today_ids.add(item_id)
        console.print(f"[green]✓ Flagged for today[/green]")
    elif choice == "i" and not running:
        _start_timer_for_item(config, item_id, items)
    elif choice == "s" and running:
        from ...timer import format_duration, stop_timer
        stopped = stop_timer(config, running["id"])
        if stopped:
            mins = int(stopped.get("duration_minutes", 0))
            console.print(f"[green]⏱ Stopped:[/green] {format_duration(mins)}")


def _todo_edit(
    config: NotelyConfig,
    text: str,
    num_to_id: dict[int, int],
    items: list[dict],
    completer: "_SlashCompleter",
) -> None:
    """Edit one or more todos: 'edit 1', 'edit 1 2 3 due=tomorrow'."""
    from rich.panel import Panel

    from ...ai import parse_record_with_ai
    from ...storage import sync_todo_index, update_todo_record

    # Parse: numbers/ranges first, then instruction
    tokens = text.split()
    nums: list[int] = []
    instruction_start = 0
    for i, tok in enumerate(tokens):
        if "-" in tok and tok[0].isdigit():
            try:
                lo, hi = tok.split("-", 1)
                nums.extend(range(int(lo), int(hi) + 1))
                instruction_start = i + 1
            except ValueError:
                break
        elif tok.isdigit():
            nums.append(int(tok))
            instruction_start = i + 1
        else:
            break

    if not nums:
        console.print("[yellow]Usage: edit NUMBER [instruction][/yellow]")
        return

    instruction = " ".join(tokens[instruction_start:]).strip()

    # Resolve items
    resolved: list[tuple[int, int, dict]] = []
    for n in nums:
        item_id = num_to_id.get(n)
        if item_id is None:
            console.print(f"[yellow]No item #{n}.[/yellow]")
            continue
        item = next((it for it in items if it["id"] == item_id), None)
        if item:
            resolved.append((n, item_id, item))

    if not resolved:
        return

    # Single item, no instruction → interactive edit
    if len(resolved) == 1 and not instruction:
        _, item_id, item = resolved[0]
        _do_edit_todo(config, item_id, item, completer)
        return

    # Prompt for instruction if missing
    if not instruction:
        try:
            instruction = PromptSession().prompt("  What to change: ")
        except (EOFError, KeyboardInterrupt):
            return
        if not instruction.strip():
            return

    # Build context for AI: all items + instruction
    items_context = []
    for n, _, it in resolved:
        parts = [f"#{n}: task={it['task']}"]
        if it.get("owner"):
            parts.append(f"owner={it['owner']}")
        if it.get("due"):
            parts.append(f"due={it['due']}")
        items_context.append(" ".join(parts))

    ai_input = "Items:\n" + "\n".join(items_context) + f"\n\nInstruction: {instruction}"

    console.print("[dim]Processing...[/dim]")
    parsed = parse_record_with_ai(
        ai_input, "todo", ["task", "owner", "due"],
        db_description="Action items with owner and optional due date",
        user_name=config.user_name or "",
    )

    if not parsed:
        console.print("[yellow]Could not parse. Try again.[/yellow]")
        return

    # Preview what will change
    changes = {k: v for k, v in parsed.items() if k in ("task", "owner", "due") and v}
    if not changes:
        console.print("[dim]No changes detected.[/dim]")
        return

    change_desc = ", ".join(f"{k}={v}" for k, v in changes.items())
    item_nums = ", ".join(f"#{n}" for n, _, _ in resolved)
    console.print(Panel(
        f"[bold]Apply to {item_nums}:[/bold]\n{change_desc}",
        title="Edit todos", border_style="green",
    ))

    from rich.prompt import Prompt
    try:
        confirm = Prompt.ask(
            r"  \[Y]es, apply / \[n]o, cancel", choices=["y", "n"], default="y",
        )
    except (EOFError, KeyboardInterrupt):
        return

    if confirm != "y":
        return

    # Apply to all resolved items
    with Database(config.db_path) as db:
        db.initialize()
        for n, item_id, it in resolved:
            # For batch: skip task field unless only 1 item (task is per-item)
            update_fields = {}
            for k in ("owner", "due"):
                if k in changes:
                    update_fields[k] = changes[k]
            # Only apply task change for single-item edits
            if len(resolved) == 1 and "task" in changes:
                update_fields["task"] = changes["task"]
            if update_fields:
                update_todo_record(config, db, item_id, update_fields)

    console.print(f"[green]✓ Updated {len(resolved)} todo(s).[/green]")
    completer.invalidate_todos()


def _do_edit_todo(
    config: NotelyConfig,
    item_id: int,
    item: dict,
    completer: "_SlashCompleter",
) -> None:
    """Edit todo fields interactively — field picker loop."""
    from ...storage import sync_todo_index, update_todo_record

    fields_list = ["task", "owner", "due"]
    current = {
        "task": item.get("task", ""),
        "owner": item.get("owner", ""),
        "due": item.get("due") or "",
    }

    changed = False
    while True:
        console.print()
        for i, k in enumerate(fields_list, 1):
            console.print(f"  [bold]{i}.[/bold] {k} = {current[k]}")
        try:
            pick = PromptSession().prompt("\n  Edit field # (q to finish): ")
        except (EOFError, KeyboardInterrupt):
            break
        pick = pick.strip().lower()
        if pick in ("q", ""):
            break
        try:
            idx = int(pick) - 1
            if 0 <= idx < len(fields_list):
                key = fields_list[idx]
                try:
                    new_val = PromptSession().prompt(
                        f"  {key} = ", default=current[key],
                    )
                except (EOFError, KeyboardInterrupt):
                    continue
                new_val = new_val.strip()
                if new_val != current[key]:
                    current[key] = new_val
                    changed = True
        except ValueError:
            console.print("[yellow]Enter a number or q.[/yellow]")

    if not changed:
        return

    # Apply changes
    update_fields = {}
    if current["task"] != item.get("task", ""):
        update_fields["task"] = current["task"]
    if current["owner"] != item.get("owner", ""):
        update_fields["owner"] = current["owner"]
    if current["due"] != (item.get("due") or ""):
        update_fields["due"] = current["due"]

    if update_fields:
        with Database(config.db_path) as db:
            db.initialize()
            update_todo_record(config, db, item_id, update_fields)
        console.print(f"[green]✓ Updated.[/green]")
        completer.invalidate_todos()


def _do_revise_todo(
    config: NotelyConfig,
    item_id: int,
    item: dict,
    completer: "_SlashCompleter",
) -> None:
    """Revise a todo with AI — user describes the change, AI produces updated fields."""
    from rich.panel import Panel

    from ...ai import parse_record_with_ai
    from ...storage import sync_todo_index, update_todo_record

    # Show current state as context
    current_str = f"task={item['task']}"
    if item.get("owner"):
        current_str += f" owner={item['owner']}"
    if item.get("due"):
        current_str += f" due={item['due']}"

    try:
        instruction = PromptSession().prompt("  What to change: ")
    except (EOFError, KeyboardInterrupt):
        return
    if not instruction.strip():
        return

    # Build input: current record + instruction
    ai_input = f"Current: {current_str}\nInstruction: {instruction}"

    console.print("[dim]Revising...[/dim]")
    parsed = parse_record_with_ai(
        ai_input, "todo", ["task", "owner", "due"],
        db_description="Action items with owner and optional due date",
        user_name=config.user_name or "",
    )

    if not parsed:
        console.print("[yellow]Could not revise. Try again.[/yellow]")
        return

    # Fill unchanged fields from current
    if "task" not in parsed:
        parsed["task"] = item["task"]
    if "owner" not in parsed and item.get("owner"):
        parsed["owner"] = item["owner"]
    if "due" not in parsed and item.get("due"):
        parsed["due"] = item["due"]

    # Preview
    lines = []
    lines.append(f"[bold]task:[/bold] {parsed.get('task', '')}")
    if parsed.get("owner"):
        lines.append(f"[bold]owner:[/bold] {parsed['owner']}")
    if parsed.get("due"):
        lines.append(f"[bold]due:[/bold] {parsed['due']}")
    console.print(Panel("\n".join(lines), title="Revised todo", border_style="green"))

    from rich.prompt import Prompt
    try:
        confirm = Prompt.ask(
            r"  \[Y]es, save / \[n]o, cancel", choices=["y", "n"], default="y",
        )
    except (EOFError, KeyboardInterrupt):
        return

    if confirm != "y":
        return

    with Database(config.db_path) as db:
        db.initialize()
        update_todo_record(config, db, item_id, parsed)

    console.print(f"[green]✓ Updated.[/green]")
    completer.invalidate_todos()


def _todo_set_due(
    config: NotelyConfig,
    text: str,
    num_to_id: dict[int, int],
    items: list[dict],
    completer: "_SlashCompleter",
) -> None:
    """Set due date on an item: 'due 4 friday' or 'due 4' (prompts for date)."""
    from ...ai import parse_record_with_ai
    from ...storage import sync_todo_index, update_todo_record

    parts = text.split(None, 1)
    if not parts:
        console.print("[yellow]Usage: due NUMBER [DATE][/yellow]")
        return

    item_id = _resolve_num_or_id(parts[0], num_to_id)
    if item_id is None:
        console.print(f"[yellow]No item #{parts[0]}.[/yellow]")
        return

    item = next((i for i in items if i["id"] == item_id), None)
    if item is None:
        console.print(f"[yellow]No item #{parts[0]}.[/yellow]")
        return

    # Get date — from inline arg or prompt
    if len(parts) >= 2:
        date_text = parts[1].strip()
    else:
        try:
            date_text = PromptSession().prompt(
                f"  Due date for '{item['task']}': ",
                default=item.get("due") or "",
            )
        except (EOFError, KeyboardInterrupt):
            return
        if not date_text.strip():
            return

    # Parse with AI for natural language dates
    parsed = parse_record_with_ai(
        f"due={date_text}", "todo", ["task", "owner", "due"],
        db_description="Action items with owner and optional due date",
        user_name=config.user_name or "",
    )

    due_val = parsed.get("due", date_text)

    with Database(config.db_path) as db:
        db.initialize()
        update_todo_record(config, db, item_id, {"due": due_val})

    console.print(f"[green]✓ Due date set to {due_val}:[/green] {item['task']}")
    completer.invalidate_todos()


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

    from ...storage import sync_todo_index, update_action_owner
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
    from ...storage import sync_todo_index
    from ._shared import _fuzzy_match_folder

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
        db.update_todo_folder(item_id, space, group_slug or None)
        sync_todo_index(config, db)

    folder_path = f"{space}/{group_slug}" if group_slug else space
    console.print(f"[green]✓ Moved to {folder_path}:[/green] {task}")
    completer.invalidate_todos()


def _do_assign(
    config: NotelyConfig, item_id: int, task: str, completer: "_SlashCompleter"
) -> None:
    """Assign a todo to another owner."""
    from ...storage import sync_todo_index, update_action_owner

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
    from prompt_toolkit import PromptSession

    from ._completers import _SlashCompleter
