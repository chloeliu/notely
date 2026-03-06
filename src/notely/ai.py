"""Claude API integration for note structuring with tool_use."""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import anthropic

from .templates import load_template, CLASSIFIER, FORMATTER, MERGER

logger = logging.getLogger(__name__)

# --- Secret masking ---

SECRET_PATTERN = re.compile(r"\|\|\|(.+?)\|\|\|", re.DOTALL)


def mask_secrets(text: str) -> tuple[str, dict[str, str]]:
    """Replace |||secret||| with [REDACTED_N]. Returns (masked_text, mapping)."""
    mapping: dict[str, str] = {}
    counter = 0

    def replacer(m: re.Match) -> str:
        nonlocal counter
        counter += 1
        key = f"[REDACTED_{counter}]"
        mapping[key] = m.group(1)
        return key

    masked = SECRET_PATTERN.sub(replacer, text)
    return masked, mapping


def unmask_secrets(text: str, mapping: dict[str, str]) -> str:
    """Replace [REDACTED_N] placeholders back with real values."""
    for key, value in mapping.items():
        text = text.replace(key, value)
    return text

import base64
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# Max image file size for Vision API (20MB)
_IMAGE_MAX_BYTES = 20 * 1024 * 1024

# SVG is text-based, not raster — skip vision
_VISION_SKIP_EXTENSIONS = {".svg"}

# Media types for the Vision API
_IMAGE_MEDIA_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
}


def describe_image(file_path: Path) -> str | None:
    """Describe an image using the Claude Vision API.

    Reads the image, base64-encodes it, and sends it to Claude with a
    note-taking-focused prompt. Returns a text description suitable for
    structuring into a note, or None on any failure.

    Args:
        file_path: path to the image file (must exist)

    Returns:
        Description text, or None if:
        - No ANTHROPIC_API_KEY set
        - File is SVG (text-based, not raster)
        - File exceeds 20MB (API limit)
        - Network/API error
    """
    suffix = file_path.suffix.lower()

    # SVG is text, not raster — skip vision
    if suffix in _VISION_SKIP_EXTENSIONS:
        return None

    media_type = _IMAGE_MEDIA_TYPES.get(suffix)
    if not media_type:
        return None

    # Check file size
    try:
        size = file_path.stat().st_size
        if size > _IMAGE_MAX_BYTES:
            logger.debug("Image too large for Vision API: %d bytes", size)
            return None
    except OSError:
        return None

    # Read and encode
    try:
        image_data = file_path.read_bytes()
        b64_data = base64.standard_b64encode(image_data).decode("ascii")
    except Exception:
        return None

    # Call Vision API
    try:
        client = anthropic.Anthropic()
        response = client.messages.create(
            model="claude-sonnet-4-5-20250929",
            max_tokens=1024,
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": media_type,
                            "data": b64_data,
                        },
                    },
                    {
                        "type": "text",
                        "text": (
                            "Describe this image for a note-taking system. "
                            "Focus on: what the image shows, any text/data visible, "
                            "key details worth capturing. Be concise but thorough. "
                            "If it's a screenshot, diagram, or chart, extract the "
                            "relevant information. If it's a photo, describe the "
                            "scene and any notable elements."
                        ),
                    },
                ],
            }],
        )
        if response.content and response.content[0].type == "text":
            return response.content[0].text
    except Exception as e:
        logger.debug("Vision API call failed: %s", e)
        return None

    return None


from .config import NotelyConfig
from .db import Database
from .models import (
    AIMetadata,
    AIStructuredOutput,
    ActionItem,
    InputSize,
    NoteRouting,
    SpaceTaxonomy,
    TaxonomyGroup,
    TaxonomySubgroup,
)

# Tool for quick list items (todos, ideas) — no full note needed
ADD_LIST_ITEM_TOOL = {
    "name": "add_list_item",
    "description": "Add a quick item to a list (todo or idea) without creating a full note. Use this when the input is a short actionable task or a brief idea — not rich prose that needs structuring.",
    "input_schema": {
        "type": "object",
        "required": ["item_type", "items"],
        "properties": {
            "item_type": {
                "type": "string",
                "enum": ["todo", "idea"],
                "description": "Whether this is a todo (action item) or an idea (content seed)",
            },
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["text"],
                    "properties": {
                        "text": {
                            "type": "string",
                            "description": "The task description or idea title",
                        },
                        "owner": {
                            "type": ["string", "null"],
                            "description": "Who owns this task (for todos). Use 'me' if the user is assigning to themselves.",
                        },
                        "due": {
                            "type": ["string", "null"],
                            "description": "Due date in YYYY-MM-DD format (for todos)",
                        },
                        "space": {
                            "type": ["string", "null"],
                            "description": "Which space this relates to, if clear from context",
                        },
                        "group": {
                            "type": ["string", "null"],
                            "description": "Which group (client, category) this relates to",
                        },
                        "tags": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Relevant tags (for ideas)",
                        },
                        "summary": {
                            "type": ["string", "null"],
                            "description": "One-line summary or expansion of the idea (for ideas)",
                        },
                    },
                },
                "description": "One or more items to add",
            },
        },
    },
}

