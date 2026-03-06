"""Slash command handlers — todo, ideas, timer, refs, workflow, CRUD."""

from __future__ import annotations

from typing import TYPE_CHECKING

from rich.prompt import Prompt

from ...config import NotelyConfig
from ...db import Database, safe_json_loads

from ._shared import console

if TYPE_CHECKING:
    from ._completers import _SlashCompleter


def _handle_todo(config: NotelyConfig, arg: str, completer: _SlashCompleter) -> None:
    """Handle /todo subcommands: interactive mode, done, reopen."""
    parts = arg.strip().split(None, 1)
    subcmd = parts[0].lower() if parts else ""
    rest = parts[1].strip() if len(parts) > 1 else ""

    if subcmd == "done":
        if rest:
            _mark_done(config, rest)
            completer.invalidate_todos()
        else:
            console.print("[yellow]Usage: /todo done ID[/yellow]")
    elif subcmd == "reopen":
        if rest:
            _reopen_todo(config, rest)
            completer.invalidate_todos()
        else:
            console.print("[yellow]Usage: /todo reopen ID[/yellow]")
    else:
        # /todo → interactive mode
        from ._todo_mode import _todo_mode
        try:
            _todo_mode(config, completer)
        except KeyboardInterrupt:
            console.print("\n[dim]Back to notely.[/dim]")
        completer.invalidate_todos()


def _show_todos(
    config: NotelyConfig,
    show_all_owners: bool = False,
    filter_arg: str | None = None,
) -> None:
    """Show open todos. By default shows only the user's items."""
    from ...dedup import (
        build_source_refs,
        find_duplicate_clusters,
        pick_best_task,
        pick_earliest_due,
    )

    with Database(config.db_path) as db:
        db.initialize()

        space = None
        client = None
        owner = None if show_all_owners else config.user_name

        if filter_arg:
            if filter_arg in config.spaces:
                space = filter_arg
            else:
                client = filter_arg

        items = db.get_open_action_items(space=space, client=client)

        # Filter by owner in Python (get_open_action_items doesn't have owner filter)
        if owner:
            owner_lower = owner.lower()
            items = [i for i in items if owner_lower in (i.get("owner") or "").lower()]

        # Dedup check — DB stays open for merges
        clusters = find_duplicate_clusters(items)
        if clusters:
            merged_any = _handle_todo_dedup(config, db, clusters)
            if merged_any:
                # Re-fetch after merges
                items = db.get_open_action_items(space=space, client=client)
                if owner:
                    owner_lower = owner.lower()
                    items = [i for i in items if owner_lower in (i.get("owner") or "").lower()]

    if not items:
        if show_all_owners:
            console.print("[dim]No open todos.[/dim]")
        else:
            console.print("[dim]No open todos for you. Try /todo all to see everyone's.[/dim]")
        return

    label = "Your Open Todos" if not show_all_owners else "Open Todos"
    console.print(f"\n[bold]{label}[/bold]\n")

    for item in items:
        meta = safe_json_loads(item["space_metadata"])
        group = meta.get("client_display") or meta.get("client") or meta.get("category_display") or ""
        title = item.get("note_title", "(standalone)")
        if title == "(standalone)":
            from_str = ""
        elif group:
            from_str = group
        else:
            from_str = title

        # Line 1: ID + task (full text, no truncation)
        console.print(f"  [dim]#{item['id']}[/dim]  {item['task']}")

        # Line 2: metadata
        meta_parts = []
        meta_parts.append(f"[cyan]{item['owner']}[/cyan]")
        if item.get("due"):
            meta_parts.append(f"due [yellow]{item['due']}[/yellow]")
        if from_str:
            meta_parts.append(f"[dim]{from_str}[/dim]")
        console.print(f"       {' · '.join(meta_parts)}")
        console.print()

    label = "open" if show_all_owners else "open for you"
    console.print(f"[dim]{len(items)} {label} — /todo done ID to complete[/dim]")


def _handle_todo_dedup(
    config: NotelyConfig,
    db: "Database",
    clusters: list[list[dict]],
) -> bool:
    """Interactive dedup for duplicate todo clusters. Returns True if any merges happened."""
    from ...storage import handle_todo_dedup
    return handle_todo_dedup(config, db, clusters)


