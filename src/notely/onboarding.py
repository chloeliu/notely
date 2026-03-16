"""Interactive onboarding — freeform questions, AI generates config."""

from __future__ import annotations

import json
import logging
from typing import Any

import anthropic
from rich.console import Console
from rich.panel import Panel

console = Console()
logger = logging.getLogger(__name__)

ONBOARDING_TOOL = {
    "name": "generate_notely_config",
    "description": "Generate a notely config.toml based on the user's answers about how they work.",
    "input_schema": {
        "type": "object",
        "required": ["spaces", "starter_folders"],
        "properties": {
            "spaces": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["slug", "display_name", "description", "group_by"],
                    "properties": {
                        "slug": {
                            "type": "string",
                            "description": "URL-safe slug for the space (lowercase, hyphens ok). e.g., 'clients', 'ideas', 'research'",
                        },
                        "display_name": {
                            "type": "string",
                            "description": "Human-readable name. e.g., 'Client Work', 'Ideas & Content'",
                        },
                        "description": {
                            "type": "string",
                            "description": "What this space is for, in one sentence",
                        },
                        "group_by": {
                            "type": "string",
                            "description": "How notes are grouped into folders. e.g., 'client', 'project', 'category', 'topic', 'course', 'date'. Use 'date' for flat/ungrouped spaces.",
                        },
                        "subgroup_by": {
                            "type": ["string", "null"],
                            "description": "Optional second-level grouping. e.g., 'topic' within each client. Null if not needed.",
                        },
                        "fields": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Extra fields for this space. Options: 'participants', 'action_items', 'content_status', 'source_ref', 'source_url'. Use 'action_items' if the user tracks tasks/todos. Use 'content_status' for ideas/content pipeline spaces.",
                        },
                        "is_inbox": {
                            "type": "boolean",
                            "description": "True if this is a catch-all space for misc thoughts",
                        },
                    },
                },
            },
            "starter_folders": {
                "type": "object",
                "description": "Starter folders to create. Keys are space slugs, values are arrays of folder names mentioned by the user. e.g., {'clients': ['Acme Corp', 'Globex']}",
                "additionalProperties": {
                    "type": "array",
                    "items": {"type": "string"},
                },
            },
        },
    },
}