# Tool for quick reference snippets (identifiers, bookmarks, facts)
ADD_SNIPPET_TOOL = {
    "name": "add_snippet",
    "description": "Save quick reference items (identifiers, bookmarks, or facts). Use when the input is a concrete identifier, URL/bookmark, or quick fact — not prose needing structuring.",
    "input_schema": {
        "type": "object",
        "required": ["items"],
        "properties": {
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["entity", "key", "value", "snippet_type"],
                    "properties": {
                        "entity": {
                            "type": "string",
                            "description": "Entity name: 'labcorp', 'plaid-api', 'dr-smith'",
                        },
                        "key": {
                            "type": "string",
                            "description": "Label: 'npi', 'docs_url', 'ehr_platform', 'api_key_location'",
                        },
                        "value": {
                            "type": "string",
                            "description": "The data itself",
                        },
                        "description": {
                            "type": "string",
                            "description": "Optional context about this reference",
                        },
                        "snippet_type": {
                            "type": "string",
                            "enum": ["identifier", "bookmark", "fact"],
                            "description": "Type: identifier (NPI, account #), bookmark (URL), fact (quick knowledge)",
                        },
                        "tags": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Optional tags",
                        },
                    },
                },
                "description": "One or more reference items to save",
            },
        },
    },
}

# Tool definition for structured output
STRUCTURE_NOTE_TOOL = {
    "name": "save_structured_note",
    "description": "Save a structured note with routing, metadata, and body content.",
    "input_schema": {
        "type": "object",
        "required": ["routing", "metadata", "body_markdown"],
        "properties": {
            "routing": {
                "type": "object",
                "description": "Where to file this note",
                "required": ["space", "group_slug", "group_display", "group_is_new"],
                "properties": {
                    "space": {
                        "type": "string",
                        "description": "Which space this note belongs to",
                    },
                    "group_slug": {
                        "type": "string",
                        "description": "Slug for the primary group (client slug or category slug)",
                    },
                    "group_display": {
                        "type": "string",
                        "description": "Display name for the group",
                    },
                    "group_is_new": {
                        "type": "boolean",
                        "description": "Whether this is a new group not yet in the taxonomy",
                    },
                    "subgroup_slug": {
                        "type": ["string", "null"],
                        "description": "Slug for the subgroup (e.g., topic). Null if no subgroup.",
                    },
                    "subgroup_display": {
                        "type": ["string", "null"],
                        "description": "Display name for the subgroup",
                    },
                    "subgroup_is_new": {
                        "type": "boolean",
                        "description": "Whether this is a new subgroup",
                    },
                    "subgroup_description": {
                        "type": ["string", "null"],
                        "description": "Brief description of the subgroup if new",
                    },
                    "append_to_note": {
                        "type": ["string", "null"],
                        "description": "If the content should be appended to an existing note, the note ID. Null for new note.",
                    },
                },
            },
            "metadata": {
                "type": "object",
                "description": "Structured metadata extracted from the raw text",
                "required": ["title", "source", "date", "summary"],
                "properties": {
                    "title": {
                        "type": "string",
                        "description": "Clear, descriptive title for the note",
                    },
                    "source": {
                        "type": "string",
                        "description": "Source type: meeting, slack, email, podcast, article, thought, conversation, manual",
                    },
                    "date": {
                        "type": "string",
                        "description": "Date of the event/input in YYYY-MM-DD format",
                    },
                    "participants": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "People mentioned or involved",
                    },
                    "tags": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Relevant tags as lowercase slugs",
                    },
                    "summary": {
                        "type": "string",
                        "description": "One to two sentence summary of the key takeaway",
                    },
                    "action_items": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "required": ["owner", "task"],
                            "properties": {
                                "owner": {"type": "string"},
                                "task": {"type": "string"},
                                "due": {"type": ["string", "null"]},
                            },
                        },
                        "description": "Action items with owner and optional due date",
                    },
                    "extra": {
                        "type": "object",
                        "description": "Space-specific extra fields: for ideas space, include content_status (seed/draft/used), source_ref, source_url if known. Never hallucinate URLs.",
                    },
                },
            },
            "body_markdown": {
                "type": "string",
                "description": "The organized note body in markdown. Use ## headings for sections like Key Points, Discussion Details, Action Items, Core Idea, Supporting Points, Potential Angles as appropriate.",
            },
            "related_contexts": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Cross-references to other spaces/groups this note relates to, as paths like 'clients/acme-corp/api-project' or 'ideas/thought-leadership'",
            },
        },
    },
}


def build_taxonomy_context(config: NotelyConfig, db: Database) -> dict[str, Any]:
    """Build a dict representing the full workspace structure for AI context.

    Walks all configured spaces, their groups, and subgroups, pulling note counts,
    recent note titles, and last-activity dates from SQLite. The result is passed
    to the AI system prompt so it can make routing decisions (match existing groups,
    detect appends to today's notes, avoid creating duplicates).

    Args:
        config: NotelyConfig with space definitions.
        db: Database instance for querying groups, subgroups, and recent notes.

    Returns:
        Dict with structure: {"spaces": {<space_name>: {"description", "group_by",
        "subgroup_by", "fields", "groups": [{"slug", "display", "note_count",
        "last_note", "recent_notes": [...], "subgroups": [...]}]}}}
    """
    taxonomy: dict[str, Any] = {"spaces": {}}

    for space_name, space_cfg in config.spaces.items():
        space_data: dict[str, Any] = {
            "description": space_cfg.description,
            "group_by": space_cfg.group_by,
            "subgroup_by": space_cfg.subgroup_by,
            "fields": space_cfg.fields,
            "groups": [],
        }

        groups = db.get_groups(space_name, space_cfg.group_by)
        for g in groups:
            group_entry: dict[str, Any] = {
                "slug": g["grp"],
                "display": g["grp_display"] or g["grp"],
                "note_count": g["note_count"],
                "last_note": g["last_note"],
            }

            # Include recent note titles so the AI can see what's already filed here
            recent = db.get_recent_notes_in_group(
                space_name, space_cfg.group_by, g["grp"], limit=5
            )
            if recent:
                group_entry["recent_notes"] = [
                    {"id": n["id"], "title": n["title"], "date": n["date"]}
                    for n in recent
                ]

            if space_cfg.subgroup_by:
                subgroups = db.get_subgroups(
                    space_name, space_cfg.group_by, g["grp"], space_cfg.subgroup_by
                )
                group_entry["subgroups"] = [
                    {
                        "slug": sg["subgrp"],
                        "display": sg["subgrp_display"] or sg["subgrp"],
                        "note_count": sg["note_count"],
                        "last_note": sg["last_note"],
                    }
                    for sg in subgroups
                ]

            space_data["groups"].append(group_entry)

        taxonomy["spaces"][space_name] = space_data

    return taxonomy