def _show_ideas(config: NotelyConfig, folder_arg: str = "") -> None:
    """Show ideas pipeline, optionally filtered by folder/category."""
    import json

    ideas_space = config.find_ideas_space()
    if not ideas_space:
        console.print("[dim]No ideas space configured.[/dim]")
        return

    with Database(config.db_path) as db:
        db.initialize()
        rows = db.get_notes_in_space(ideas_space, limit=15)

    if not rows:
        console.print("[dim]No ideas yet.[/dim]")
        return

    # Filter by folder/category if provided
    if folder_arg:
        fl = folder_arg.lower()
        rows = [
            r for r in rows
            if fl in (safe_json_loads(r["space_metadata"]).get("category_display", "")).lower()
            or fl in (safe_json_loads(r["space_metadata"]).get("category", "")).lower()
            or fl in r.get("title", "").lower()
        ]
        if not rows:
            console.print(f"[dim]No ideas matching '{folder_arg}'.[/dim]")
            return

    from rich.table import Table
    table = Table(show_lines=False, title="Ideas")
    table.add_column("ID", style="dim", width=10)
    table.add_column("Status", width=8)
    table.add_column("Title", min_width=30)
    table.add_column("Date", width=12)

    status_styles = {"seed": "yellow", "draft": "cyan", "used": "green"}

    for r in rows:
        meta = safe_json_loads(r["space_metadata"])
        status = meta.get("content_status", "seed")
        style = status_styles.get(status, "dim")
        table.add_row(r["id"], f"[{style}]{status}[/{style}]", r["title"], r["date"])

    console.print(table)


def _show_list(config: NotelyConfig, folder_arg: str = "") -> None:
    """Show recent notes, optionally filtered by folder."""
    from ...models import SearchFilters

    filters = None
    if folder_arg:
        from ._shared import _fuzzy_match_folder
        match = _fuzzy_match_folder(config, folder_arg)
        if match:
            space, group_slug, display_name, _ = match
            filters = SearchFilters(space=space, folder=group_slug)
        else:
            console.print(f"[yellow]No folder matching '{folder_arg}'.[/yellow]")
            return

    with Database(config.db_path) as db:
        db.initialize()
        rows = db.search(filters=filters, limit=10)

    if not rows:
        console.print("[dim]No notes yet.[/dim]")
        return

    from rich.table import Table
    title = f"Recent Notes — {display_name}" if folder_arg and filters else "Recent Notes"
    table = Table(show_lines=False, title=title)
    table.add_column("ID", style="dim", width=10)
    table.add_column("Date", width=12)
    table.add_column("Space", width=10)
    table.add_column("Title", min_width=30)

    for r in rows:
        table.add_row(r["id"], r["date"], r["space"], r["title"])

    console.print(table)


def _show_search(config: NotelyConfig, query: str) -> None:
    """Search notes. First word is tried as folder filter."""
    from ...models import SearchFilters

    filters = None
    search_query = query

    # Try first word as folder
    parts = query.split(None, 1)
    if len(parts) == 2:
        from ._shared import _fuzzy_match_folder
        match = _fuzzy_match_folder(config, parts[0])
        if match:
            space, group_slug, display_name, _ = match
            filters = SearchFilters(space=space, folder=group_slug)
            search_query = parts[1]

    with Database(config.db_path) as db:
        db.initialize()
        rows = db.search(text_query=search_query, filters=filters, limit=10)

    if not rows:
        console.print(f"[dim]No results for \"{search_query}\".[/dim]")
        return

    from rich.table import Table
    table = Table(show_lines=False, title=f"Search: {search_query}")
    table.add_column("ID", style="dim", width=10)
    table.add_column("Space", width=10)
    table.add_column("Title", min_width=30)
    table.add_column("Summary", width=40, style="dim")

    for r in rows:
        table.add_row(r["id"], r["space"], r["title"], r["summary"][:40])

    console.print(table)


def _show_spaces(config: NotelyConfig) -> None:
    """Show spaces overview."""
    with Database(config.db_path) as db:
        db.initialize()
        stats = db.get_space_stats()

    for name, space_cfg in config.spaces.items():
        count = stats.get(name, {}).get("count", 0)
        console.print(f"  [bold]{space_cfg.display_name}[/bold] ({name}) — {count} notes")
        console.print(f"    [dim]{space_cfg.description}[/dim]")


