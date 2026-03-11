# Notely — Agent Context

Build and test commands, architecture overview, and style guide for AI coding agents.

## Build & Test

```bash
pip install -e ".[dev]"          # Install with dev dependencies
python -m pytest tests/ -v       # Run all tests (170+ tests)
notely open                      # Interactive session (needs API key + workspace)
```

## Architecture

One-way data flow: **Markdown -> SQLite -> LanceDB/CSV**. Markdown files are the source of truth for notes. Databases (todos, contacts, facts, user-created types) live in the SQLite `snippets` table. Secrets live in `.secrets.toml` (separate from both). Everything derived is rebuildable via `notely reindex`.

### Pipeline

```
CAPTURE -> CLASSIFY -> ROUTE -> FORMAT -> SAVE -> INDEX
```

- **Capture**: User pastes text or drags files (`_input.py`, `dump.py`)
- **Classify**: AI decides: structured note, quick todo, or database record (`ai.py`)
- **Route**: Hash check -> vector search -> user confirmation -> folder (`routing.py`)
- **Format**: AI structures content + extracts records inline via `extracted_records` (`ai.py`, `templates.py`)
- **Save**: Write markdown, save extracted records to databases, sync all stores (`storage.py`)
- **Index**: SQLite FTS5 + LanceDB vectors + per-database CSV exports (`db.py`, `vectors.py`)

### Key modules

| Module | Purpose |
|--------|---------|
| `src/notely/db.py` | SQLite database, FTS5 search, all CRUD. Unified `snippets` table stores todos, facts, and user-created databases (discriminated by `snippet_type`) |
| `src/notely/storage.py` | Markdown file I/O, CSV sync, save pipeline (`save_and_sync`), record extraction (`_save_extracted_records`) |
| `src/notely/ai.py` | Anthropic API, prompt building, three-way classification. `extracted_records` field on tool schemas for inline record extraction |
| `src/notely/templates.py` | User-editable prompt templates (classifier, formatter, merger) |
| `src/notely/routing.py` | Duplicate detection + folder routing |
| `src/notely/models.py` | Pydantic data models (Note, ActionItem, SearchFilters, Snippet) |
| `src/notely/vectors.py` | LanceDB vector store, semantic search |
| `src/notely/prompts.py` | Standardized interactive CLI prompts (confirm, pick, duplicate) |
| `src/notely/dedup.py` | Todo deduplication (pure functions, SequenceMatcher) |
| `src/notely/commands/open_cmd/` | Interactive session (package with 9 modules, including `_database_mode.py`) |

### Style

- Data flows one direction: modify Note -> `write_note()` -> `upsert_note()`. Never update DB and sync back.
- Shared logic in shared modules (`storage.py`, `db.py`). Don't duplicate across commands.
- `rich` for terminal output, `click` for CLI, `logging` for debug.
- All DB access through `db.py` methods, never raw SQL from commands.
- LanceDB is fire-and-forget. If it fails, note is still saved.

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the full deep dive.