def _build_system_prompt(
    taxonomy: dict[str, Any],
    todays_notes: list[dict[str, Any]],
    input_size: InputSize,
    user_name: str | None = None,
    workspace_path: Path | str | None = None,
) -> str:
    """Build the system prompt with taxonomy context.

    Loads the classifier template from workspace templates/ if available,
    otherwise uses the built-in default.
    """
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    size_guidance = {
        InputSize.SMALL: (
            "This is a SHORT input (<500 chars). The user typed a quick thought or idea. "
            "Expand the thought: add context, supporting points, and suggested angles. "
            "Structure it into a proper note but keep the original voice."
        ),
        InputSize.MEDIUM: (
            "This is a MEDIUM input (500-10K chars). Likely a Slack thread or short meeting notes. "
            "Extract key points, organize by topic, identify action items, generate a clear summary."
        ),
        InputSize.LARGE: (
            "This is a LARGE input (10K+ chars). Likely a full meeting transcript or long document. "
            "Summarize heavily: pull out key decisions, action items, and main discussion points. "
            "Create a well-organized note that captures the essence."
        ),
    }

    todays_notes_str = ""
    if todays_notes:
        todays_notes_str = "\n\nNotes already created today (append to these if new input relates to the same topic/meeting):\n"
        for n in todays_notes:
            sm = json.loads(n.get("space_metadata", "{}"))
            todays_notes_str += f"  - [{n['id']}] {n['title']} (space={n['space']}"
            if "client" in sm:
                todays_notes_str += f", client={sm['client']}"
            if "topic" in sm:
                todays_notes_str += f", topic={sm['topic']}"
            if "category" in sm:
                todays_notes_str += f", category={sm['category']}"
            todays_notes_str += ")\n"

    user_str = ""
    if user_name:
        user_str = (
            f'\nThe user\'s name is **{user_name}**. When the user refers to '
            f'themselves ("I need to", "remind me"), use "{user_name}" as the owner. '
            f'For action items owned by others, use their first name as owner — '
            f'{user_name} only needs to manage their own items.\n'
        )

    template = load_template(workspace_path, CLASSIFIER)
    return template.format(
        today=today,
        user_str=user_str,
        taxonomy=json.dumps(taxonomy, indent=2),
        todays_notes_str=todays_notes_str,
        size_guidance=size_guidance.get(input_size, size_guidance[InputSize.MEDIUM]),
    )


## --- New structuring-only tools (no routing) ---

STRUCTURE_ONLY_TOOL = {
    "name": "structure_note",
    "description": "Structure raw text into an organized note with metadata and body content. Routing has already been decided — just structure the content.",
    "input_schema": {
        "type": "object",
        "required": ["metadata", "body_markdown"],
        "properties": {
            "metadata": {
                "type": "object",
                "description": "Structured metadata extracted from the raw text",
                "required": ["title", "source", "date", "summary"],
                "properties": {
                    "title": {
                        "type": "string",
                        "description": "Clear, descriptive title for the note",
                    },
                    "source": {
                        "type": "string",
                        "description": "Source type: meeting, slack, email, podcast, article, thought, conversation, manual",
                    },
                    "date": {
                        "type": "string",
                        "description": "Date of the event/input in YYYY-MM-DD format",
                    },
                    "participants": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "People mentioned or involved",
                    },
                    "tags": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Relevant tags as lowercase slugs",
                    },
                    "summary": {
                        "type": "string",
                        "description": "One to two sentence summary of the key takeaway",
                    },
                    "action_items": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "required": ["owner", "task"],
                            "properties": {
                                "owner": {"type": "string"},
                                "task": {"type": "string"},
                                "due": {"type": ["string", "null"]},
                            },
                        },
                        "description": "Action items with owner and optional due date",
                    },
                    "extra": {
                        "type": "object",
                        "description": "Space-specific extra fields: for ideas space, include content_status (seed/draft/used), source_ref, source_url if known. Never hallucinate URLs.",
                    },
                },
            },
            "body_markdown": {
                "type": "string",
                "description": "The organized note body in markdown. Use ## headings for sections.",
            },
            "related_contexts": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Cross-references to other spaces/groups this note relates to",
            },
        },
    },
}