def _timer_dispatch(config: NotelyConfig, arg: str) -> None:
    """Route /timer subcommands."""
    from ...timer import (
        add_timer_entry,
        elapsed_since,
        format_duration,
        get_running_timers,
        get_timer_log,
        parse_duration,
        start_timer,
        stop_timer,
    )

    parts = arg.strip().split() if arg.strip() else []
    sub = parts[0].lower() if parts else ""

    # /timer — show running timers
    if not parts:
        running = get_running_timers(config)
        if not running:
            console.print("[dim]No running timers.[/dim]")
            console.print("[dim]Start one: /timer start FOLDER description[/dim]")
            return
        for t in running:
            elapsed = elapsed_since(t["start"])
            console.print(
                f"  [bold]{t['description']}[/bold] ({t['folder']}) — "
                f"{elapsed} [dim](id: {t['id']})[/dim]"
            )
        return

    # /timer stop [hint]
    if sub == "stop":
        running = get_running_timers(config)
        if not running:
            console.print("[dim]No running timers to stop.[/dim]")
            return

        hint = " ".join(parts[1:]).lower() if len(parts) > 1 else ""

        # Pick which timer to stop
        if len(running) == 1:
            target = running[0]
        elif hint:
            # Try matching hint against description or folder
            matches = [
                t for t in running
                if hint in t["description"].lower() or hint in t["folder"].lower()
            ]
            if len(matches) == 1:
                target = matches[0]
            elif not matches:
                console.print(f"[yellow]No running timer matches '{hint}'.[/yellow]")
                return
            else:
                # Multiple matches — show numbered list
                from ...prompts import pick_from_list
                timer_items = [
                    (t["id"], f"{t['description']} ({t['folder']}) — {elapsed_since(t['start'])}")
                    for t in matches
                ]
                choice = pick_from_list(timer_items, prompt_text="Which timer to stop?")
                if choice is None:
                    console.print("[yellow]Cancelled.[/yellow]")
                    return
                try:
                    idx = int(choice) - 1
                    target = matches[idx]
                except (ValueError, IndexError):
                    console.print("[yellow]Cancelled.[/yellow]")
                    return
        else:
            # Multiple running, no hint — show numbered list
            from ...prompts import pick_from_list
            timer_items = [
                (t["id"], f"{t['description']} ({t['folder']}) — {elapsed_since(t['start'])}")
                for t in running
            ]
            choice = pick_from_list(timer_items, prompt_text="Which timer to stop?")
            if choice is None:
                console.print("[yellow]Cancelled.[/yellow]")
                return
            try:
                idx = int(choice) - 1
                target = running[idx]
            except (ValueError, IndexError):
                console.print("[yellow]Cancelled.[/yellow]")
                return

        elapsed = elapsed_since(target["start"])
        adjust = Prompt.ask(
            f"[dim]Logged {elapsed}. Adjust? (e.g. 45m, 1h, or Enter to keep)[/dim]",
            default="",
        )
        override = parse_duration(adjust) if adjust.strip() else None
        result = stop_timer(config, target["id"], override_minutes=override)
        if result:
            dur = format_duration(int(result["duration_minutes"]))
            console.print(
                f"[green]Stopped:[/green] {result['description']} ({result['folder']}) — {dur}"
            )
        return

    # /timer add FOLDER DESC DURATION
    if sub == "add":
        if len(parts) < 4:
            console.print("[yellow]Usage: /timer add FOLDER description DURATION[/yellow]")
            console.print("[dim]  e.g. /timer add sanity emergency call 30m[/dim]")
            return
        folder = parts[1]
        # Last token is the duration, everything in between is description
        duration_text = parts[-1]
        duration = parse_duration(duration_text)
        if duration is None:
            console.print(f"[yellow]Can't parse duration: '{duration_text}'[/yellow]")
            console.print("[dim]  Try: 30m, 1h, 1h15m, 90[/dim]")
            return
        desc = " ".join(parts[2:-1])
        if not desc:
            console.print("[yellow]Need a description before the duration.[/yellow]")
            return
        entry = add_timer_entry(config, folder, desc, duration)
        console.print(
            f"[green]Logged:[/green] {entry['description']} ({entry['folder']}) — "
            f"{format_duration(duration)}"
        )
        return

    # /timer log [FOLDER] [Nd]
    if sub == "log":
        folder = None
        days = 7
        for p in parts[1:]:
            # Check if it's a day count like "30d" or "7d"
            import re
            day_match = re.match(r"^(\d+)d$", p.lower())
            if day_match:
                days = int(day_match.group(1))
            else:
                folder = p
        entries = get_timer_log(config, folder=folder, days=days)
        if not entries:
            label = f" for '{folder}'" if folder else ""
            console.print(f"[dim]No time entries{label} in the last {days} days.[/dim]")
            return
        total_minutes = 0
        for e in entries:
            dur_min = int(e.get("duration_minutes", 0))
            total_minutes += dur_min
            dur = format_duration(dur_min)
            # Display start in local time
            from datetime import datetime as dt
            try:
                start_utc = dt.fromisoformat(e["start"])
                start_local = start_utc.astimezone()
                date_str = start_local.strftime("%b %d %H:%M")
            except (ValueError, KeyError):
                date_str = "?"
            console.print(
                f"  {date_str}  [bold]{dur:>6}[/bold]  {e['description']}  "
                f"[dim]({e['folder']})[/dim]"
            )
        console.print(f"\n  [bold]Total: {format_duration(total_minutes)}[/bold]", end="")
        if folder:
            console.print(f" [dim]({folder}, last {days}d)[/dim]")
        else:
            console.print(f" [dim](last {days}d)[/dim]")
        return

    # /timer start FOLDER [desc]
    if sub == "start":
        if len(parts) < 2:
            console.print("[yellow]Usage: /timer start FOLDER [description][/yellow]")
            return
        folder = parts[1]
        desc = " ".join(parts[2:]) if len(parts) > 2 else ""
        if not desc:
            desc = Prompt.ask("[dim]Description[/dim]").strip()
            if not desc:
                console.print("[yellow]Cancelled.[/yellow]")
                return
        entry = start_timer(config, folder, desc)
        console.print(
            f"[green]Timer started:[/green] {entry['description']} ({entry['folder']})"
        )
        return

    console.print(f"[yellow]Unknown timer command: {sub}[/yellow]")
    console.print("[dim]Try: /timer start, /timer stop, /timer add, /timer log[/dim]")


