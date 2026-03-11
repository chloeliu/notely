"""Interactive database mode — /<db_name> enters a sub-mode for managing records."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from prompt_toolkit import PromptSession
from prompt_toolkit.completion import Completer, Completion
from rich.prompt import Prompt

from ...config import NotelyConfig
from ...db import Database

from ._shared import console

if TYPE_CHECKING:
    from prompt_toolkit.document import Document
    from prompt_toolkit.complete_event import CompleteEvent


class _DatabaseCommandCompleter(Completer):
    """Tab completion for database mode commands + entity names."""

    def __init__(self, db_name: str, config: NotelyConfig) -> None:
        self._db_name = db_name
        self._config = config
        self._entities: list[str] | None = None
        self._keys: list[str] | None = None

    def _get_entities(self) -> list[str]:
        if self._entities is None:
            try:
                with Database(self._config.db_path) as db:
                    db.initialize()
                    records = db.get_database_records(self._db_name)
                seen: set[str] = set()
                entities: list[str] = []
                for r in records:
                    name = r["entity"]
                    if name not in seen:
                        seen.add(name)
                        entities.append(name)
                self._entities = entities
            except Exception:
                self._entities = []
        return self._entities

    def _get_keys(self) -> list[str]:
        if self._keys is None:
            try:
                with Database(self._config.db_path) as db:
                    db.initialize()
                    self._keys = db.get_database_keys(self._db_name)
            except Exception:
                self._keys = []
        return self._keys

    def invalidate(self) -> None:
        self._entities = None
        self._keys = None

    def get_completions(
        self, document: "Document", complete_event: "CompleteEvent"
    ):
        text = document.text_before_cursor.strip()
        _SUBCMDS = [
            ("add", "add a record"),
            ("delete", "delete a record"),
            ("edit", "edit a record value or entity name"),
            ("show", "show entity details"),
            ("search", "search records"),
            ("info", "show database settings"),
            ("describe", "set database description"),
            ("fields", "set expected fields"),
            ("all", "show all records"),
            ("open", "open CSV in default app"),
            ("drop", "delete entire database"),
            ("refresh", "reload records"),
        ]

        if " " not in text:
            partial = text.lower()
            for sub, hint in _SUBCMDS:
                if sub.startswith(partial):
                    yield Completion(sub, start_position=-len(text), display_meta=hint)
            # Entity names
            for name in self._get_entities():
                if not partial or partial in name.lower():
                    yield Completion(name, start_position=-len(text))
        else:
            words = text.split()
            first = words[0].lower()
            if first in ("show", "delete", "edit"):
                rest = text.split(None, 1)[1] if len(words) > 1 else ""
                partial = rest.strip().lower()
                for name in self._get_entities():
                    if not partial or partial in name.lower():
                        yield Completion(name, start_position=-len(rest))
            elif first == "add":
                if len(words) == 1:
                    # After "add " — suggest entity names
                    for name in self._get_entities():
                        yield Completion(name, start_position=0)
                elif len(words) == 2:
                    # After "add ENTITY " — suggest known keys
                    partial = words[1].lower() if len(words) > 1 else ""
                    # Check if we're still typing the entity or moved to key
                    after_add = text.split(None, 1)[1] if len(text.split(None, 1)) > 1 else ""
                    if " " in after_add:
                        # We have entity + partial key
                        key_part = after_add.split(None, 1)
                        key_partial = key_part[1].lower() if len(key_part) > 1 else ""
                        for k in self._get_keys():
                            if not key_partial or k.lower().startswith(key_partial):
                                yield Completion(k, start_position=-len(key_partial))
                    else:
                        # Still typing entity name
                        for name in self._get_entities():
                            if not partial or partial in name.lower():
                                yield Completion(name, start_position=-len(partial))
                elif len(words) == 3:
                    # After "add ENTITY key_partial" — suggest known keys
                    key_partial = words[2].lower()
                    for k in self._get_keys():
                        if k.lower().startswith(key_partial):
                            yield Completion(k, start_position=-len(words[2]))


def _database_mode(
    config: NotelyConfig,
    db_name: str,
    working_folder: dict | None = None,
) -> None:
    """Interactive database management mode."""
    from ...storage import sync_database_indexes

    # Session-local number→id mapping
    num_to_id: dict[int, int] = {}
    records: list[dict] = []

    wf_space = (working_folder or {}).get("space", "")
    wf_group = (working_folder or {}).get("group_slug", "")

    def _load(entity_filter: str | None = None, show_all: bool = False) -> list[dict]:
        nonlocal records, num_to_id
        with Database(config.db_path) as db:
            db.initialize()
            if entity_filter:
                raw = db.get_database_records(db_name, entity=entity_filter)
            elif show_all:
                raw = db.get_database_records(db_name)
            else:
                # Folder-scoped + global
                scoped = db.get_database_records(db_name, space=wf_space, group_slug=wf_group)
                if wf_space:
                    glob = db.get_database_records(db_name, space="", group_slug="")
                    seen = {r["id"] for r in scoped}
                    scoped.extend(r for r in glob if r["id"] not in seen)
                raw = scoped
        records = raw
        num_to_id.clear()
        return raw

    def _display(recs: list[dict] | None = None, limit_entities: int = 10) -> None:
        show = recs if recs is not None else records
        if not show:
            console.print(f"[dim]No records in {db_name}.[/dim]")
            # Show known fields if any exist globally
            with Database(config.db_path) as db:
                db.initialize()
                keys = db.get_database_keys(db_name)
            if keys:
                console.print(f"[dim]Known fields: {', '.join(keys)}[/dim]")
            console.print(f"[dim]Use: add ENTITY key value[/dim]")
            return
        # Count unique entities to check if we need to truncate
        all_entities = list(dict.fromkeys(r["entity"] for r in show))
        if limit_entities and len(all_entities) > limit_entities:
            visible = set(all_entities[:limit_entities])
            truncated = [r for r in show if r["entity"] in visible]
            _render_records(truncated)
            hidden = len(all_entities) - limit_entities
            console.print(f"\n[dim]Showing {limit_entities} of {len(all_entities)} entities. "
                          f"Type 'all' to see all, or 'open' for CSV.[/dim]")
        else:
            _render_records(show)

    def _render_records(recs: list[dict]) -> None:
        # Group by entity
        by_entity: dict[str, list[dict]] = {}
        for r in recs:
            by_entity.setdefault(r["entity"], []).append(r)

        counter = 1
        for entity, fields in sorted(by_entity.items()):
            # Determine folder scope display
            scope_parts: set[str] = set()
            for f in fields:
                s, g = f.get("space", ""), f.get("group_slug", "")
                if s and g:
                    scope_parts.add(f"{s}/{g}")
                elif s:
                    scope_parts.add(s)
            scope = f" ({', '.join(sorted(scope_parts))})" if scope_parts else " (global)"

            console.print(f"\n  [bold]{entity}[/bold][dim]{scope}[/dim]")

            for f in fields:
                num_to_id[counter] = f["id"]
                desc = f"  [dim]{f['description']}[/dim]" if f.get("description") else ""
                console.print(
                    f"    [dim]{counter}[/dim]  {f['key']}: [cyan]{f['value']}[/cyan]{desc}"
                )
                counter += 1

            # Contacts get interaction history
            if db_name == "contacts":
                with Database(config.db_path) as db:
                    db.initialize()
                    interactions = db.get_contact_interactions(entity, limit=3)
                if interactions:
                    recent_str = ", ".join(
                        f"'{n['title']}' ({n['date']})" for n in interactions
                    )
                    console.print(f"    [dim]Recent: {recent_str}[/dim]")

        console.print()

    def _show_entity(name: str) -> None:
        with Database(config.db_path) as db:
            db.initialize()
            recs = db.get_database_records(db_name, entity=name)
        if not recs:
            console.print(f"[yellow]No records for '{name}' in {db_name}.[/yellow]")
            return

        entity = recs[0]["entity"]
        scope_parts: set[str] = set()
        for f in recs:
            s, g = f.get("space", ""), f.get("group_slug", "")
            if s and g:
                scope_parts.add(f"{s}/{g}")
            elif s:
                scope_parts.add(s)
        scope = f" ({', '.join(sorted(scope_parts))})" if scope_parts else " (global)"

        console.print(f"\n  [bold]{entity}[/bold][dim]{scope}[/dim]")
        for f in recs:
            console.print(f"    {f['key']}: [cyan]{f['value']}[/cyan]")
            if f.get("description"):
                console.print(f"      [dim]{f['description']}[/dim]")

        # Contacts: interaction history
        if db_name == "contacts":
            with Database(config.db_path) as db:
                db.initialize()
                interactions = db.get_contact_interactions(entity, limit=10)
            if interactions:
                console.print(f"\n  [bold]Recent Interactions[/bold]")
                for n in interactions:
                    console.print(
                        f"    {n['date']}  [cyan]{n['title']}[/cyan]  [dim]#{n['id']}[/dim]"
                    )
            else:
                console.print(f"\n  [dim]No notes mentioning {entity}.[/dim]")
        console.print()

    # Initial load + display
    _load(show_all=not wf_space)
    _display()

    completer = _DatabaseCommandCompleter(db_name, config)
    session: PromptSession = PromptSession(completer=completer)
    prompt_label = f"notely-{db_name}> "

    last_ctrl_c = 0.0

    while True:
        try:
            text = session.prompt(prompt_label).strip()
        except KeyboardInterrupt:
            now = time.monotonic()
            if now - last_ctrl_c < 2.0:
                break
            last_ctrl_c = now
            console.print("[dim]Press Ctrl+C again to exit mode.[/dim]")
            continue
        except EOFError:
            break

        if not text:
            continue

        low = text.lower()

        if low in ("q", "/back", "exit", "quit"):
            break

        if low == "refresh":
            _load(show_all=not wf_space)
            _display()
            completer.invalidate()
            continue

        if low == "info":
            with Database(config.db_path) as db:
                db.initialize()
                desc = db.get_database_description(db_name) or "(none)"
                flds = db.get_database_fields(db_name)
                extract = db.get_database_meta(db_name, "extract_from_notes") == "true"
                total = len(db.get_database_records(db_name))
            console.print(f"\n  [bold]{db_name}[/bold]")
            console.print(f"    Description:  {desc}")
            console.print(f"    Fields:       {', '.join(flds) if flds else '(none)'}")
            console.print(f"    Auto-extract: {'yes' if extract else 'no'}")
            console.print(f"    Records:      {total}")
            console.print(f"\n[dim]Change with: describe, fields[/dim]")
            continue

        if low.startswith("describe"):
            desc_text = text[len("describe"):].strip()
            if not desc_text:
                # Show current description
                with Database(config.db_path) as db:
                    db.initialize()
                    current = db.get_database_description(db_name)
                if current:
                    console.print(f"[dim]Description: {current}[/dim]")
                else:
                    console.print(f"[dim]No description set.[/dim]")
                try:
                    desc_text = Prompt.ask("[dim]New description[/dim]", default="")
                except (KeyboardInterrupt, EOFError):
                    continue
            if desc_text.strip():
                with Database(config.db_path) as db:
                    db.initialize()
                    db.set_database_description(db_name, desc_text.strip())
                console.print(f"[green]Description updated.[/green]")
            continue

        if low.startswith("fields"):
            fields_text = text[len("fields"):].strip()
            with Database(config.db_path) as db:
                db.initialize()
                current = db.get_database_fields(db_name)
            if current:
                console.print(f"[dim]Fields: {', '.join(current)}[/dim]")
            if not fields_text:
                try:
                    fields_text = Prompt.ask(
                        "[dim]Expected fields (comma-separated)[/dim]", default=""
                    )
                except (KeyboardInterrupt, EOFError):
                    continue
            if fields_text.strip():
                fields = [f.strip().lower() for f in fields_text.split(",") if f.strip()]
                if fields:
                    with Database(config.db_path) as db:
                        db.initialize()
                        db.set_database_fields(db_name, fields)
                    console.print(f"[green]Fields updated: {', '.join(fields)}[/green]")
            continue

        if low == "drop":
            try:
                confirm = Prompt.ask(
                    f"[red]Delete ALL records in '{db_name}'? This cannot be undone.[/red]",
                    choices=["y", "n"], default="n",
                )
            except (KeyboardInterrupt, EOFError):
                continue
            if confirm != "y":
                console.print("[dim]Cancelled.[/dim]")
                continue
            with Database(config.db_path) as db:
                db.initialize()
                count = db.delete_database(db_name)
                sync_database_indexes(config, db)
            console.print(f"[green]Deleted {count} record(s). Database '{db_name}' removed.[/green]")
            break  # Exit mode since database is gone

        if low == "all":
            _load(show_all=True)
            _display(limit_entities=0)
            continue

        if low == "open":
            import subprocess
            csv_path = config.workspace_path / f"_{db_name}.csv"
            if csv_path.exists():
                subprocess.Popen(["open", str(csv_path)])
                console.print(f"[dim]Opening {csv_path.name}...[/dim]")
            else:
                console.print(f"[yellow]No CSV file found. Run 'refresh' first.[/yellow]")
            continue

        parts = text.split(None, 2)
        cmd = parts[0].lower()

        # add ENTITY key value
        if cmd == "add":
            add_parts = text.split(None, 3)
            if len(add_parts) < 4:
                console.print("[yellow]Usage: add ENTITY key value[/yellow]")
                continue
            entity, key, value = add_parts[1], add_parts[2], add_parts[3]
            with Database(config.db_path) as db:
                db.initialize()
                # Confirm new database creation on first record
                if not db.database_exists(db_name):
                    from ._shared import _confirm_new_database
                    if not _confirm_new_database(config, db_name):
                        console.print("[dim]Cancelled.[/dim]")
                        continue
                # Check for similar entity names
                similar = db.find_similar_entities(entity, db_name)
                if similar:
                    console.print(f"[yellow]Similar entities exist: {', '.join(similar[:3])}[/yellow]")
                    try:
                        choice = Prompt.ask(
                            f"[dim]Use existing name, or keep '{entity}'?[/dim]",
                            choices=[*[str(i+1) for i in range(min(3, len(similar)))], "k"],
                            default="k",
                        )
                        if choice != "k":
                            entity = similar[int(choice) - 1]
                    except (KeyboardInterrupt, EOFError, ValueError):
                        pass
                ref_id = db.add_reference(
                    entity=entity, key=key, value=value,
                    snippet_type=db_name,
                    space=wf_space, group_slug=wf_group,
                )
                sync_database_indexes(config, db)
            console.print(f"[green]Saved:[/green] {entity}.{key} = {value} (#{ref_id})")
            _load(show_all=not wf_space)
            completer.invalidate()
            continue

        # delete N [N2 N3 ...] or delete N-M
        if cmd == "delete":
            if len(parts) < 2:
                console.print("[yellow]Usage: delete N [N2 N3 ...] or delete N-M[/yellow]")
                continue
            # Parse numbers: support "1 3 5", "1-5", or mixed "1-3 5 7"
            nums: list[int] = []
            for tok in parts[1:]:
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
                # Try as entity name
                entity_name = " ".join(parts[1:])
                with Database(config.db_path) as db:
                    db.initialize()
                    entity_recs = db.get_database_records(db_name, entity=entity_name)
                if not entity_recs:
                    console.print(f"[yellow]No entity '{entity_name}' in {db_name}.[/yellow]")
                    continue
                count = len(entity_recs)
                confirm = Prompt.ask(
                    f"Delete all {count} record(s) for '{entity_name}'?",
                    choices=["y", "n"], default="n",
                )
                if confirm != "y":
                    console.print("[dim]Cancelled.[/dim]")
                    continue
                deleted = 0
                with Database(config.db_path) as db:
                    db.initialize()
                    for r in entity_recs:
                        if db.delete_reference(r["id"]):
                            deleted += 1
                    if deleted:
                        sync_database_indexes(config, db)
                console.print(f"[green]Deleted {deleted} record(s) for '{entity_name}'.[/green]")
                _load(show_all=not wf_space)
                completer.invalidate()
                continue
            # Validate all numbers
            valid = [(n, num_to_id.get(n)) for n in nums if num_to_id.get(n) is not None]
            invalid = [n for n in nums if num_to_id.get(n) is None]
            if invalid:
                console.print(f"[yellow]No records for: {', '.join(str(n) for n in invalid)}[/yellow]")
            if not valid:
                continue
            label = ", ".join(f"#{n}" for n, _ in valid)
            confirm = Prompt.ask(
                f"Delete {len(valid)} record(s) ({label})?",
                choices=["y", "n"], default="n",
            )
            if confirm != "y":
                console.print("[dim]Cancelled.[/dim]")
                continue
            deleted = 0
            with Database(config.db_path) as db:
                db.initialize()
                for num, row_id in valid:
                    if db.delete_reference(row_id):
                        deleted += 1
                if deleted:
                    sync_database_indexes(config, db)
            console.print(f"[green]Deleted {deleted} record(s).[/green]")
            _load(show_all=not wf_space)
            completer.invalidate()
            continue

        # edit N — pick fields to edit from a numbered list
        if cmd == "edit":
            if len(parts) < 2:
                console.print("[yellow]Usage: edit N[/yellow]")
                continue
            try:
                num = int(parts[1])
            except ValueError:
                console.print("[yellow]Usage: edit N (session number)[/yellow]")
                continue
            row_id = num_to_id.get(num)
            if row_id is None:
                console.print(f"[yellow]No record #{num} in current view.[/yellow]")
                continue
            # Load the entity and all its fields
            with Database(config.db_path) as db:
                db.initialize()
                row = db.conn.execute(
                    "SELECT entity FROM snippets WHERE id = ?", (row_id,)
                ).fetchone()
                if not row:
                    console.print(f"[yellow]Record not found.[/yellow]")
                    continue
                cur_entity = row["entity"]
                all_fields = db.get_database_records(db_name, entity=cur_entity)
            # Build editable items: entity name + each key=value
            editable: list[tuple[str, str, int | None]] = []  # (label, current_value, row_id|None)
            editable.append(("entity", cur_entity, None))
            for f in all_fields:
                editable.append((f["key"], f["value"], f["id"]))
            changed = False
            while True:
                console.print(f"\n  [bold]{cur_entity}[/bold]")
                for i, (label, val, _) in enumerate(editable, 1):
                    if label == "entity":
                        console.print(f"    [{i}] entity: [cyan]{val}[/cyan]")
                    else:
                        console.print(f"    [{i}] {label}: [cyan]{val}[/cyan]")
                try:
                    pick = Prompt.ask(
                        r"[dim]Pick a number to edit, or \[q] to finish[/dim]",
                        default="q",
                    )
                except (KeyboardInterrupt, EOFError):
                    break
                if pick.strip().lower() in ("q", ""):
                    break
                try:
                    idx = int(pick) - 1
                except ValueError:
                    continue
                if idx < 0 or idx >= len(editable):
                    continue
                label, cur_val, field_row_id = editable[idx]
                try:
                    new_val = Prompt.ask(f"  {label}", default=cur_val)
                except (KeyboardInterrupt, EOFError):
                    continue
                if new_val == cur_val:
                    continue
                with Database(config.db_path) as db:
                    db.initialize()
                    if label == "entity":
                        # Rename all rows for this entity
                        db.conn.execute(
                            "UPDATE snippets SET entity = ? WHERE entity = ? AND snippet_type = ?",
                            (new_val, cur_entity, db_name),
                        )
                        db.conn.commit()
                        cur_entity = new_val
                        editable[0] = ("entity", new_val, None)
                    else:
                        db.update_snippet(field_row_id, new_val)
                        editable[idx] = (label, new_val, field_row_id)
                    sync_database_indexes(config, db)
                console.print(f"[green]Updated {label} → {new_val}[/green]")
                changed = True
            if changed:
                _load(show_all=not wf_space)
                completer.invalidate()
            continue

        # show ENTITY
        if cmd == "show":
            if len(parts) < 2:
                console.print("[yellow]Usage: show ENTITY[/yellow]")
                continue
            _show_entity(" ".join(parts[1:]))
            continue

        # search TEXT
        if cmd == "search":
            if len(parts) < 2:
                console.print("[yellow]Usage: search TEXT[/yellow]")
                continue
            query = " ".join(parts[1:])
            with Database(config.db_path) as db:
                db.initialize()
                results = db.search_references(query)
                results = [r for r in results if r.get("snippet_type") == db_name]
            if not results:
                console.print(f"[yellow]No results for '{query}' in {db_name}.[/yellow]")
                continue
            _render_records(results)
            continue

        # Bare text → try entity filter
        entity_recs = []
        with Database(config.db_path) as db:
            db.initialize()
            entity_recs = db.get_database_records(db_name, entity=text)
        if entity_recs:
            _render_records(entity_recs)
        else:
            # Try FTS search
            with Database(config.db_path) as db:
                db.initialize()
                results = db.search_references(text)
                results = [r for r in results if r.get("snippet_type") == db_name]
            if results:
                _render_records(results)
            else:
                console.print(f"[yellow]Unknown command or no results: {text}[/yellow]")
                console.print("[dim]Commands: add, edit, delete, show, search, all, open, info, refresh, q[/dim]")