MERGE_NOTE_TOOL = {
    "name": "merge_note",
    "description": "Merge new content into an existing note, producing an updated body and metadata.",
    "input_schema": {
        "type": "object",
        "required": ["updated_body", "updated_summary"],
        "properties": {
            "updated_body": {
                "type": "string",
                "description": "The complete updated note body in markdown, integrating both old and new content.",
            },
            "updated_summary": {
                "type": "string",
                "description": "Updated 1-2 sentence summary covering both old and new content.",
            },
            "new_tags": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Additional tags to add (will be merged with existing)",
            },
            "new_action_items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["owner", "task"],
                    "properties": {
                        "owner": {"type": "string"},
                        "task": {"type": "string"},
                        "due": {"type": ["string", "null"]},
                    },
                },
                "description": "New action items extracted from the new content",
            },
            "new_participants": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Additional participants to add",
            },
        },
    },
}


def _build_existing_note_str(existing_note: Any) -> str:
    """Build the existing note context string for merge prompts."""
    existing_sections = []

    existing_sections.append(f"**Title:** {existing_note.title}")
    existing_sections.append(f"**Date:** {existing_note.date}")
    existing_sections.append(f"**Summary:** {existing_note.summary or '(none)'}")

    if existing_note.tags:
        existing_sections.append(f"**Tags (already assigned):** {', '.join(existing_note.tags)}")

    if existing_note.participants:
        existing_sections.append(f"**Participants (already listed):** {', '.join(existing_note.participants)}")

    if existing_note.action_items:
        action_lines = []
        for item in existing_note.action_items:
            due_str = f" (due {item.due})" if item.due else ""
            status = item.status.value if hasattr(item.status, "value") else item.status
            action_lines.append(f"  - [{status}] {item.owner}: {item.task}{due_str}")
        existing_sections.append("**Action items (already tracked):**\n" + "\n".join(action_lines))

    existing_sections.append(f"\n**Body:**\n{existing_note.body or '(empty)'}")

    return "\n".join(existing_sections)


def _build_structuring_prompt(
    space_config: dict[str, Any],
    input_size: InputSize,
    user_name: str | None = None,
    mode: str = "new",
    existing_note: Any | None = None,
    workspace_path: Path | str | None = None,
) -> str:
    """Build a focused system prompt for structuring only (no routing).

    Loads formatter or merger template from workspace templates/ if available,
    otherwise uses the built-in default.

    For merge mode, existing_note should be the full Note object so the AI
    sees everything: body, summary, action items, tags, participants.
    """
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    size_guidance = {
        InputSize.SMALL: (
            "This is a SHORT input (<500 chars). Expand the thought: add context, "
            "supporting points, and suggested angles. Keep the original voice."
        ),
        InputSize.MEDIUM: (
            "This is a MEDIUM input (500-10K chars). Extract key points, organize by "
            "topic, identify action items, generate a clear summary."
        ),
        InputSize.LARGE: (
            "This is a LARGE input (10K+ chars). Summarize heavily: pull out key "
            "decisions, action items, and main discussion points."
        ),
    }

    user_str = ""
    if user_name:
        user_str = (
            f'\nThe user\'s name is **{user_name}**. When the user refers to '
            f'themselves ("I need to", "remind me"), use "{user_name}" as the owner. '
            f'For action items owned by others, use their first name as owner — '
            f'{user_name} only needs to manage their own items.\n'
        )

    space_info = ""
    if space_config:
        space_info = f"\n## Target Space Configuration\n\n{json.dumps(space_config, indent=2)}\n"

    if mode == "merge" and existing_note is not None:
        existing_note_str = _build_existing_note_str(existing_note)
        template = load_template(workspace_path, MERGER)
        return template.format(
            today=today,
            user_str=user_str,
            space_info=space_info,
            existing_note_str=existing_note_str,
        )

    template = load_template(workspace_path, FORMATTER)
    return template.format(
        today=today,
        user_str=user_str,
        space_info=space_info,
        size_guidance=size_guidance.get(input_size, size_guidance[InputSize.MEDIUM]),
    )


def structure_only(
    raw_text: str,
    space_config: dict[str, Any],
    input_size: InputSize,
    secret_mapping: dict[str, str] | None = None,
    user_name: str | None = None,
    user_instruction: str | None = None,
    workspace_path: Path | str | None = None,
) -> AIStructuredOutput:
    """Send raw text to the Claude API for structuring into a note (no routing).

    Routing has already been decided by the routing pipeline. This function
    only handles content structuring: title, summary, tags, action items, body.
    Uses tool_use with the structure_note tool to get structured JSON output.

    The AI may choose add_list_item instead of structure_note if the input looks
    like a quick task or idea rather than prose. In that case, a ListItemResult
    exception is raised for the caller to handle.

    Args:
        raw_text: The text to structure (should already be secret-masked if needed).
        space_config: Dict describing the target space (description, fields, etc.).
        input_size: InputSize enum (SMALL/MEDIUM/LARGE) to guide AI verbosity.
        secret_mapping: If provided, maps [REDACTED_N] placeholders back to real
            values. Not used here directly -- stored for callers to unmask later.
        user_name: If set, the AI uses this name instead of "me" for action item owners.
        user_instruction: If provided, the user's typed context describing what
            to focus on or how to structure the note. Sent as a separate directive
            so the AI treats it as guidance, not content.
        workspace_path: Path to the notely workspace for loading user templates.

    Returns:
        AIStructuredOutput with metadata (title, summary, tags, action_items, etc.)
        and body_markdown. The routing field contains a dummy placeholder since
        routing is handled externally.

    Raises:
        ListItemResult: If the AI chose add_list_item instead of structure_note.
        ValueError: If the AI response contains no tool_use block.
    """
    client = anthropic.Anthropic()

    system = _build_structuring_prompt(
        space_config, input_size, user_name=user_name,
        workspace_path=workspace_path,
    )

    if user_instruction:
        user_msg = (
            f"USER INSTRUCTION: {user_instruction}\n\n"
            f"---\n\n"
            f"{raw_text}"
        )
    else:
        user_msg = (
            f"Structure this input into a note.\n\n{raw_text}"
        )

    response = client.messages.create(
        model="claude-sonnet-4-5-20250929",
        max_tokens=4096,
        system=system,
        tools=[STRUCTURE_ONLY_TOOL, ADD_LIST_ITEM_TOOL, ADD_SNIPPET_TOOL],
        tool_choice={"type": "any"},
        messages=[{"role": "user", "content": user_msg}],
    )

    for block in response.content:
        if block.type == "tool_use":
            if block.name == "structure_note":
                return _parse_structure_only_output(block.input)
            elif block.name == "add_list_item":
                raise ListItemResult(block.input)
            elif block.name == "add_snippet":
                raise SnippetResult(block.input)

    raise ValueError("AI did not return structured output via tool_use")