def _show_references(config: NotelyConfig, arg: str) -> None:
    """View, search, add, or delete reference data (account numbers, NPIs, phones, etc.)."""
    from ...storage import sync_references_index

    parts = arg.strip().split(None, 2) if arg.strip() else []

    with Database(config.db_path) as db:
        db.initialize()

        # /ref delete ID — delete a reference
        if len(parts) >= 2 and parts[0].lower() == "delete":
            try:
                ref_id = int(parts[1])
            except ValueError:
                console.print("[yellow]Usage: /ref delete ID[/yellow]")
                return
            if db.delete_reference(ref_id):
                console.print(f"[green]Deleted reference #{ref_id}.[/green]")
                sync_references_index(config, db)
            else:
                console.print(f"[yellow]No reference with ID {ref_id}.[/yellow]")
            return

        # /ref add entity key value — manual add (to working folder if set)
        if len(parts) >= 3 and parts[0].lower() == "add":
            # Re-split after "add"
            add_parts = arg.strip().split(None, 3)
            if len(add_parts) < 4:
                console.print("[yellow]Usage: /ref add entity key value[/yellow]")
                return
            entity, key, value = add_parts[1], add_parts[2], add_parts[3]
            ref_id = db.add_reference(entity=entity, key=key, value=value)
            console.print(f"[green]Saved:[/green] {entity}.{key} = {value} (#{ref_id})")
            sync_references_index(config, db)
            return

        # /ref entity key value — inline add (backwards compat)
        if len(parts) >= 3:
            entity, key, value = parts[0], parts[1], parts[2]
            ref_id = db.add_reference(entity=entity, key=key, value=value)
            console.print(f"[green]Saved:[/green] {entity}.{key} = {value} (#{ref_id})")
            sync_references_index(config, db)
            return

        # /ref entity key — missing value, prompt for it
        if len(parts) == 2:
            entity, key = parts[0], parts[1]
            value = Prompt.ask("[dim]Value[/dim]").strip()
            if not value:
                console.print("[yellow]Cancelled.[/yellow]")
                return
            ref_id = db.add_reference(entity=entity, key=key, value=value)
            console.print(f"[green]Saved:[/green] {entity}.{key} = {value} (#{ref_id})")
            sync_references_index(config, db)
            return

        # /ref QUERY — FTS search or entity filter
        if len(parts) == 1:
            query = parts[0]
            # Try FTS search first
            results = db.search_references(query)
            if not results:
                # Try entity filter
                results = db.get_references(entity=query)
            if not results:
                console.print(f"[yellow]No references matching '{query}'.[/yellow]")
                console.print("[dim]Use /ref entity key value to add one.[/dim]")
                return
            # Group by entity for display
            by_entity: dict[str, list] = {}
            for r in results:
                by_entity.setdefault(r["entity"], []).append(r)
            for entity, refs in sorted(by_entity.items()):
                console.print(f"  [bold]{entity}[/bold]")
                for r in refs:
                    stype = f" [{r['snippet_type']}]" if r["snippet_type"] != "fact" else ""
                    console.print(f"    {r['key']}: [cyan]{r['value']}[/cyan]{stype}  [dim]#{r['id']}[/dim]")
                    if r.get("description"):
                        console.print(f"      [dim]{r['description']}[/dim]")
            return

        # /ref — show all references
        all_refs = db.get_references()
        if not all_refs:
            console.print("[dim]No references stored yet.[/dim]")
            console.print("[dim]Use: /ref entity key value[/dim]")
            console.print("[dim]  e.g. /ref labcorp account_number 12345678[/dim]")
            return

        by_entity: dict[str, list] = {}
        for r in all_refs:
            by_entity.setdefault(r["entity"], []).append(r)
        for entity, refs in sorted(by_entity.items()):
            console.print(f"  [bold]{entity}[/bold]")
            for r in refs:
                stype = f" [{r['snippet_type']}]" if r["snippet_type"] != "fact" else ""
                console.print(f"    {r['key']}: [cyan]{r['value']}[/cyan]{stype}  [dim]#{r['id']}[/dim]")
                if r.get("description"):
                    console.print(f"      [dim]{r['description']}[/dim]")


