"""Standardized interactive prompt patterns.

Leaf module — depends only on ``rich``.  No notely internal imports.

Three reusable patterns + two small helpers cover every interactive
confirmation in the codebase:

* ``confirm_action``   — preview-then-confirm loop (save / merge / edit / revise / drop)
* ``pick_from_list``   — numbered choices with letter extras and optional free text
* ``duplicate_found``  — consistent "update / new / skip" for all duplicate types

* ``confirm_destructive`` — simple yes / no for deletions
* ``no_changes_retry``    — "describe what's new / skip" after empty merge
"""

from __future__ import annotations

import os
import select
import sys
from typing import Any, Callable

from rich.console import Console
from rich.prompt import Prompt

_console = Console()


def _drain_stdin() -> None:
    """Drain leftover bytes from stdin to prevent auto-accepting prompts.

    After prompt-toolkit exits raw mode or after Rich Prompt.ask returns,
    leftover bytes (newlines, escape sequences, routing choice echoes) can
    cause the next Prompt.ask to return immediately with garbage input.
    """
    try:
        fd = sys.stdin.fileno()
        while select.select([fd], [], [], 0.05)[0]:
            os.read(fd, 4096)
    except (OSError, ValueError):
        pass


# ---------------------------------------------------------------------------
# confirm_action — preview → prompt → branch → re-preview loop
# ---------------------------------------------------------------------------

def confirm_action(
    preview_fn: Callable[[], None],
    verb: str = "save",
    edit_fn: Callable[[], None] | None = None,
    revise_fn: Callable[[], None] | None = None,
    drop_fn: Callable[[], bool] | None = None,
    drop_label: str = "drop items",
    console: Console | None = None,
) -> bool:
    """Show a preview and ask the user to confirm, edit, revise, or skip.

    Dynamically builds the option string from which callbacks are provided.

    Args:
        preview_fn: Called to render the current preview panel.
        verb: Action word shown after ``[Y]es,`` — typically ``"save"`` or ``"merge"``.
        edit_fn: If provided, adds ``[e]dit`` option.  Called when user picks ``e``.
            Should mutate shared state that ``preview_fn`` also reads.
        revise_fn: If provided, adds ``[r]evise with AI``.
        drop_fn: If provided, adds ``[d]rop`` option.
            Must return ``False`` if all items were dropped (cancels the action).
        drop_label: Label shown after ``[d]`` — default ``"drop items"``.
        console: Rich Console to use.  Falls back to module-level default.

    Returns:
        ``True`` if the user confirmed (``Y``), ``False`` if skipped / cancelled.
    """
    con = console or _console

    # Drain leftover stdin bytes that could auto-accept the prompt.
    # This happens after large pastes or routing prompts — leftover
    # newlines/escape sequences cause Prompt.ask to return immediately.
    _drain_stdin()

    while True:
        preview_fn()

        # Build option string dynamically
        parts = [rf"\[Y]es, {verb}"]
        if edit_fn is not None:
            parts.append(r"\[e]dit")
        if revise_fn is not None:
            parts.append(r"\[r]evise with AI")
        if drop_fn is not None:
            # drop_label should start with 'd' — we wrap first char in brackets
            parts.append(rf"\[{drop_label[0]}]{drop_label[1:]}")
        parts.append(r"\[n]o, skip")

        try:
            choice = Prompt.ask(" / ".join(parts), default="Y")
        except (KeyboardInterrupt, EOFError):
            con.print("\n[yellow]Cancelled.[/yellow]")
            return False

        ch = choice.lower()

        if ch in ("y", ""):
            return True

        if ch == "n":
            con.print("[yellow]Skipped.[/yellow]")
            return False

        if ch == "e" and edit_fn is not None:
            edit_fn()
            continue

        if ch == "r" and revise_fn is not None:
            revise_fn()
            continue

        if ch == "d" and drop_fn is not None:
            has_items = drop_fn()
            if not has_items:
                return False
            continue

    # Unreachable, but keeps mypy happy
    return False  # pragma: no cover


# ---------------------------------------------------------------------------
# pick_from_list — numbered items + letter extras + optional free text
# ---------------------------------------------------------------------------