class ListItemResult(Exception):
    """Exception to signal AI chose list_item instead of note.

    Caught across module boundaries (ai.py → open_cmd.py, dump.py).
    """

    def __init__(self, data: dict[str, Any]) -> None:
        self.data = {"type": "list_item", **data}
        super().__init__("AI chose list_item")


class SnippetResult(Exception):
    """Exception to signal AI chose add_snippet instead of note.

    Caught across module boundaries (ai.py → open_cmd.py, dump.py).
    """

    def __init__(self, data: dict[str, Any]) -> None:
        self.data = {"type": "snippet", **data}
        super().__init__("AI chose snippet")




def merge_with_existing(
    raw_text: str,
    existing_note: Any,
    space_config: dict[str, Any],
    input_size: InputSize,
    user_name: str | None = None,
    workspace_path: Path | str | None = None,
) -> dict[str, Any]:
    """Send raw text + the full existing Note to Claude for intelligent merging.

    The AI receives the complete current state of the note (title, summary,
    body, tags, participants, action items with statuses) so it can identify
    what is genuinely new vs already captured. The merge prompt instructs the
    AI to only update what has new information -- not rewrite for the sake of it.

    Args:
        raw_text: New content to merge in (should already be secret-masked if needed).
        existing_note: The full Note object (from models.py). Passed to the system
            prompt so the AI sees body, summary, tags, participants, and action items
            labeled as "already tracked" / "already assigned" to prevent duplicates.
        space_config: Dict describing the target space configuration.
        input_size: InputSize enum (SMALL/MEDIUM/LARGE) to guide AI verbosity.
        user_name: If set, the AI uses this name for action item owners.
        workspace_path: Path to the notely workspace for loading user templates.

    Returns:
        Dict with keys:
            updated_body (str): Complete merged note body in markdown.
            updated_summary (str): Updated 1-2 sentence summary.
            new_tags (list[str]): Only tags not already on the note.
            new_action_items (list[ActionItem]): Only genuinely new action items.
            new_participants (list[str]): Only participants not already listed.

    Raises:
        ValueError: If the AI response contains no merge_note tool_use block.
    """
    client = anthropic.Anthropic()

    system = _build_structuring_prompt(
        space_config, input_size, user_name=user_name,
        mode="merge",
        existing_note=existing_note,
        workspace_path=workspace_path,
    )

    response = client.messages.create(
        model="claude-sonnet-4-5-20250929",
        max_tokens=4096,
        system=system,
        tools=[MERGE_NOTE_TOOL],
        tool_choice={"type": "tool", "name": "merge_note"},
        messages=[{"role": "user", "content": f"Merge this new content into the existing note:\n\n{raw_text}"}],
    )

    for block in response.content:
        if block.type == "tool_use" and block.name == "merge_note":
            data = block.input
            # Parse action items
            action_items = []
            for item in data.get("new_action_items", []):
                action_items.append(ActionItem(
                    owner=item["owner"],
                    task=item["task"],
                    due=item.get("due"),
                ))
            return {
                "updated_body": data["updated_body"],
                "updated_summary": data["updated_summary"],
                "new_tags": data.get("new_tags", []),
                "new_action_items": action_items,
                "new_participants": data.get("new_participants", []),
            }

    raise ValueError("AI did not return merge output via tool_use")