_AGENT_YAML_TEMPLATE = """\
name: {name}
description: {description}

source:
  service: {service}

trigger:
  on_startup: {on_startup}
  on_demand: true

fetch:
  tool: {fetch_tool}
  params:
{fetch_params}
{expand_section}
{processing_section}
output:
  dedup: "{dedup}"
  suggest_routing: true
"""


def _show_secrets(config: NotelyConfig, arg: str) -> None:
    """View secrets stored in .secrets.toml. Values shown only with service + key."""
    from ...secrets import SecretsStore

    store = SecretsStore(config.secrets_path)
    secrets = store.get_all()

    if not secrets:
        console.print("[dim]No secrets stored. Use |||value||| markers when pasting to capture secrets.[/dim]")
        return

    parts = arg.strip().split(None, 1) if arg.strip() else []

    if len(parts) == 0:
        # /secret — list all services and keys (no values)
        for service in sorted(secrets):
            keys = sorted(secrets[service])
            key_list = ", ".join(keys)
            console.print(f"  [cyan]{service}[/cyan]: {key_list}")
        return

    service = parts[0]
    if service not in secrets:
        console.print(f"[yellow]No secrets for '{service}'. Available: {', '.join(sorted(secrets))}[/yellow]")
        return

    if len(parts) == 1:
        # /secret pypi — show keys for that service (no values)
        for key in sorted(secrets[service]):
            console.print(f"  [cyan]{service}[/cyan].{key} = ********")
        return

    # /secret pypi api_token — show the value
    key = parts[1]
    value = secrets[service].get(key)
    if value is None:
        console.print(f"[yellow]No key '{key}' in {service}. Available: {', '.join(sorted(secrets[service]))}[/yellow]")
        return

    console.print(f"  [cyan]{service}[/cyan].{key} = {value}")


