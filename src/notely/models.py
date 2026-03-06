"""Pydantic models for notes, search, and AI output."""

from __future__ import annotations

from datetime import date, datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class Refinement(str, Enum):
    RAW = "raw"
    AI_STRUCTURED = "ai-structured"
    HUMAN_REVIEWED = "human-reviewed"


class InputSize(str, Enum):
    SMALL = "small"
    MEDIUM = "medium"
    LARGE = "large"


class ActionItemStatus(str, Enum):
    OPEN = "open"
    DONE = "done"


class ContentStatus(str, Enum):
    SEED = "seed"
    DRAFT = "draft"
    USED = "used"


class SnippetType(str, Enum):
    IDENTIFIER = "identifier"  # NPI, account number, member ID
    BOOKMARK = "bookmark"      # URL with description
    FACT = "fact"              # Quick fact, relationship, preference


class ActionItem(BaseModel):
    owner: str
    task: str
    due: str | None = None
    status: ActionItemStatus = ActionItemStatus.OPEN


class Note(BaseModel):
    """Core note model — maps to both frontmatter and DB row."""

    id: str
    space: str
    title: str
    source: str = "manual"
    refinement: Refinement = Refinement.RAW
    input_size: InputSize = InputSize.MEDIUM
    date: str  # YYYY-MM-DD
    created: str  # ISO 8601
    updated: str  # ISO 8601
    summary: str = ""
    tags: list[str] = Field(default_factory=list)
    participants: list[str] = Field(default_factory=list)
    file_path: str = ""
    body: str = ""
    raw_text: str = ""
    action_items: list[ActionItem] = Field(default_factory=list)
    related_contexts: list[str] = Field(default_factory=list)
    source_url: str = ""
    attachments: list[str] = Field(default_factory=list)
    space_metadata: dict[str, Any] = Field(default_factory=dict)


class NoteRouting(BaseModel):
    """AI routing decision for where a note belongs."""

    space: str
    group_slug: str
    group_display: str
    group_is_new: bool = False
    subgroup_slug: str | None = None
    subgroup_display: str | None = None
    subgroup_is_new: bool = False
    subgroup_description: str | None = None
    append_to_note: str | None = None


class InboxItemStatus(str, Enum):
    PENDING = "pending"
    FILED = "filed"
    SKIPPED = "skipped"


class InboxItem(BaseModel):
    """An item waiting for user review in the inbox."""

    id: str                    # uuid
    source: str                # agent name: "granola-sync"
    source_id: str = ""        # external dedup key: "granola:meeting_abc123"
    type: str = "note"         # "note" (future: "snippet", "todo")

    # Note-shaped fields
    title: str = ""
    summary: str = ""
    body: str = ""
    tags: list[str] = Field(default_factory=list)
    participants: list[str] = Field(default_factory=list)
    action_items: list[ActionItem] = Field(default_factory=list)
    source_url: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)
    processed: bool = True  # False = raw content, needs AI structuring at review

    # Routing
    suggested_space: str = ""
    suggested_group: str = ""

    # Status
    status: InboxItemStatus = InboxItemStatus.PENDING
    created: str = ""          # ISO 8601
    reviewed_at: str = ""
    filed_note_id: str = ""    # → notes.id after filing


class Snippet(BaseModel):
    """A short reference item stored in the DB — identifier, bookmark, or fact."""

    id: int = 0  # auto-increment
    space: str = ""
    group_slug: str = ""  # folder scoping
    entity: str = ""      # who/what: "labcorp", "dr-smith", "canvas-medical"
    key: str = ""         # label: "npi", "docs_url", "ehr_platform"
    value: str = ""       # the data: "9876543210", "https://...", "Canvas Medical"
    description: str = ""  # optional context
    snippet_type: SnippetType = SnippetType.FACT
    tags: list[str] = Field(default_factory=list)
    created: str = ""
    note_id: str | None = None  # linked note (if extracted from a note)


class AIStructuredOutput(BaseModel):
    """Full output from AI structuring call."""

    routing: NoteRouting
    metadata: AIMetadata
    body_markdown: str
    related_contexts: list[str] = Field(default_factory=list)


class AIMetadata(BaseModel):
    """Metadata extracted by AI from raw text."""

    title: str
    source: str = "manual"
    date: str
    participants: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    summary: str
    action_items: list[ActionItem] = Field(default_factory=list)
    # Space-specific fields the AI may set
    extra: dict[str, Any] = Field(default_factory=dict)


class SearchFilters(BaseModel):
    """Filters for note search."""

    space: str | None = None
    folder: str | None = None  # group_slug — filters via file_path prefix
    tags: list[str] = Field(default_factory=list)
    source: str | None = None
    refinement: list[str] = Field(default_factory=list)
    date_from: str | None = None
    date_to: str | None = None
    # Space-specific filters (stored as space_metadata keys)
    client: str | None = None
    topic: str | None = None
    category: str | None = None
    content_status: str | None = None


class SearchQuery(BaseModel):
    """Full search query for the query command."""

    intent: str = "search_notes"
    space: str | None = None
    query: str | None = None  # FTS text query
    filters: SearchFilters = Field(default_factory=SearchFilters)
    options: SearchOptions = Field(default_factory=lambda: SearchOptions())
    # get_context fields
    client: str | None = None
    note_id: str | None = None
    content_status: str | None = None


class SearchOptions(BaseModel):
    limit: int = 20
    include_body: bool = False
    include_raw: bool = False
    sort_by: str = "recency"


class SearchResult(BaseModel):
    """A single search result."""

    id: str
    title: str
    space: str
    date: str
    refinement: str
    summary: str
    tags: list[str] = Field(default_factory=list)
    file_path: str = ""
    action_items_open: int = 0
    body: str | None = None
    space_metadata: dict[str, Any] = Field(default_factory=dict)


class SearchResponse(BaseModel):
    """Full search response."""

    status: str = "ok"
    count: int = 0
    results: list[SearchResult] = Field(default_factory=list)


class TaxonomyGroup(BaseModel):
    """A group within a space (e.g., a client or category)."""

    slug: str
    display: str
    description: str = ""
    note_count: int = 0
    last_note: str | None = None
    subgroups: list[TaxonomySubgroup] = Field(default_factory=list)


class TaxonomySubgroup(BaseModel):
    slug: str
    display: str = ""
    description: str = ""
    note_count: int = 0
    last_note: str | None = None


class SpaceTaxonomy(BaseModel):
    """Full taxonomy for one space — sent to AI for routing."""

    name: str
    description: str
    group_by: str
    subgroup_by: str | None = None
    groups: list[TaxonomyGroup] = Field(default_factory=list)


class ContextResponse(BaseModel):
    """Response for get_context intent."""

    status: str = "ok"
    space: str
    overview: dict[str, Any] = Field(default_factory=dict)
    recent_notes: list[SearchResult] = Field(default_factory=list)
    open_action_items: list[dict[str, Any]] = Field(default_factory=list)
