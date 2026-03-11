"""Notely: Structured note system for AI-powered retrieval.

Package structure:
    config.py       — Config loading, path management, auto-discovery
    models.py       — Pydantic data models (Note, ActionItem, SearchFilters, etc.)
    db.py           — SQLite + FTS5 search index, CRUD, duplicate detection
    storage.py      — Markdown file I/O, CSV sync, action item status updates
    ai.py           — Anthropic API integration, secret masking/unmasking
    routing.py      — Duplicate detection + folder routing (hash → snippet → vector)
    vectors.py      — LanceDB vector store for semantic search
    files.py        — File detection, text extraction (text/PDF/image), attachments
    secrets.py      — Credential storage in .secrets.toml
    onboarding.py   — Interactive workspace setup wizard
    cli.py          — Click CLI entry point, command registration
    mcp_server.py   — MCP server (14 tools) for Claude Desktop integration
    commands/       — CLI subcommands (see commands/README.md)

See README.md in this directory for data flow diagrams and module guide.
See docs/ARCHITECTURE.md for architecture details and extension patterns.
"""

__version__ = "0.2.0"