def _handle_workflow(config: NotelyConfig, arg: str) -> None:
    """Handle /workflow commands — delegates to notely-agent."""
    parts = arg.strip().split(None, 1) if arg.strip() else []
    subcmd = parts[0].lower() if parts else ""

    if not subcmd:
        console.print(
            "[dim]Usage: /workflow create | /workflow pull [dim]\\[NAME][/dim] | /workflow list[/dim]"
        )
        return

    try:
        if subcmd == "create":
            from notely_agent.api import workflow_create
            workflow_create(config.base_dir)

        elif subcmd == "list":
            from notely_agent.api import workflow_list
            workflows = workflow_list()
            if not workflows:
                console.print("[dim]No workflows found. Create one with /workflow create[/dim]")
                return
            for wf in workflows:
                triggers = []
                t = wf.get("trigger", {})
                if t.get("on_startup"):
                    triggers.append("startup")
                if t.get("on_demand"):
                    triggers.append("on-demand")
                trigger_str = f" [dim]({', '.join(triggers)})[/dim]" if triggers else ""
                console.print(f"  [cyan]{wf['name']}[/cyan] — {wf.get('description', '')}{trigger_str}")

        elif subcmd == "pull":
            import asyncio
            from notely_agent.api import workflow_pull
            wf_name = parts[1].strip() if len(parts) > 1 else None
            if wf_name:
                console.print(f"[dim]Pulling from {wf_name}...[/dim]")
            else:
                console.print("[dim]Pulling from all workflows...[/dim]")
            new_items = asyncio.run(workflow_pull(wf_name))
            if new_items:
                console.print(f"[cyan]{len(new_items)} new item(s) added to inbox.[/cyan]")
            else:
                console.print("[dim]No new items.[/dim]")

        else:
            console.print(f"[yellow]Unknown subcommand: {subcmd}[/yellow]")
            console.print("[dim]Usage: /workflow create | pull | list[/dim]")

    except ImportError:
        console.print(
            "[yellow]Install notely-agent for workflow support:[/yellow] "
            "pip install -e ../notely-agent"
        )
    except Exception as e:
        console.print(f"[red]Workflow error: {e}[/red]")


def _mark_done(config: NotelyConfig, arg: str) -> None:
    """Mark a todo as done."""
    from ...storage import sync_todo_index

    try:
        item_id = int(arg.strip())
    except ValueError:
        console.print("[yellow]Usage: /todo done ID[/yellow]")
        return

    with Database(config.db_path) as db:
        db.initialize()

        row = db.get_action_item(item_id)

        if not row:
            console.print(f"[red]No todo with ID {item_id}.[/red]")
            return

        if row["status"] == "done":
            console.print(f"[yellow]Already done:[/yellow] {row['task']}")
            return

        from ...storage import update_action_status
        update_action_status(config, db, item_id, "done")
        sync_todo_index(config, db)
        console.print(f"[green]Done:[/green] {row['task']}")


def _reopen_todo(config: NotelyConfig, arg: str) -> None:
    """Reopen a completed todo."""
    from ...storage import sync_todo_index

    try:
        item_id = int(arg.strip())
    except ValueError:
        console.print("[yellow]Usage: /todo reopen ID[/yellow]")
        return

    with Database(config.db_path) as db:
        db.initialize()

        row = db.get_action_item(item_id)

        if not row:
            console.print(f"[red]No todo with ID {item_id}.[/red]")
            return

        if row["status"] == "open":
            console.print(f"[yellow]Already open:[/yellow] {row['task']}")
            return

        from ...storage import update_action_status
        update_action_status(config, db, item_id, "open")
        sync_todo_index(config, db)
        console.print(f"[green]Reopened:[/green] {row['task']}")


def _mkdir(config: NotelyConfig, path: str) -> None:
    """Create a folder under notes/."""
    from ...vectors import try_vector_sync_directory

    path = path.strip().strip("/")
    parts = path.split("/")

    if len(parts) < 2 or len(parts) > 3:
        console.print("[yellow]Usage: /mkdir space/group or /mkdir space/group/subgroup[/yellow]")
        return

    space = parts[0]
    if space not in config.space_names():
        console.print(f"[red]Unknown space: {space}[/red]")
        console.print(f"[dim]Available: {', '.join(config.space_names())}[/dim]")
        return

    target = config.notes_dir / path
    if target.exists():
        console.print(f"[yellow]Already exists:[/yellow] {path}")
        return

    target.mkdir(parents=True, exist_ok=True)

    # Ask for description and store in DB + vectors
    description = Prompt.ask("[dim]Description (optional)[/dim]", default="")
    group_slug = parts[1]
    subgroup_slug = parts[2] if len(parts) == 3 else None
    display_name = (subgroup_slug or group_slug).replace("-", " ").title()
    dir_id = "/".join(parts)

    with Database(config.db_path) as db:
        db.initialize()
        db.upsert_directory(
            dir_id=dir_id,
            space=space,
            group_slug=group_slug,
            display_name=display_name,
            description=description,
            subgroup_slug=subgroup_slug,
        )

    try_vector_sync_directory(
        config,
        dir_id=dir_id,
        space=space,
        group_slug=group_slug,
        subgroup_slug=subgroup_slug,
        display_name=display_name,
        description=description,
    )

    console.print(f"[green]Created:[/green] {path}")


