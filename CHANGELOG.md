# Changelog

All notable changes to notely are documented here.

## [0.2.0] - 2026-03-10

### Added
- **Database system** — unified `snippets` table stores todos, contacts, facts, and user-created databases. `snippet_type` column as discriminator. `/_meta` rows for database metadata (description, fields, auto-extract flag).
- **Interactive database mode** — `/<db_name>` enters a sub-mode for any database. Add, edit, delete, search, browse records. Tab completion for commands, entities, and keys.
- **Inline record extraction** — AI extracts database records alongside note structuring in a single call via `extracted_records`. Databases with `extract_from_notes` enabled get records automatically.
- **Database creation flow** — create new databases on the fly: name, description, expected fields, auto-extract toggle. Databases grow organically as data is pasted.
- **Per-database CSV export** — each database gets its own `_<name>.csv` file (e.g. `_contacts.csv`, `_providers.csv`).
- **Entity name dedup** — fuzzy matching prevents near-duplicate entities (e.g. "Jose Rodriguez" vs "Jos\u00e9 Rodriguez").
- **Re-extraction prevention** — when re-pasting a note, existing records are passed to the AI as context so it doesn't re-extract them.
- **`notely reindex` rebuilds databases** — snippet databases are preserved and re-synced during reindex.
- **User-editable prompt templates** — customize how AI classifies, structures, and merges notes via `templates/` directory. Three templates: `classifier.md`, `formatter.md`, `merger.md`.
- **`/secret` command** — view stored secrets with tab autocomplete on service and key names.
- **Standardized interactive prompts** — `prompts.py` module with 5 reusable functions for all CLI user interaction. Consistent UX across all commands.

### Changed
- Todos are now a built-in database (`snippet_type='todo'`) instead of a separate `action_items` table. Legacy table migrated automatically on startup.
- Schema cleanup: removed legacy `action_items` table from `SCHEMA_SQL`, renamed `refs_` triggers/indexes to `snippets_`.
- All `snippet_type` defaults unified to `"fact"` (was inconsistently `"references"` in Python code vs `"fact"` in DB schema).
- Fixed broken MCP `store_reference()` mapping that sent `"fact"` type to `"references"` database.
- Three-way AI classification: `structure_note` / `add_list_item` / `add_snippet` in a single API call.
- Classifier and formatter templates use `{databases_str}` placeholder — no hardcoded database names.
- Record dedup matches on task text only (no owner matching) — AI assigns owners non-deterministically.

### Fixed
- MCP deprecated `store_reference()` mapping was broken — `snippet_type="fact"` incorrectly mapped to `database="references"`.
- Version mismatch between `__init__.py` (0.1.0) and `pyproject.toml` (0.1.2).

## [0.1.2] - 2026-03-06

### Added
- Standardized interactive prompts (`prompts.py`) replacing 25+ inline `Prompt.ask()` patterns.
- `/secret` command for viewing stored secrets with tab autocomplete.
- Direct folder typing at routing prompts — type `clients/acme` instead of picking a number.
- Eager folder creation during routing (mkdir + DB + vectors immediately).
- Routing autocomplete shows config spaces on empty tab.

### Fixed
- Rich markup eating hotkey brackets in routing prompts.

## [0.1.1] - 2026-03-05

### Added
- User-editable prompt templates (`templates/` directory).
- Interactive `/todo` sub-mode with assign, move, delete, plan commands.
- Todo display dedup — merges duplicate todos accumulated across notes.
- `@note` autocomplete in main prompt and agent mode.
- `/delete` and `/edit` folder-to-note autocomplete.
- Conditional `complete_while_typing` — autocomplete only for `/` commands and `@` references.

### Changed
- `open_cmd.py` monolith (4,000 lines) split into package with 8 focused modules.

## [0.1.0] - 2026-03-04

Initial release.

- AI-powered note structuring from raw text (meeting notes, Slack threads, quick thoughts).
- Three-layer duplicate detection (exact hash, snippet hash, vector search).
- Folder routing via vector search + user confirmation.
- Full-text search (SQLite FTS5) + semantic search (LanceDB).
- Action item extraction with owner and due date.
- `/chat` folder-scoped conversational mode.
- `/clip` web page clipping (optional Firecrawl dependency).
- Secret masking (`|||secret|||` markers).
- MCP server for Claude Desktop / Claude Max integration.
- File attachments (text, PDF, images with Vision API).
- Time tracking (`/timer`).
- CSV export (`_todos.csv`).
