"""Tab completion classes for notely open."""

from __future__ import annotations

from prompt_toolkit.completion import Completer, Completion

from ...config import NotelyConfig
from ...db import Database
from ._shared import _get_all_folders


class _SlashCompleter(Completer):
    """Tab-complete slash commands, /chat folder names, and /agent folder + @note."""

    COMMANDS = [
        "/agent", "/chat", "/clip", "/folder", "/inbox", "/timer", "/todo",
        "/ideas", "/list", "/search", "/spaces", "/mkdir", "/rmdir",
        "/delete", "/edit", "/ref", "/secret", "/sync", "/workflow", "/help", "/quit",
    ]

    def __init__(self, config: NotelyConfig) -> None:
        self._config = config
        self._folder_cache: list[tuple[str, str, str]] | None = None
        self._note_cache: dict[str, list[tuple[str, str]]] = {}
        self._todo_cache: list[dict] | None = None
        self._recent_notes_cache: list[dict] | None = None
        self._secret_cache: dict[str, dict[str, str]] | None = None

    def _get_folders(self) -> list[tuple[str, str, str]]:
        """Get folders, cached."""
        if self._folder_cache is None:
            self._folder_cache = _get_all_folders(self._config)
        return self._folder_cache

    def _get_secrets(self) -> dict[str, dict[str, str]]:
        """Get secrets structure {service: {key: value}}, cached."""
        if self._secret_cache is None:
            try:
                from ...secrets import SecretsStore
                self._secret_cache = SecretsStore(self._config.secrets_path).get_all()
            except Exception:
                self._secret_cache = {}
        return self._secret_cache

    def invalidate_secrets(self) -> None:
        """Clear secret cache after saving new secrets."""
        self._secret_cache = None

    def invalidate(self) -> None:
        """Clear cache after /mkdir or /rmdir."""
        self._folder_cache = None
        self._note_cache.clear()
        self._todo_cache = None
        self._recent_notes_cache = None

    def invalidate_notes(self) -> None:
        """Clear note cache after save/delete."""
        self._recent_notes_cache = None

    def invalidate_todos(self) -> None:
        """Clear todo cache after /done or new note with action items."""
        self._todo_cache = None
        self._recent_notes_cache = None

    def _get_open_todos(self) -> list[dict]:
        """Get YOUR open action items. Cached until invalidated."""
        if self._todo_cache is None:
            try:
                with Database(self._config.db_path) as db:
                    db.initialize()
                    self._todo_cache = db.get_action_items_filtered(
                        owner=self._config.user_name,
                    )
            except Exception:
                self._todo_cache = []
        return self._todo_cache

    def _get_done_todos(self) -> list[dict]:
        """Get YOUR recently completed action items for /todo reopen."""
        try:
            with Database(self._config.db_path) as db:
                db.initialize()
                return db.get_action_items_filtered(
                    owner=self._config.user_name,
                    status="done",
                )
        except Exception:
            return []

    def _get_recent_notes(self) -> list[dict]:
        """Get recent notes (id, title, date) for /delete and /edit autocomplete."""
        if self._recent_notes_cache is None:
            try:
                with Database(self._config.db_path) as db:
                    db.initialize()
                    rows = db.conn.execute(
                        "SELECT id, title, date FROM notes ORDER BY date DESC LIMIT 50",
                    ).fetchall()
                    self._recent_notes_cache = [dict(r) for r in rows]
            except Exception:
                self._recent_notes_cache = []
        return self._recent_notes_cache

    def _get_notes_in_folder(
        self, space: str, group_slug: str
    ) -> list[tuple[str, str]]:
        """Get (title, date) for notes in a folder. Cached by space/group_slug."""
        cache_key = f"{space}/{group_slug}"
        if cache_key not in self._note_cache:
            try:
                with Database(self._config.db_path) as db:
                    db.initialize()
                    ctx = db.get_folder_context(space, group_slug)
                self._note_cache[cache_key] = [
                    (n["title"], n["date"])
                    for n in ctx.get("notes", [])
                ]
            except Exception:
                self._note_cache[cache_key] = []
        return self._note_cache[cache_key]

    def _resolve_folder_from_word(
        self, word: str
    ) -> tuple[str, str] | None:
        """Match a typed word to a folder. Returns (space, folder_path) or None.

        Accepts both short slugs ("sanity") and full paths ("clients/sanity").
        folder_path is "" for space-level, group_slug for groups,
        "group/subgroup" for subgroups.
        """
        word_lower = word.lower()
        for slug, display, space in self._get_folders():
            full_path = space if slug == space else f"{space}/{slug}"
            folder_path = "" if slug == space else slug
            if (word_lower == full_path.lower()
                    or word_lower == slug.lower()
                    or word_lower == display.lower()):
                return (space, folder_path)
        return None

    def _get_services_for_folder(
        self, space: str, group_slug: str
    ) -> list[str]:
        """Get connected service names for a folder from notely-agent config."""
        try:
            from notely_agent.config import AgentConfig
            config = AgentConfig()
            folder_path = f"{space}/{group_slug}"
            return config.get_connected_services(space=space, folder=folder_path)
        except Exception:
            return []

    def _folder_completions(self, partial: str):
        """Yield folder completions with hierarchical drill-down.

        - Empty input → show only top-level spaces
        - Input with "/" → prefix match for drill-down (clients/ → clients/sanity)
        - Input without "/" → fuzzy match all levels (san → clients/sanity)
        """
        query = partial.strip().lower()
        for slug, display, space in self._get_folders():
            full_path = space if slug == space else f"{space}/{slug}"
            is_space = slug == space

            if not query:
                # Empty → show only top-level spaces
                if is_space:
                    yield Completion(
                        full_path,
                        start_position=-len(partial),
                        display_meta=display,
                    )
            elif "/" in query:
                # Has slash → prefix match for drill-down
                if full_path.lower().startswith(query) and full_path.lower() != query.rstrip("/"):
                    yield Completion(
                        full_path,
                        start_position=-len(partial),
                        display_meta=display,
                    )
            else:
                # No slash → fuzzy match all levels
                if query in slug.lower() or query in display.lower():
                    yield Completion(
                        full_path,
                        start_position=-len(partial),
                        display_meta=display,
                    )

    def get_completions(self, document, complete_event):
        text = document.text_before_cursor

        # Complete slash command names
        if text.startswith("/") and " " not in text:
            for cmd in self.COMMANDS:
                if cmd.startswith(text.lower()):
                    yield Completion(cmd, start_position=-len(text))
            return

        # Complete /chat folder names
        if text.lower().startswith("/chat "):
            yield from self._folder_completions(text[6:])
            return

        # Complete /folder folder names
        if text.lower().startswith("/folder "):
            yield from self._folder_completions(text[8:])
            return

        # Complete /list, /ideas, /ref — folder names
        for prefix in ("/list ", "/ideas ", "/ref "):
            if text.lower().startswith(prefix):
                after = text[len(prefix):]
                if " " not in after:
                    yield from self._folder_completions(after)
                return

        # Complete /search — folder name as first word, then query
        if text.lower().startswith("/search "):
            after = text[8:]
            if " " not in after:
                yield from self._folder_completions(after)
            return

        # Complete /clip — folder names after URL (second word)
        if text.lower().startswith("/clip "):
            after = text[6:]
            words = after.split()
            # After URL, offer folder completions
            if len(words) >= 1 and " " in after:
                folder_part = after[len(words[0]):].lstrip()
                yield from self._folder_completions(folder_part)
            return

        # Complete /todo — subcommands, then IDs for done/reopen
        if text.lower().startswith("/todo "):
            after = text[6:]
            if " " not in after:
                # First word: subcommands
                partial = after.lower()
                for sub, hint in (
                    ("done", "mark a todo as done"),
                    ("reopen", "reopen a completed todo"),
                    ("all", "show all open todos"),
                ):
                    if sub.startswith(partial):
                        yield Completion(sub, start_position=-len(after), display_meta=hint)
                return
            words = after.split(None, 1)
            first = words[0].lower()
            rest = words[1] if len(words) > 1 else ""
            if first == "done":
                partial = rest.strip().lower()
                for item in self._get_open_todos():
                    item_id = str(item["id"])
                    if not partial or item_id.startswith(partial):
                        task = item["task"]
                        owner = item.get("owner") or ""
                        meta = f"{task[:50]} ({owner})" if owner else task[:60]
                        yield Completion(
                            item_id,
                            start_position=-len(rest),
                            display_meta=meta,
                        )
                return
            if first == "reopen":
                partial = rest.strip().lower()
                for item in self._get_done_todos():
                    item_id = str(item["id"])
                    if not partial or item_id.startswith(partial):
                        task = item["task"]
                        owner = item.get("owner") or ""
                        meta = f"{task[:50]} ({owner})" if owner else task[:60]
                        yield Completion(
                            item_id,
                            start_position=-len(rest),
                            display_meta=meta,
                        )
                return
            return

        # Complete /timer — subcommands only first, then folder after start/add/log
        if text.lower().startswith("/timer "):
            after = text[7:]
            if " " not in after:
                # First word: subcommands only
                partial = after.lower()
                for sub, hint in (
                    ("start", "start a timer"),
                    ("stop", "stop a timer"),
                    ("add", "log retroactively"),
                    ("log", "view entries"),
                ):
                    if sub.startswith(partial):
                        yield Completion(sub, start_position=-len(after), display_meta=hint)
                return
            words = after.split(None, 1)
            first = words[0].lower()
            rest = words[1] if len(words) > 1 else ""
            if first == "stop":
                # Offer running timer descriptions
                from ...timer import get_running_timers
                running = get_running_timers(self._config)
                partial = rest.lower()
                for t in running:
                    desc = t.get("description", "")
                    if not partial or partial in desc.lower():
                        folder = t.get("folder", "")
                        yield Completion(
                            desc,
                            start_position=-len(rest),
                            display_meta=folder,
                        )
                return
            if first in ("start", "add", "log"):
                # Offer folder names as next word
                if " " not in rest:
                    yield from self._folder_completions(rest)
                return
            return

        # Complete /inbox — subcommands
        if text.lower().startswith("/inbox "):
            after = text[7:]
            if " " not in after:
                partial = after.lower()
                for sub, hint in (
                    ("count", "show inbox count"),
                    ("skip", "skip all pending"),
                    ("history", "recently filed items"),
                ):
                    if sub.startswith(partial):
                        yield Completion(sub, start_position=-len(after), display_meta=hint)
            return

        # Complete /secret — service names, then key names
        if text.lower().startswith("/secret "):
            after = text[8:]
            secrets = self._get_secrets()
            if " " not in after:
                # First word: service names
                partial = after.lower()
                for service in sorted(secrets):
                    if service.startswith(partial) or not partial:
                        key_count = len(secrets[service])
                        yield Completion(
                            service,
                            start_position=-len(after),
                            display_meta=f"{key_count} key(s)",
                        )
            else:
                # Second word: key names within the service
                words = after.split(None, 1)
                service = words[0]
                rest = words[1] if len(words) > 1 else ""
                partial = rest.strip().lower()
                if service in secrets:
                    for key in sorted(secrets[service]):
                        if key.startswith(partial) or not partial:
                            yield Completion(
                                key,
                                start_position=-len(rest),
                            )
            return

        # Complete /delete and /edit — recent note IDs with titles
        for prefix in ("/delete ", "/edit "):
            if text.lower().startswith(prefix):
                partial = text[len(prefix):].strip().lower()
                for note in self._get_recent_notes():
                    nid = note["id"]
                    if not partial or partial in nid.lower() or partial in note["title"].lower():
                        meta = f"{note['title'][:50]} ({note['date']})"
                        yield Completion(
                            nid,
                            start_position=-len(text[len(prefix):]),
                            display_meta=meta,
                        )
                return

        # Complete /workflow — subcommands + workflow names after pull
        if text.lower().startswith("/workflow "):
            after = text[10:]
            if " " not in after:
                partial = after.lower()
                for sub, hint in (
                    ("create", "create a new workflow with AI"),
                    ("pull", "pull from workflows"),
                    ("list", "show available workflows"),
                ):
                    if sub.startswith(partial):
                        yield Completion(sub, start_position=-len(after), display_meta=hint)
                return
            # /workflow pull NAME — offer workflow names
            words = after.split(None, 1)
            if words[0].lower() == "pull":
                rest = words[1] if len(words) > 1 else ""
                partial = rest.strip().lower()
                try:
                    from notely_agent.api import workflow_list
                    for wf_info in workflow_list():
                        name = wf_info["name"]
                        if not partial or partial in name:
                            yield Completion(
                                name,
                                start_position=-len(rest),
                                display_meta=wf_info.get("description", ""),
                            )
                except (ImportError, Exception):
                    pass
            return

        # Complete /agent — subcommands, then folder/service/@note per subcommand
        if text.lower().startswith("/agent "):
            after = text[7:]
            words = after.split()

            _AGENT_SUBCMDS = [
                ("chat", "conversational agent mode"),
                ("run", "one-shot agent action"),
                ("connect", "connect services"),
                ("disconnect", "remove a service"),
            ]

            # Phase 0: first word → subcommands (+ folder names for backwards compat)
            if " " not in after:
                partial = after.lower()
                for sub, hint in _AGENT_SUBCMDS:
                    if sub.startswith(partial):
                        yield Completion(sub, start_position=-len(after), display_meta=hint)
                # Also offer folder names for backwards compat (/agent sanity)
                yield from self._folder_completions(after)
                return

            subcmd = words[0].lower()
            rest_text = after[len(words[0]):].lstrip()

            # /agent chat — folder names, then @note titles
            if subcmd == "chat":
                rest_words = rest_text.split()
                # @note completion
                if rest_words and "@" in rest_text:
                    folder_word = rest_words[0]
                    resolved = self._resolve_folder_from_word(folder_word)
                    if resolved:
                        space, group_slug = resolved
                        at_pos = rest_text.rfind("@")
                        partial_title = rest_text[at_pos + 1:]
                        query = partial_title.lower()
                        for title, date in self._get_notes_in_folder(space, group_slug):
                            if not query or query in title.lower():
                                yield Completion(
                                    f"@{title}",
                                    start_position=-(len(partial_title) + 1),
                                    display_meta=date,
                                )
                    return
                # Folder completion
                yield from self._folder_completions(rest_text)
                return

            # /agent run — folder, then service names
            if subcmd == "run":
                rest_words = rest_text.split()
                if not rest_words or (len(rest_words) == 1 and " " not in rest_text):
                    yield from self._folder_completions(rest_text)
                    return
                # After folder word → service names
                folder_word = rest_words[0]
                resolved = self._resolve_folder_from_word(folder_word)
                if resolved and len(rest_words) <= 2:
                    services = self._get_services_for_folder(*resolved)
                    partial = rest_words[1] if len(rest_words) == 2 else ""
                    query = partial.lower()
                    for svc in services:
                        if not query or query in svc:
                            yield Completion(svc, start_position=-len(partial))
                return

            # /agent connect — folder names
            if subcmd == "connect":
                yield from self._folder_completions(rest_text)
                return

            # /agent disconnect — folder, then service names
            if subcmd == "disconnect":
                rest_words = rest_text.split()
                if not rest_words or (len(rest_words) == 1 and " " not in rest_text):
                    yield from self._folder_completions(rest_text)
                    return
                folder_word = rest_words[0]
                resolved = self._resolve_folder_from_word(folder_word)
                if resolved and len(rest_words) <= 2:
                    services = self._get_services_for_folder(*resolved)
                    partial = rest_words[1] if len(rest_words) == 2 else ""
                    query = partial.lower()
                    for svc in services:
                        if not query or query in svc:
                            yield Completion(svc, start_position=-len(partial))
                return

            # Backwards compat: /agent FOLDER — treat first word as folder
            resolved = self._resolve_folder_from_word(subcmd)
            if resolved:
                # @note completion
                if "@" in rest_text:
                    space, group_slug = resolved
                    at_pos = rest_text.rfind("@")
                    partial_title = rest_text[at_pos + 1:]
                    query = partial_title.lower()
                    for title, date in self._get_notes_in_folder(space, group_slug):
                        if not query or query in title.lower():
                            yield Completion(
                                f"@{title}",
                                start_position=-(len(partial_title) + 1),
                                display_meta=date,
                            )
                    return
                # Service names
                rest_words = rest_text.split()
                if len(rest_words) <= 1:
                    services = self._get_services_for_folder(*resolved)
                    partial = rest_words[0] if rest_words else ""
                    query = partial.lower()
                    for svc in services:
                        if not query or query in svc:
                            yield Completion(svc, start_position=-len(partial))
            return


