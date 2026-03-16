"""Tab completion classes for notely open."""

from __future__ import annotations

from prompt_toolkit.completion import Completer, Completion

from ...config import NotelyConfig
from ...db import Database
from ._shared import _get_all_folders


class _SlashCompleter(Completer):
    """Tab-complete slash commands, /chat folder names, and /agent folder + @note."""

    # Static commands — database commands are added dynamically
    _STATIC_COMMANDS = [
        "/agent", "/chat", "/clip", "/folder", "/inbox", "/timer", "/todo",
        "/ideas", "/list", "/search", "/spaces", "/mkdir", "/rmdir",
        "/delete", "/edit", "/secret", "/sync", "/workflow", "/help", "/quit",
    ]

    def __init__(self, config: NotelyConfig) -> None:
        self._config = config
        self._folder_cache: list[tuple[str, str, str]] | None = None
        self._note_cache: dict[str, list[tuple[str, str]]] = {}
        self._todo_cache: list[dict] | None = None
        self._recent_notes_cache: list[dict] | None = None
        self._secret_cache: dict[str, dict[str, str]] | None = None
        self._db_names_cache: set[str] | None = None

    def _get_database_names(self) -> set[str]:
        """Get all database names that have records in the DB."""
        if self._db_names_cache is None:
            names: set[str] = set()
            try:
                with Database(self._config.db_path) as db:
                    db.initialize()
                    names = set(db.get_database_names())
            except Exception:
                pass
            self._db_names_cache = names
        return self._db_names_cache

    @property
    def COMMANDS(self) -> list[str]:
        """Dynamic command list including database names."""
        db_cmds = [f"/{name}" for name in sorted(self._get_database_names())]
        return self._STATIC_COMMANDS + db_cmds

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
        self._db_names_cache = None

    def invalidate_notes(self) -> None:
        """Clear note cache after save/delete."""
        self._recent_notes_cache = None

    def invalidate_todos(self) -> None:
        """Clear todo cache after /done or new note with action items."""
        self._todo_cache = None
        self._recent_notes_cache = None

    def _get_open_todos(self) -> list[dict]:
        """Get YOUR open todos. Cached until invalidated."""
        if self._todo_cache is None:
            try:
                with Database(self._config.db_path) as db:
                    db.initialize()
                    self._todo_cache = db.get_todos_filtered(
                        owner=self._config.user_name,
                    )
            except Exception:
                self._todo_cache = []
        return self._todo_cache

    def _get_done_todos(self) -> list[dict]:
        """Get YOUR recently completed todos for /todo reopen."""
        try:
            with Database(self._config.db_path) as db:
                db.initialize()
                return db.get_todos_filtered(
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
    ) -> list[tuple[str, str, str]]:
        """Get (id, title, date) for notes in a folder. Cached by space/group_slug."""
        cache_key = f"{space}/{group_slug}"
        if cache_key not in self._note_cache:
            try:
                with Database(self._config.db_path) as db:
                    db.initialize()
                    ctx = db.get_folder_context(space, group_slug)
                self._note_cache[cache_key] = [
                    (n["id"], n["title"], n["date"])
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

        # Complete /list, /ideas — folder names
        for prefix in ("/list ", "/ideas "):
            if text.lower().startswith(prefix):
                after = text[len(prefix):]
                if " " not in after:
                    yield from self._folder_completions(after)
                return

        # Complete /search — folder with drill-down to notes
        if text.lower().startswith("/search "):
            after = text[8:]
            words = after.split(None, 1)

            # After folder + space: show notes in that folder
            if len(words) == 2 or (len(words) == 1 and after.endswith(" ")):
                folder_word = words[0]
                resolved = self._resolve_folder_from_word(folder_word)
                if resolved:
                    space, group_slug = resolved
                    partial_title = (words[1] if len(words) > 1 else "").lower()
                    for nid, title, date in self._get_notes_in_folder(space, group_slug):
                        if not partial_title or partial_title in title.lower():
                            yield Completion(
                                title[:60],
                                start_position=-len(words[1]) if len(words) > 1 else 0,
                                display_meta=f"({date})",
                            )
                return

            # First word: folders with drill-down
            partial = words[0] if words else ""
            folder_key = partial.rstrip("/")

            # Exact folder match ending with / → show notes inside
            if partial.endswith("/") and folder_key and self._resolve_folder_from_word(folder_key):
                space, group_slug = self._resolve_folder_from_word(folder_key)
                for nid, title, date in self._get_notes_in_folder(space, group_slug):
                    yield Completion(
                        f"{folder_key}/{title[:60]}",
                        start_position=-len(partial),
                        display_meta=f"({date})",
                    )

            # Matching folders — always append / for drill-down
            for comp in self._folder_completions(partial):
                ctext = comp.text
                if not ctext.endswith("/"):
                    ctext += "/"
                yield Completion(
                    ctext,
                    start_position=comp.start_position,
                    display_meta=comp.display_meta,
                )
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

        # Complete /todo — subcommands + folders, then IDs for done/reopen
        if text.lower().startswith("/todo "):
            after = text[6:]
            if " " not in after:
                # First word: subcommands + folder completions
                partial = after.lower()
                for sub, hint in (
                    ("done", "mark a todo as done"),
                    ("reopen", "reopen a completed todo"),
                    ("all", "show all open todos"),
                ):
                    if sub.startswith(partial):
                        yield Completion(sub, start_position=-len(after), display_meta=hint)
                # Also offer folder completions for /todo FOLDER scoping
                yield from self._folder_completions(after)
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

        # Complete /<database> — subcommands + entity names for any known database
        for db_name in self._get_database_names():
            db_prefix = f"/{db_name} "
            if text.lower().startswith(db_prefix):
                after = text[len(db_prefix):]
                if " " not in after:
                    partial = after.lower()
                    for sub, hint in (
                        ("add", "add a record"),
                        ("show", "show entity details"),
                        ("delete", "delete a record"),
                        ("search", "search records"),
                    ):
                        if sub.startswith(partial):
                            yield Completion(sub, start_position=-len(after), display_meta=hint)
                    # Entity names
                    try:
                        with Database(self._config.db_path) as db:
                            db.initialize()
                            records = db.get_database_records(db_name)
                        seen: set[str] = set()
                        for r in records:
                            name = r["entity"]
                            if name not in seen and (not partial or partial in name.lower()):
                                seen.add(name)
                                yield Completion(name, start_position=-len(after))
                    except Exception:
                        pass
                else:
                    words = after.split(None, 1)
                    first = words[0].lower()
                    rest = words[1] if len(words) > 1 else ""
                    if first in ("show", "delete"):
                        partial = rest.strip().lower()
                        try:
                            with Database(self._config.db_path) as db:
                                db.initialize()
                                records = db.get_database_records(db_name)
                            seen: set[str] = set()
                            for r in records:
                                name = r["entity"]
                                if name not in seen and (not partial or partial in name.lower()):
                                    seen.add(name)
                                    yield Completion(name, start_position=-len(rest))
                        except Exception:
                            pass
                return

        # Complete /delete and /edit — folder→note drill-down
        # Typing flow: /delete → folders → pick folder → notes in that folder
        # No trailing space on folders — user types / for subfolders or space for notes
        for prefix in ("/delete ", "/edit "):
            if text.lower().startswith(prefix):
                after = text[len(prefix):]
                words = after.split(None, 1)

                # After a space: note ID filter within the resolved folder
                if len(words) == 2 or (len(words) == 1 and after.endswith(" ")):
                    folder_word = words[0]
                    resolved = self._resolve_folder_from_word(folder_word)
                    if resolved:
                        space, group_slug = resolved
                        partial_title = (words[1] if len(words) > 1 else "").lower()
                        for nid, title, date in self._get_notes_in_folder(space, group_slug):
                            if not partial_title or partial_title in title.lower():
                                yield Completion(
                                    nid,
                                    start_position=-len(words[1]) if len(words) > 1 else 0,
                                    display_meta=f"{title[:50]} ({date})",
                                )
                    return

                # Still typing first word — show folders + notes for exact matches
                partial = words[0] if words else ""
                folder_key = partial.rstrip("/")

                # If text exactly matches a folder, show notes from it
                if folder_key and self._resolve_folder_from_word(folder_key):
                    space, group_slug = self._resolve_folder_from_word(folder_key)
                    for nid, title, date in self._get_notes_in_folder(space, group_slug):
                        yield Completion(
                            f"{folder_key} {nid}",
                            start_position=-len(partial),
                            display_meta=f"{title[:50]} ({date})",
                        )

                # Always show matching folders — append / to space-level entries
                # so selecting "projects" becomes "projects/" for instant drill-down
                for comp in self._folder_completions(partial):
                    ctext = comp.text
                    if "/" not in ctext:
                        ctext += "/"
                    yield Completion(
                        ctext,
                        start_position=comp.start_position,
                        display_meta=comp.display_meta,
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
                        for _, title, date in self._get_notes_in_folder(space, group_slug):
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
                    for _, title, date in self._get_notes_in_folder(space, group_slug):
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

        # @note reference — folder→note autocomplete in the main prompt
        # Type @ → folders, select folder + space → note titles in that folder
        if not text.strip().startswith("/") and "@" in text:
            yield from self._at_note_completions(text)
            return

    def _at_note_completions(self, text: str):
        """Yield @note completions: folder first, then note titles."""
        at_pos = text.rfind("@")
        after_at = text[at_pos + 1:]
        words = after_at.split(None, 1)

        # Phase 1: folder completion (no words yet, or typing first word)
        if not words or (len(words) == 1 and not after_at.endswith(" ")):
            partial = words[0] if words else ""
            for comp in self._folder_completions(partial):
                yield Completion(
                    comp.text,
                    start_position=-len(after_at),
                    display_meta=comp.display_meta,
                )
            return

        # Phase 2: note title completion within the selected folder
        folder_word = words[0]
        resolved = self._resolve_folder_from_word(folder_word)
        if resolved:
            space, group_slug = resolved
            partial_title = (words[1] if len(words) > 1 else "").lower()
            for _, title, date in self._get_notes_in_folder(space, group_slug):
                if not partial_title or partial_title in title.lower():
                    yield Completion(
                        title,
                        start_position=-len(after_at),
                        display_meta=date,
                    )


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
        ("delete", "Delete a todo permanently"),
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

    def __init__(
        self,
        folders: list[tuple[str, str, str]] | None = None,
        has_default_folder: bool = False,
    ):
        self._folders = folders or []
        self._has_default_folder = has_default_folder
        # Todo fields for key= autocomplete
        self._todo_fields = ["task", "owner", "due", "description"]

    def _yield_field_completions(self, after_add: str):
        """Yield key= field suggestions for the text after 'add '."""
        set_fields: set[str] = set()
        for token in after_add.split():
            if "=" in token:
                set_fields.add(token.split("=", 1)[0].lower())
        if after_add.endswith(" ") or not after_add:
            partial = ""
        else:
            partial = after_add.rsplit(None, 1)[-1]
        if "=" in partial:
            return
        partial_lower = partial.lower()
        for field in self._todo_fields:
            if field.lower() not in set_fields:
                suggestion = f"{field}="
                if not partial or field.lower().startswith(partial_lower):
                    yield Completion(
                        suggestion,
                        start_position=-len(partial),
                        display_meta=f"set {field}",
                    )

    def get_completions(self, document, complete_event):
        text = document.text_before_cursor
        stripped = text.strip().lower()
        if not stripped:
            return

        # After 'add ' → field or folder completions depending on context
        # After 'move N ' → folder autocomplete for the third word
        # Use text.lower() (not stripped) to detect trailing space after 'add'
        text_lower = text.lstrip().lower()
        folder_partial = None
        if text_lower.startswith("add "):
            after_add = text.split(None, 1)[1] if len(text.split(None, 1)) > 1 else ""
            if self._has_default_folder:
                # Folder already scoped — show field completions only
                yield from self._yield_field_completions(after_add)
                return
            # No default folder — first word is folder, rest is free-form
            parts = text.split(None, 2)
            if len(parts) <= 2 and self._folders:
                folder_partial = parts[1] if len(parts) == 2 else ""
            elif len(parts) > 2:
                # Past folder word — show field completions
                yield from self._yield_field_completions(parts[2] if len(parts) > 2 else "")
                return
        elif text_lower.startswith("move ") and self._folders:
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


class _AddFieldCompleter(Completer):
    """Suggest remaining `key=` completions while typing a record to add."""

    def __init__(self, fields: list[str]):
        self._fields = fields

    def get_completions(self, document, complete_event):
        text = document.text_before_cursor

        # Find which fields are already set (have key=something)
        set_fields: set[str] = set()
        for token in text.split():
            if "=" in token:
                key = token.split("=", 1)[0].lower()
                set_fields.add(key)

        # Current partial word
        if text.endswith(" ") or not text:
            partial = ""
        else:
            partial = text.rsplit(None, 1)[-1]

        # If partial contains '=', user is typing a value — no completion
        if "=" in partial:
            return

        partial_lower = partial.lower()
        for field in self._fields:
            if field.lower() in set_fields:
                continue
            suggestion = f"{field}="
            if not partial or field.lower().startswith(partial_lower):
                yield Completion(
                    suggestion,
                    start_position=-len(partial),
                    display_meta=f"set {field}",
                )