def revise_note(
    note: Any,
    instruction: str,
    space_config: dict[str, Any],
    input_size: InputSize,
    user_name: str | None = None,
) -> AIStructuredOutput:
    """Re-structure an existing note based on user revision instructions.

    Sends the full current note state plus the user's instruction to the AI,
    which returns a fully revised note (same format as structure_only).

    Args:
        note: The full Note object to revise.
        instruction: User's description of what to change.
        space_config: Dict describing the target space.
        input_size: InputSize enum to guide AI verbosity.
        user_name: If set, the AI uses this name for action item owners.

    Returns:
        AIStructuredOutput with revised metadata and body_markdown.

    Raises:
        ValueError: If the AI response contains no tool_use block.
    """
    client = anthropic.Anthropic()

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    user_str = ""
    if user_name:
        user_str = (
            f'\nThe user\'s name is **{user_name}**. When the user refers to '
            f'themselves ("I need to", "remind me"), use "{user_name}" as the owner. '
            f'For action items owned by others, use their first name as owner — '
            f'{user_name} only needs to manage their own items.\n'
        )

    space_info = ""
    if space_config:
        space_info = f"\n## Target Space Configuration\n\n{json.dumps(space_config, indent=2)}\n"

    # Build current note context
    note_sections = []
    note_sections.append(f"**Title:** {note.title}")
    note_sections.append(f"**Date:** {note.date}")
    note_sections.append(f"**Source:** {note.source}")
    note_sections.append(f"**Summary:** {note.summary or '(none)'}")
    if note.tags:
        note_sections.append(f"**Tags:** {', '.join(note.tags)}")
    if note.participants:
        note_sections.append(f"**Participants:** {', '.join(note.participants)}")
    if note.action_items:
        action_lines = []
        for item in note.action_items:
            due_str = f" (due {item.due})" if item.due else ""
            status = item.status.value if hasattr(item.status, "value") else item.status
            action_lines.append(f"  - [{status}] {item.owner}: {item.task}{due_str}")
        note_sections.append("**Action items:**\n" + "\n".join(action_lines))
    note_sections.append(f"\n**Body:**\n{note.body or '(empty)'}")
    note_str = "\n".join(note_sections)

    system = f"""You are a note structuring assistant. Your job is to REVISE an existing note based on the user's instructions.

Today's date: {today}
{user_str}
{space_info}
## Current Note

{note_str}

## Instructions

The user wants specific changes to this note. Apply their requested changes and return the fully revised note using the structure_note tool. Keep everything the user didn't ask to change. Only modify what they asked for.

## Sensitive Data

Preserve [REDACTED_N] placeholders exactly as-is."""

    response = client.messages.create(
        model="claude-sonnet-4-5-20250929",
        max_tokens=4096,
        system=system,
        tools=[STRUCTURE_ONLY_TOOL],
        tool_choice={"type": "tool", "name": "structure_note"},
        messages=[{"role": "user", "content": f"Revise this note: {instruction}"}],
    )

    for block in response.content:
        if block.type == "tool_use" and block.name == "structure_note":
            return _parse_structure_only_output(block.input)

    raise ValueError("AI did not return revised output via tool_use")


def revise_list_items(
    items: list[dict],
    item_type: str,
    instruction: str,
) -> list[dict]:
    """Revise a batch of list items (todos/ideas) or snippets based on user instruction.

    Sends the current items plus the user's instruction to the AI, which
    returns a revised items array using the same tool schema.

    Args:
        items: Current list of item dicts.
        item_type: "todo", "idea", or "snippet".
        instruction: User's description of what to change.

    Returns:
        Revised items list, or the original items if AI fails.
    """
    import yaml

    client = anthropic.Anthropic()

    items_yaml = yaml.dump(items, default_flow_style=False, allow_unicode=True)

    if item_type == "snippet":
        tool = ADD_SNIPPET_TOOL
        tool_name = "add_snippet"
    else:
        tool = ADD_LIST_ITEM_TOOL
        tool_name = "add_list_item"

    system = f"""You are a note assistant. The user has a batch of {item_type} items they want revised.

Current items:
```yaml
{items_yaml}
```

Apply the user's requested changes and return the revised items using the {tool_name} tool.
Keep items the user didn't ask to change. You may add, remove, or modify items as requested."""

    try:
        response = client.messages.create(
            model="claude-sonnet-4-5-20250929",
            max_tokens=2048,
            system=system,
            tools=[tool],
            tool_choice={"type": "tool", "name": tool_name},
            messages=[{"role": "user", "content": instruction}],
        )

        for block in response.content:
            if block.type == "tool_use" and block.name == tool_name:
                data = block.input
                if item_type == "snippet":
                    return data.get("items", items)
                else:
                    return data.get("items", items)
    except Exception as e:
        logger.warning("AI revision failed: %s", e)

    return items


def _parse_structure_only_output(data: dict[str, Any]) -> AIStructuredOutput:
    """Parse the structure_note tool output into AIStructuredOutput.

    Uses a dummy routing since routing is handled externally.
    """
    meta_data = data["metadata"]

    action_items = []
    for item in meta_data.get("action_items", []):
        action_items.append(ActionItem(
            owner=item["owner"],
            task=item["task"],
            due=item.get("due"),
        ))

    # Dummy routing — will be replaced by the caller
    routing = NoteRouting(
        space="",
        group_slug="",
        group_display="",
    )

    metadata = AIMetadata(
        title=meta_data["title"],
        source=meta_data.get("source", "manual"),
        date=meta_data["date"],
        participants=meta_data.get("participants", []),
        tags=meta_data.get("tags", []),
        summary=meta_data["summary"],
        action_items=action_items,
        extra=meta_data.get("extra", {}),
    )

    return AIStructuredOutput(
        routing=routing,
        metadata=metadata,
        body_markdown=data["body_markdown"],
        related_contexts=data.get("related_contexts", []),
    )