class _TodoItemCompleter(Completer):
    """Autocomplete open todo task text. Today's items shown first."""

    def __init__(self, items: list[dict], today_ids: set[int]) -> None:
        self._items = items
        self._today_ids = today_ids

    def get_completions(self, document, complete_event):
        text = document.text_before_cursor.lower()
        # Sort: today items first, then by due date
        sorted_items = sorted(self._items, key=lambda i: (
            i["id"] not in self._today_ids,
            i.get("due") is None,
            i.get("due") or "",
        ))
        for item in sorted_items:
            task = item["task"]
            if text in task.lower():
                display_meta = item.get("_folder_display", "")
                yield Completion(
                    str(item["id"]),
                    start_position=-len(document.text_before_cursor),
                    display=task[:60],
                    display_meta=display_meta,
                )


class _FolderPathCompleter(Completer):
    """Tab-complete folder paths with hierarchical drill-down.

    Returns full paths (e.g. 'clients/sanity') with display names.
    Used by /todo add and anywhere a folder path prompt is needed.
    """

    def __init__(self, folders: list[tuple[str, str, str]]) -> None:
        # (slug, display_name, space)
        self._folders = folders

    def get_completions(self, document, complete_event):
        partial = document.text_before_cursor
        query = partial.strip().lower()
        for slug, display, space in self._folders:
            full_path = space if slug == space else f"{space}/{slug}"
            is_space = slug == space

            if not query:
                # Empty → show only top-level spaces
                if is_space:
                    yield Completion(
                        full_path,
                        start_position=-len(partial),
                        display_meta=display,
                    )
            elif "/" in query:
                # Has slash → prefix match for drill-down
                if full_path.lower().startswith(query) and full_path.lower() != query.rstrip("/"):
                    yield Completion(
                        full_path,
                        start_position=-len(partial),
                        display_meta=display,
                    )
            else:
                # No slash → fuzzy match all levels
                if query in slug.lower() or query in display.lower() or query in full_path.lower():
                    yield Completion(
                        full_path,
                        start_position=-len(partial),
                        display_meta=display,
                    )