def pick_from_list(
    items: list[tuple[str, str]],
    extras: list[tuple[str, str]] | None = None,
    prompt_text: str = "Choice",
    default: str = "1",
    allow_text: bool = False,
    text_hint: str = "Or type a folder path (e.g. clients/acme)",
    console: Console | None = None,
) -> str | None:
    """Show numbered choices plus letter extras.  Returns the user's pick.

    Args:
        items: List of ``(key, label)`` tuples.  Displayed as ``[1] label``.
        extras: Letter-key extras at the bottom, e.g. ``[("n", "New"), ("s", "Skip")]``.
        prompt_text: Text shown on the input line.
        default: Default value if user presses Enter.
        allow_text: If ``True``, show ``text_hint`` and return raw text for
            non-numeric / non-letter input.
        text_hint: Displayed when ``allow_text`` is ``True``.
        console: Rich Console to use.

    Returns:
        * A digit string ``"1"``..``"N"`` for a numbered choice.
        * A letter string ``"n"``/``"s"``/etc. for an extra.
        * Raw text string when ``allow_text=True`` and input didn't match above.
        * ``None`` if the user cancelled (Ctrl-C / EOF).
    """
    con = console or _console
    _drain_stdin()

    # Numbered items
    for i, (_key, label) in enumerate(items, 1):
        con.print(f"  [{i}] {label}")

    # Letter extras
    if extras:
        extra_keys: set[str] = set()
        for key, label in extras:
            con.print(rf"  \[{key}] {label}")
            extra_keys.add(key.lower())
    else:
        extra_keys = set()

    if allow_text:
        con.print(f"[dim]  {text_hint}[/dim]")

    try:
        raw = Prompt.ask(prompt_text, default=default)
    except (KeyboardInterrupt, EOFError):
        return None

    raw_stripped = raw.strip()

    # Check numbered
    try:
        num = int(raw_stripped)
        if 1 <= num <= len(items):
            return str(num)
    except ValueError:
        pass

    # Check extras
    if raw_stripped.lower() in extra_keys:
        return raw_stripped.lower()

    # Free text
    if allow_text and raw_stripped:
        return raw_stripped

    return None


# ---------------------------------------------------------------------------
# duplicate_found — always the same 3 choices
# ---------------------------------------------------------------------------

_MATCH_LABELS = {
    "exact": "Exact match",
    "near": "This looks like",
    "similar": "You may already have this",
    "related": "Related note found",
}


def duplicate_found(
    title: str,
    date: str,
    match_type: str = "exact",
    console: Console | None = None,
) -> str:
    """Prompt the user when a duplicate is found.

    Args:
        title: Title of the existing note.
        date: Date of the existing note.
        match_type: ``"exact"``, ``"near"``, or ``"similar"``.
        console: Rich Console to use.

    Returns:
        ``"update"``, ``"new"``, ``"records"``, or ``"skip"``.
    """
    con = console or _console
    _drain_stdin()

    label = _MATCH_LABELS.get(match_type, match_type)
    con.print(f"\n[dim]{label}:[/dim] '{title}' ({date})")

    try:
        choice = Prompt.ask(
            r"\[u]pdate / \[n]ew / \[r]ecords only / \[s]kip",
            default="", show_default=False,
        )
    except (KeyboardInterrupt, EOFError):
        return "skip"

    ch = choice.lower()
    if ch == "u":
        return "update"
    if ch == "n":
        return "new"
    if ch == "r":
        return "records"
    return "skip"


# ---------------------------------------------------------------------------
# confirm_destructive — simple yes / no
# ---------------------------------------------------------------------------

def confirm_destructive(
    message: str,
    default_no: bool = True,
    console: Console | None = None,
) -> bool:
    """Ask a simple yes/no confirmation for a destructive action.

    Args:
        message: Displayed with ``[red]`` styling.
        default_no: If ``True`` the default answer is "no".
        console: Rich Console to use.

    Returns:
        ``True`` if the user confirmed, ``False`` otherwise.
    """
    con = console or _console
    default = "n" if default_no else "y"
    try:
        choice = Prompt.ask(
            rf"[red]{message}[/red] \[y/N]" if default_no else rf"[red]{message}[/red] \[Y/n]",
            default=default,
        )
    except (KeyboardInterrupt, EOFError):
        con.print("\n[dim]Cancelled.[/dim]")
        return False
    return choice.lower() == "y"


# ---------------------------------------------------------------------------
# no_changes_retry — describe what's new or skip
# ---------------------------------------------------------------------------

def no_changes_retry(console: Console | None = None) -> str:
    """Prompt after an AI merge found no new information.

    Returns:
        ``"d"`` to describe what's new, ``"s"`` to skip.
    """
    con = console or _console
    try:
        choice = Prompt.ask(
            r"\[d]escribe what's new / \[s]kip",
            default="s",
        )
    except (KeyboardInterrupt, EOFError):
        return "s"
    return choice.lower() if choice.lower() in ("d", "s") else "s"