def structure_input(
    raw_text: str,
    taxonomy: dict[str, Any],
    todays_notes: list[dict[str, Any]],
    hints: dict[str, str],
    input_size: InputSize,
    secret_mapping: dict[str, str] | None = None,
    user_name: str | None = None,
    workspace_path: Path | str | None = None,
) -> AIStructuredOutput | dict[str, Any]:
    """Call Claude API to process raw text.

    If secret_mapping is provided, the raw_text should already be masked.
    The mapping is stored on the result so callers can unmask later.

    Returns either:
    - AIStructuredOutput for full notes (save_structured_note)
    - dict with {"type": "list_item", ...} for quick items (add_list_item)
    """
    client = anthropic.Anthropic()

    system = _build_system_prompt(
        taxonomy, todays_notes, input_size, user_name=user_name,
        workspace_path=workspace_path,
    )

    user_message = f"Process this input — decide if it's a full note or a quick list item:\n\n{raw_text}"
    if hints:
        user_message += f"\n\nUser-provided hints: {json.dumps(hints)}"

    response = client.messages.create(
        model="claude-sonnet-4-5-20250929",
        max_tokens=4096,
        system=system,
        tools=[ADD_LIST_ITEM_TOOL, STRUCTURE_NOTE_TOOL],
        tool_choice={"type": "any"},
        messages=[{"role": "user", "content": user_message}],
    )

    for block in response.content:
        if block.type == "tool_use":
            if block.name == "save_structured_note":
                return _parse_ai_output(block.input)
            elif block.name == "add_list_item":
                return {"type": "list_item", **block.input}

    raise ValueError("AI did not return structured output via tool_use")


# Keep old name as alias for backwards compat
def structure_note(
    raw_text: str,
    taxonomy: dict[str, Any],
    todays_notes: list[dict[str, Any]],
    hints: dict[str, str],
    input_size: InputSize,
) -> AIStructuredOutput:
    """Call Claude API to structure raw text into a note (always creates a note)."""
    result = structure_input(raw_text, taxonomy, todays_notes, hints, input_size)
    if isinstance(result, AIStructuredOutput):
        return result
    # AI chose list_item but caller expected a note — shouldn't happen in normal flow
    raise ValueError("AI chose list_item when note was expected")


def _parse_ai_output(data: dict[str, Any]) -> AIStructuredOutput:
    """Parse the raw tool_use output into typed models."""
    routing_data = data["routing"]
    meta_data = data["metadata"]

    action_items = []
    for item in meta_data.get("action_items", []):
        action_items.append(ActionItem(
            owner=item["owner"],
            task=item["task"],
            due=item.get("due"),
        ))

    routing = NoteRouting(
        space=routing_data["space"],
        group_slug=routing_data["group_slug"],
        group_display=routing_data["group_display"],
        group_is_new=routing_data.get("group_is_new", False),
        subgroup_slug=routing_data.get("subgroup_slug"),
        subgroup_display=routing_data.get("subgroup_display"),
        subgroup_is_new=routing_data.get("subgroup_is_new", False),
        subgroup_description=routing_data.get("subgroup_description"),
        append_to_note=routing_data.get("append_to_note"),
    )

    metadata = AIMetadata(
        title=meta_data["title"],
        source=meta_data.get("source", "manual"),
        date=meta_data["date"],
        participants=meta_data.get("participants", []),
        tags=meta_data.get("tags", []),
        summary=meta_data["summary"],
        action_items=action_items,
        extra=meta_data.get("extra", {}),
    )

    return AIStructuredOutput(
        routing=routing,
        metadata=metadata,
        body_markdown=data["body_markdown"],
        related_contexts=data.get("related_contexts", []),
    )


# --- Chat mode: folder-scoped AI conversation ---

# Include full note bodies in system prompt if folder has fewer than this many notes
CHAT_SMALL_FOLDER_THRESHOLD = 20

CHAT_SEARCH_TOOL = {
    "name": "search_notes",
    "description": "Search notes in this folder by keyword. Use when the user asks about a specific topic, person, or event and you need to find relevant notes beyond the summaries provided.",
    "input_schema": {
        "type": "object",
        "required": ["query"],
        "properties": {
            "query": {
                "type": "string",
                "description": "Search query — keywords, person names, topics",
            },
        },
    },
}

CHAT_GET_NOTE_TOOL = {
    "name": "get_note_body",
    "description": "Get the full body text of a specific note. Use when you need details beyond the summary — action items, meeting specifics, full discussion content.",
    "input_schema": {
        "type": "object",
        "required": ["note_id"],
        "properties": {
            "note_id": {
                "type": "string",
                "description": "The note ID (8-character hex string shown in brackets like [abc12345])",
            },
        },
    },
}