class _ConnectFolderCompleter(Completer):
    """Tab-complete folder names for /connect, with 'all' option."""

    def __init__(self, folders: list[tuple[str, str, str]]) -> None:
        # (slug, display_name, space)
        self._folders = folders

    def get_completions(self, document, complete_event):
        text = document.text_before_cursor.strip().lower()
        # "all" as a special option
        if not text or "all".startswith(text):
            yield Completion(
                "all",
                start_position=-len(document.text_before_cursor),
                display_meta="All folders (global default)",
            )
        for slug, display, space in self._folders:
            if not text or text in slug.lower() or text in display.lower():
                meta = display if slug == space else f"{display} ({space})"
                yield Completion(
                    slug,
                    start_position=-len(document.text_before_cursor),
                    display_meta=meta,
                )


def _make_agent_note_completer(config: NotelyConfig, space: str, group_slug: str):
    """Create a completer for @note titles in agent mode."""

    class _AgentNoteCompleter(Completer):
        def __init__(self):
            self._notes: list[tuple[str, str]] | None = None

        def _load_notes(self) -> list[tuple[str, str]]:
            if self._notes is None:
                try:
                    with Database(config.db_path) as db:
                        db.initialize()
                        ctx = db.get_folder_context(space, group_slug)
                    self._notes = [
                        (n["title"], n["date"])
                        for n in ctx.get("notes", [])
                    ]
                except Exception:
                    self._notes = []
            return self._notes

        def get_completions(self, document, complete_event):
            text = document.text_before_cursor
            if "@" not in text:
                return
            at_pos = text.rfind("@")
            partial = text[at_pos + 1:]
            query = partial.lower()
            for title, date in self._load_notes():
                if not query or query in title.lower():
                    yield Completion(
                        f"@{title}",
                        start_position=-(len(partial) + 1),
                        display_meta=date,
                    )

    return _AgentNoteCompleter()