def _save_api_key(target_dir: "Path", key: str) -> None:
    """Save API key to .env file in the notely directory."""
    from pathlib import Path
    target_dir = Path(target_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    env_path = target_dir / ".env"
    env_path.write_text(f"ANTHROPIC_API_KEY={key}\n", encoding="utf-8")
    console.print(f"[dim]API key saved to {env_path}[/dim]")


QUESTIONS = [
    (
        "what_you_do",
        "What do you do? What kind of notes do you need to keep track of?",
        "e.g., \"I run a consulting business and also write a newsletter\"",
    ),
    (
        "how_notes_happen",
        "Where do your notes come from? How do you usually capture them?",
        "e.g., \"mostly meetings and Slack, sometimes I just have ideas on walks\"",
    ),
    (
        "specifics",
        "Anything specific you want organized from the start?",
        "e.g., \"I have 3 clients: Acme, Globex, and DataFlow\"",
    ),
]


def _ask(prompt: str = "> ", multiline: bool = False) -> str:
    """Read input. For multiline: Enter submits, Shift+Enter adds new lines.

    Uses prompt_toolkit for multiline input with bracket paste support.
    """
    if not multiline:
        try:
            import readline  # noqa: F401 — enables arrow keys, history, editing
        except ImportError:
            pass
        return input(prompt)

    from prompt_toolkit import PromptSession
    from prompt_toolkit.key_binding import KeyBindings

    bindings = KeyBindings()

    @bindings.add('enter', eager=True)
    def handle_enter(event):
        buf = event.current_buffer
        current_line = buf.document.current_line
        # Empty line with content above — submit
        if current_line.strip() == '' and buf.text.strip():
            from prompt_toolkit.document import Document
            stripped = buf.text.rstrip('\n')
            buf.document = Document(stripped, len(stripped))
            buf.validate_and_handle()
        else:
            buf.insert_text('\n')

    session = PromptSession()
    text = session.prompt(prompt, multiline=True, key_bindings=bindings,
                          prompt_continuation='. ')
    # Strip trailing blank lines
    lines = text.split('\n')
    while lines and lines[-1].strip() == '':
        lines.pop()
    return '\n'.join(lines)


def _collect_answers() -> list[tuple[str, str]]:
    """Ask questions, review answers, let user redo any before proceeding."""
    answers: dict[str, str] = {}

    # First pass — ask all questions
    for key, question, hint in QUESTIONS:
        console.print()
        console.print(f"[bold]{question}[/bold]")
        console.print(f"[dim]{hint}[/dim]")
        console.print("[dim]Enter for new lines, Enter twice to submit.[/dim]")
        answers[key] = _ask(multiline=True)

    # Review — show all answers, let user redo
    while True:
        console.print()
        lines = ["[bold]Your answers:[/bold]", ""]
        for i, (key, question, _hint) in enumerate(QUESTIONS, 1):
            lines.append(f"  [bold]{i}.[/bold] {question}")
            lines.append(f"     [green]{answers[key]}[/green]")
            lines.append("")
        console.print(Panel("\n".join(lines), border_style="dim"))

        choice = _ask("[Y] Continue / [1-3] Redo an answer: ")

        if choice.strip().lower() in ("y", ""):
            return [(key, answers[key]) for key, _, _ in QUESTIONS]

        if choice.strip() in ("1", "2", "3"):
            idx = int(choice.strip()) - 1
            key, question, hint = QUESTIONS[idx]
            console.print(f"\n[bold]{question}[/bold]")
            console.print(f"[dim]{hint}[/dim]")
            console.print("[dim]Enter for new lines, Enter twice to submit.[/dim]")
            answers[key] = _ask(multiline=True)
        else:
            console.print("[yellow]Enter Y or a number 1-3.[/yellow]")


def run_onboarding(target_dir: "Path | None" = None) -> str:
    """Run the interactive onboarding and return a config.toml string."""
    from pathlib import Path

    console.print()
    console.print(
        Panel(
            "[bold]Welcome to Notely[/bold]\n\n"
            "Tell me a bit about how you work and I'll set things up.\n"
            "Just answer in your own words — you can always change this later.",
            border_style="blue",
            width=65,
        )
    )

    # ── Get user's name ─────────────────────────────────────────────
    console.print()
    console.print("[bold]What's your first name?[/bold]")
    console.print("[dim]Used to identify your todos vs. others'[/dim]")
    user_name = _ask().strip()

    # ── Collect freeform answers ──────────────────────────────────────
    answers = _collect_answers()

    # ── Send to AI ────────────────────────────────────────────────────
    console.print()
    console.print("[dim]Setting things up...[/dim]")

    try:
        ai_result = _call_ai(answers)
    except Exception as e:
        error_msg = str(e)
        if "api_key" in error_msg.lower() or "auth" in error_msg.lower():
            console.print()
            console.print("[yellow]Anthropic API key not found.[/yellow]")
            console.print("Notely uses Claude to set up your workspace.")
            console.print("Get a key at: [bold]https://console.anthropic.com/settings/keys[/bold]")
            console.print()
            key = _ask("Paste your API key (or Enter to cancel): ")
            if not key.strip():
                raise SystemExit(1)
            import os
            os.environ["ANTHROPIC_API_KEY"] = key.strip()
            # Save to .env so it persists
            if target_dir:
                _save_api_key(target_dir, key.strip())
            console.print("[dim]Setting things up...[/dim]")
            ai_result = _call_ai(answers)  # let it raise if still broken
        else:
            raise

    spaces = ai_result.get("spaces", [])
    starters = ai_result.get("starter_folders", {})

    return _review_loop(spaces, starters, target_dir, user_name=user_name)


def _review_loop(
    spaces: list[dict],
    starters: dict[str, list[str]],
    target_dir: "Path | None",
    user_name: str | None = None,
) -> str:
    """Show summary, let user approve/edit/restart."""
    while True:
        config = _build_config(spaces, starters, user_name=user_name)
        _show_summary(spaces, starters, target_dir)

        console.print()
        choice = _ask("[Y] Looks good / [e] Edit / [n] Start over: ")

        if choice.strip().lower() in ("y", ""):
            return config
        elif choice.strip().lower() == "e":
            config = _edit_config(spaces, starters, target_dir, user_name=user_name)
            return config
        else:
            console.print("[yellow]No worries — let's start fresh.[/yellow]")
            return run_onboarding(target_dir)


def _call_ai(answers: list[tuple[str, str]]) -> dict[str, Any]:
    """Call Claude to generate a notely config from freeform answers."""
    client = anthropic.Anthropic()

    answers_text = "\n".join(f"**{label}**: {answer}" for label, answer in answers)

    system = """You are helping set up Notely, a structured note system. Based on the user's answers about how they work, generate a config with the right spaces and organization.

Guidelines:
- Create 2-4 spaces based on what the user described. Don't over-engineer.
- Each space should map to a distinct area of their work/life.
- Pick group_by values that match how they'd naturally look for notes later.
- Only use subgroup_by if there's clearly a second level of organization.
- Add 'action_items' and 'participants' fields for work/meeting-heavy spaces.
- Add 'content_status' field for spaces about ideas or content creation.
- If they mention specific names (clients, projects, courses), include them as starter_folders.
- If they want something simple, keep it simple. Don't add spaces they didn't ask for.
- Always consider adding a catch-all/inbox space (is_inbox=true) for misc thoughts, unless the user's setup already covers everything.

Use the generate_notely_config tool to return your result."""

    response = client.messages.create(
        model="claude-sonnet-4-5-20250929",
        max_tokens=2048,
        system=system,
        tools=[ONBOARDING_TOOL],
        tool_choice={"type": "tool", "name": "generate_notely_config"},
        messages=[{
            "role": "user",
            "content": f"Here's what I told you about how I work:\n\n{answers_text}\n\nSet up notely for me.",
        }],
    )

    for block in response.content:
        if block.type == "tool_use" and block.name == "generate_notely_config":
            return block.input

    raise ValueError("AI did not return config via tool_use")


def _build_config(spaces: list[dict], starters: dict[str, list[str]], user_name: str | None = None) -> str:
    """Generate config.toml from AI output."""
    lines = ["# Notely configuration", "# Generated during setup — edit anytime", ""]

    if user_name:
        lines.append(f'user_name = "{user_name}"')
        lines.append("")

    for sp in spaces:
        slug = sp["slug"]
        lines.append(f"[spaces.{slug}]")
        lines.append(f'display_name = "{sp["display_name"]}"')
        lines.append(f'description = "{sp["description"]}"')
        lines.append(f'group_by = "{sp["group_by"]}"')

        if sp.get("subgroup_by"):
            lines.append(f'subgroup_by = "{sp["subgroup_by"]}"')

        fields = sp.get("fields", [])
        inner = ", ".join(f'"{f}"' for f in fields)
        lines.append(f"fields = [{inner}]")
        lines.append("")

    # Starter folders as comments
    for space_slug, names in starters.items():
        if names:
            lines.append(f"# Starter folders for {space_slug}:")
            for name in names:
                lines.append(f"#   [{space_slug}] {name}")
            lines.append("")

    return "\n".join(lines) + "\n"


def _show_summary(
    spaces: list[dict],
    starters: dict[str, list[str]],
    target_dir: "Path | None" = None,
) -> None:
    """Show what the AI set up, including the folder tree."""
    from pathlib import Path

    from slugify import slugify

    lines = ["[bold]Here's what we'll create:[/bold]", ""]

    # Show spaces overview
    for sp in spaces:
        slug = sp["slug"]
        label = f"  [green]{sp['display_name']}[/green] [dim]({slug}/)[/dim]"
        lines.append(label)
        lines.append(f"    {sp['description']}")

        if sp.get("is_inbox"):
            pass
        elif sp["group_by"] != "date":
            org = f"    Organized by {sp['group_by']}"
            if sp.get("subgroup_by"):
                org += f", then by {sp['subgroup_by']}"
            lines.append(org)
        else:
            lines.append("    Flat — notes sorted by date")

        fields = sp.get("fields", [])
        if "action_items" in fields:
            lines.append("    Tracks action items")
        if "content_status" in fields:
            lines.append("    Tracks idea status (seed / draft / used)")

        lines.append("")

    # Show folder tree
    root = target_dir or Path(".")
    lines.append("[bold]Folders:[/bold]")
    lines.append(f"  {root}/")
    lines.append(f"  {' ' * len(str(root))} config.toml")
    lines.append(f"  {' ' * len(str(root))} index.db")
    lines.append(f"  {' ' * len(str(root))} notes/")

    for sp in spaces:
        slug = sp["slug"]
        lines.append(f"  {' ' * len(str(root))}   {slug}/")
        starter_names = starters.get(slug, [])
        for name in starter_names:
            folder_slug = slugify(name)
            lines.append(f"  {' ' * len(str(root))}     {folder_slug}/  [dim]({name})[/dim]")

    lines.append("")

    console.print(Panel("\n".join(lines), border_style="blue", title="Setup Summary"))


def _edit_config(
    spaces: list[dict],
    starters: dict[str, list[str]],
    target_dir: "Path | None" = None,
    user_name: str | None = None,
) -> str:
    """Let user describe what to change, send back to AI."""
    console.print()
    console.print("[bold]What would you like to change?[/bold]")
    console.print("[dim]e.g., \"rename Ideas to Content\", \"add a space for research\", \"remove the inbox\"[/dim]")
    console.print("[dim]Enter for new lines, Enter twice to submit.[/dim]")
    console.print()
    edit_request = _ask(multiline=True)

    console.print("[dim]Updating...[/dim]")

    try:
        client = anthropic.Anthropic()

        current_config = json.dumps({"spaces": spaces, "starter_folders": starters})

        response = client.messages.create(
            model="claude-sonnet-4-5-20250929",
            max_tokens=2048,
            system="""You are helping edit a Notely config. The user has an existing config and wants to change it. Apply their requested changes and return the updated config using the generate_notely_config tool.

Keep everything the user didn't mention unchanged. Only modify what they asked for.""",
            tools=[ONBOARDING_TOOL],
            tool_choice={"type": "tool", "name": "generate_notely_config"},
            messages=[{
                "role": "user",
                "content": f"Current config:\n{current_config}\n\nChange requested: {edit_request}",
            }],
        )

        for block in response.content:
            if block.type == "tool_use" and block.name == "generate_notely_config":
                result = block.input
                new_spaces = result.get("spaces", spaces)
                new_starters = result.get("starter_folders", starters)
                _show_summary(new_spaces, new_starters, target_dir)

                console.print()
                choice = _ask("[Y] Looks good / [e] Edit more / [n] Keep original: ")
                if choice.strip().lower() in ("y", ""):
                    return _build_config(new_spaces, new_starters, user_name=user_name)
                elif choice.strip().lower() == "e":
                    return _edit_config(new_spaces, new_starters, target_dir, user_name=user_name)
                else:
                    return _build_config(spaces, starters, user_name=user_name)

    except Exception as e:
        console.print(f"[yellow]Edit failed ({e}). Keeping original config.[/yellow]")

    return _build_config(spaces, starters, user_name=user_name)