def _build_chat_system_prompt(
    folder_context: dict[str, Any],
    folder_name: str,
    user_name: str | None = None,
    references: list[dict[str, Any]] | dict[str, dict[str, str]] | None = None,
) -> str:
    """Build the system prompt for folder-scoped chat.

    For small folders (<20 notes), includes full note bodies.
    For large folders, includes summaries only + tool instructions.
    """
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    notes = folder_context["notes"]
    open_todos = folder_context["open_todos"]
    subfolders = folder_context.get("subfolders", [])
    is_small = len(notes) < CHAT_SMALL_FOLDER_THRESHOLD

    parts = []

    # Header
    user_str = f" for {user_name}" if user_name else ""
    parts.append(
        f"You are an AI assistant{user_str} answering questions about the "
        f'"{folder_name}" folder. Today is {today}.'
    )

    # Overview
    parts.append("\n## Folder Overview")
    parts.append(f"- {len(notes)} note(s)")
    if open_todos:
        parts.append(f"- {len(open_todos)} open todo(s)")
    if subfolders:
        sub_list = ", ".join(
            s.get("display_name") or s.get("id", "")
            for s in subfolders
        )
        parts.append(f"- Subfolders: {sub_list}")

    # Open todos
    if open_todos:
        parts.append("\n## Open Todos")
        for t in open_todos:
            due = f" (due {t['due']})" if t.get("due") else ""
            from_note = t.get("note_title", "(standalone)")
            parts.append(f"- [{t['owner']}] {t['task']}{due} (from: {from_note})")

    # Notes
    parts.append("\n## Notes")
    if is_small:
        for n in notes:
            parts.append(f"\n### [{n['id']}] {n['title']} ({n['date']})")
            if n.get("tags"):
                parts.append(f"Tags: {', '.join(n['tags'])}")
            if n.get("participants"):
                parts.append(f"Participants: {', '.join(n['participants'])}")
            parts.append(f"Summary: {n['summary']}")
            if n.get("body"):
                parts.append(f"\n{n['body']}")
            if n.get("action_items"):
                parts.append("\nAction Items:")
                for a in n["action_items"]:
                    status = a.get("status", "open")
                    due = f" (due {a['due']})" if a.get("due") else ""
                    parts.append(f"- [{status}] {a['owner']}: {a['task']}{due}")
    else:
        for n in notes:
            tags_str = f" [{', '.join(n['tags'])}]" if n.get("tags") else ""
            parts.append(
                f"- [{n['id']}] **{n['title']}** ({n['date']}){tags_str}: {n['summary']}"
            )
        parts.append(
            "\n*Use search_notes to find relevant notes by keyword, "
            "and get_note_body to read full details.*"
        )

    # References (from DB — folder-scoped lookup data)
    if references:
        parts.append("\n## References")
        if isinstance(references, list):
            # DB format: list of dicts with entity/key/value/description/snippet_type
            by_entity: dict[str, list[dict]] = {}
            for r in references:
                by_entity.setdefault(r["entity"], []).append(r)
            for entity, refs in sorted(by_entity.items()):
                items_str = ", ".join(f"{r['key']}: {r['value']}" for r in refs)
                parts.append(f"- **{entity}**: {items_str}")
        else:
            # Legacy TOML format: dict of entity -> {key: value}
            for entity, kvs in sorted(references.items()):
                items_str = ", ".join(f"{k}: {v}" for k, v in kvs.items())
                parts.append(f"- **{entity}**: {items_str}")

    # Instructions
    parts.append("\n## Instructions")
    parts.append("- Answer questions using the notes above.")
    parts.append("- When citing information, reference the note title and date.")
    parts.append(
        "- If asked to produce a deliverable (summary, email draft, report, "
        "PRD), synthesize from the available notes."
    )
    parts.append("- Be concise but thorough. Use markdown formatting.")
    if not is_small:
        parts.append(
            "- Use the search_notes and get_note_body tools when you need "
            "details not in the summaries above."
        )

    return "\n".join(parts)


def chat_about_notes(
    user_message: str,
    conversation_history: list[dict[str, Any]],
    folder_context: dict[str, Any],
    folder_name: str,
    tool_handler: Any,
    user_name: str | None = None,
    references: list[dict[str, Any]] | dict[str, dict[str, str]] | None = None,
) -> tuple[str, list[dict[str, Any]]]:
    """Send a message in a folder-scoped chat session.

    Handles the tool-use loop: if the AI calls search_notes or get_note_body,
    the tool_handler executes the search/lookup, results are sent back, and
    the loop continues until the AI produces a final text response.

    Args:
        user_message: Current user question/request.
        conversation_history: Prior messages in Anthropic API format.
        folder_context: Dict from db.get_folder_context() with notes, todos, subfolders.
        folder_name: Display name of the folder for the system prompt.
        tool_handler: Callable(tool_name, tool_input) -> result dict/string.
        user_name: User's name for personalization.
        references: Matching references from references.toml for this folder.

    Returns:
        (response_text, updated_history) where updated_history includes all
        new messages from this turn.
    """
    client = anthropic.Anthropic()

    system = _build_chat_system_prompt(
        folder_context, folder_name, user_name, references=references,
    )

    # Use tools for large folders
    use_tools = len(folder_context["notes"]) >= CHAT_SMALL_FOLDER_THRESHOLD
    tools = [CHAT_SEARCH_TOOL, CHAT_GET_NOTE_TOOL] if use_tools else []

    # Build messages: history + current user message
    messages = list(conversation_history)
    messages.append({"role": "user", "content": user_message})

    # Tool-use loop (capped at 5 iterations for safety)
    for _ in range(5):
        kwargs: dict[str, Any] = {
            "model": "claude-sonnet-4-5-20250929",
            "max_tokens": 4096,
            "system": system,
            "messages": messages,
        }
        if tools:
            kwargs["tools"] = tools

        response = client.messages.create(**kwargs)

        if response.stop_reason == "tool_use":
            # Append assistant's response (contains tool_use blocks)
            messages.append({"role": "assistant", "content": response.content})

            # Execute each tool call
            tool_results = []
            for block in response.content:
                if block.type == "tool_use":
                    result = tool_handler(block.name, block.input)
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": json.dumps(result) if isinstance(result, (dict, list)) else str(result),
                    })

            messages.append({"role": "user", "content": tool_results})
            continue

        # Final text response
        text = ""
        for block in response.content:
            if hasattr(block, "text"):
                text += block.text

        messages.append({"role": "assistant", "content": text})
        return text, messages

    return "[Chat reached maximum tool-use depth]", messages