class _TodoCommandCompleter(Completer):
    """Autocomplete subcommands in todo mode.

    Typing 'd' suggests 'done', 'a' suggests 'add', etc.
    After 'add ', provides folder autocomplete so users can type
    'add clients/sanity My new task' in one line.
    """

    _COMMANDS = [
        ("done", "Mark a todo as done"),
        ("add", "Add a new todo"),
        ("today", "Flag items for today"),
        ("due", "View sorted by due date"),
        ("timer", "Start timer on a todo"),
        ("assign", "Change owner"),
        ("move", "Move to another folder"),
        ("plan", "Pick today's focus"),
        ("all", "Show everyone's todos"),
        ("refresh", "Reload list"),
        ("q", "Exit todo mode"),
    ]

    def __init__(self, folders: list[tuple[str, str, str]] | None = None):
        self._folders = folders or []

    def get_completions(self, document, complete_event):
        text = document.text_before_cursor
        stripped = text.strip().lower()
        if not stripped:
            return

        # After 'add ' → folder autocomplete for the second word
        # After 'move N ' → folder autocomplete for the third word
        folder_partial = None
        if stripped.startswith("add ") and self._folders:
            parts = text.split(None, 2)
            if len(parts) <= 2:
                folder_partial = parts[1] if len(parts) == 2 else ""
        elif stripped.startswith("move ") and self._folders:
            parts = text.split(None, 3)  # ['move', num, partial_folder?, ...]
            if len(parts) == 3:
                folder_partial = parts[2]
            elif len(parts) == 2 and not parts[1][0].isdigit():
                # 'move san...' — folder without number
                folder_partial = parts[1]

        if folder_partial is not None:
            query = folder_partial.lower()
            for slug, display, space in self._folders:
                full_path = space if slug == space else f"{space}/{slug}"
                is_space = slug == space

                if not query:
                    if is_space:
                        yield Completion(
                            full_path,
                            start_position=-len(folder_partial),
                            display_meta=display,
                        )
                elif "/" in query:
                    if full_path.lower().startswith(query) and full_path.lower() != query.rstrip("/"):
                        yield Completion(
                            full_path,
                            start_position=-len(folder_partial),
                            display_meta=display,
                        )
                else:
                    if query in slug.lower() or query in display.lower() or query in full_path.lower():
                        yield Completion(
                            full_path,
                            start_position=-len(folder_partial),
                            display_meta=display,
                        )
            return

        # First word → subcommand completion
        if " " in stripped:
            return
        for cmd, desc in self._COMMANDS:
            if cmd.startswith(stripped):
                yield Completion(
                    cmd,
                    start_position=-len(text),
                    display_meta=desc,
                )