def _rmdir(config: NotelyConfig, path: str) -> None:
    """Remove an empty folder under notes/."""
    from ...vectors import try_vector_delete_directory

    path = path.strip().strip("/")
    parts = path.split("/")

    if len(parts) < 2 or len(parts) > 3:
        console.print("[yellow]Usage: /rmdir space/group or /rmdir space/group/subgroup[/yellow]")
        return

    # Don't allow removing a whole space
    if len(parts) == 1:
        console.print("[red]Can't remove an entire space.[/red]")
        return

    target = config.notes_dir / path
    if not target.exists():
        console.print(f"[red]Not found:[/red] {path}")
        return

    # Check if empty (no files, only allow empty subdirs)
    contents = list(target.rglob("*"))
    files = [c for c in contents if c.is_file()]
    if files:
        console.print(f"[red]Folder not empty:[/red] {path} ({len(files)} file(s))")
        console.print("[dim]Move or delete the notes first.[/dim]")
        return

    # Remove empty directory tree
    import shutil
    shutil.rmtree(target)

    # Clean up DB + vectors
    dir_id = "/".join(parts)
    with Database(config.db_path) as db:
        db.initialize()
        db.delete_directory(dir_id)
    try_vector_delete_directory(config, dir_id)

    console.print(f"[green]Removed:[/green] {path}")


def _delete_note(config: NotelyConfig, note_id: str) -> None:
    """Delete a note — removes file, raw file, DB entry, and vector."""
    from ...storage import delete_note_files, sync_todo_index, sync_ideas_index
    from ...vectors import try_vector_delete_note

    note_id = note_id.strip()
    with Database(config.db_path) as db:
        db.initialize()

        row = db.get_note(note_id)
        if not row:
            console.print(f"[red]No note with ID {note_id}.[/red]")
            return

        console.print(f"  [bold]{row['title']}[/bold]")
        console.print(f"  [dim]{row['space']} / {row['date']} / {row['file_path']}[/dim]")
        from ...prompts import confirm_destructive
        if not confirm_destructive("Delete this note?"):
            console.print("[dim]Cancelled.[/dim]")
            return

        delete_note_files(config, row["file_path"])
        db.delete_note(note_id)
        try_vector_delete_note(config, note_id)
        sync_todo_index(config, db)
        sync_ideas_index(config, db)
        console.print(f"[green]Deleted:[/green] {row['title']}")


def _edit_note_inline(config: NotelyConfig, note_id: str) -> None:
    """Open a note in $EDITOR and re-index on save."""
    import os
    import subprocess
    from ...storage import absolute_path, read_note

    note_id = note_id.strip()
    with Database(config.db_path) as db:
        db.initialize()

        row = db.get_note(note_id)
        if not row:
            console.print(f"[red]No note with ID {note_id}.[/red]")
            return

        file_path = row["file_path"]
        abs_path = absolute_path(config, file_path)

        if not abs_path.exists():
            console.print(f"[red]File not found: {abs_path}[/red]")
            return

        editor = os.environ.get("EDITOR", "vi")
        mtime_before = abs_path.stat().st_mtime

        subprocess.run([editor, str(abs_path)])

        mtime_after = abs_path.stat().st_mtime

        if mtime_after != mtime_before:
            note = read_note(config, file_path)
            if note:
                from ...models import Refinement
                note.refinement = Refinement.HUMAN_REVIEWED
                db.upsert_note(note)
                try:
                    from ...vectors import try_vector_sync_note
                    try_vector_sync_note(config, note)
                except Exception:
                    pass
                console.print(f"[green]Re-indexed: {note.title}[/green]")
            else:
                console.print("[yellow]Could not re-read note after edit.[/yellow]")
        else:
            console.print("[dim]No changes detected.[/dim]")
