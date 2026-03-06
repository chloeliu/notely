# src/notely/ — Core Module Index

This is the main package. All business logic lives here. Commands (in `commands/`) are thin wrappers that call into these modules.

## Module Map

### Data Layer — where notes live

| Module | What it owns | Key classes/functions |
|--------|-------------|----------------------|
| `models.py` | Pydantic data models | `Note`, `ActionItem`, `NoteRouting`, `AIStructuredOutput`, `SearchFilters` (with `folder` filter) |
| `db.py` | SQLite + FTS5 search index | `Database` — CRUD, search, duplicate hash lookups, action items, directory tracking, folder-scoped queries (`get_folder_context`, `search_notes_in_group`). Also: `safe_json_loads()`, `safe_parse_tags()` helpers |
| `storage.py` | Markdown file I/O, CSV sync | `write_note()`, `read_note()`, `save_and_sync()`, `update_action_status()`, `show_merge_preview()`, `classify_input_size()` |
| `vectors.py` | LanceDB semantic search | `VectorStore` — embed/search notes + directories. `get_vector_store()`, `try_vector_sync_note()` |

### Intelligence Layer — AI and routing

| Module | What it owns | Key classes/functions |
|--------|-------------|----------------------|
| `ai.py` | Anthropic API calls, secret masking, folder chat | `structure_only()`, `merge_with_existing()`, `chat_about_notes()`, `mask_secrets()`, `unmask_secrets()` |
| `routing.py` | Duplicate detection + folder placement | `route_input()`, `explore_routing()` — hash check → snippet check → vector search → user prompts |
| `files.py` | File detection + text extraction | `is_file_path()`, `extract_text()`, `copy_attachment()` — text/PDF/image support |
| `secrets.py` | Credential storage | `SecretsStore` — read/write `.secrets.toml`, organized by service name |

### Entry Points — how users access notely

| Module | What it owns | Key classes/functions |
|--------|-------------|----------------------|
| `cli.py` | Click CLI group, command registration | `cli` group — registers all commands from `commands/` |
| `mcp_server.py` | MCP tools for Claude Desktop | 14 tools: `find_similar`, `save_note`, `update_note`, `search_notes`, `get_taxonomy`, etc. |
| `onboarding.py` | Interactive workspace setup | `run_onboarding()` — freeform questions → AI generates `config.toml` |

### Configuration

| Module | What it owns | Key classes/functions |
|--------|-------------|----------------------|
| `config.py` | Config loading, path management | `NotelyConfig` — auto-discovers `config.toml` (walks up like git), computes all paths |

## Data Flow

```
                    ┌──────────────────┐
                    │   User Input     │
                    │  (paste, file,   │
                    │   CLI, MCP)      │
                    └────────┬─────────┘
                             │
              ┌──────────────┼──────────────┐
              ▼              ▼              ▼
         routing.py      ai.py        files.py
         (where?)     (structure)   (extract text)
              │              │              │
              └──────────────┼──────────────┘
                             ▼
                        models.py
                      (Note object)
                             │
              ┌──────────────┼──────────────┐
              ▼              ▼              ▼
         storage.py       db.py        vectors.py
         (write .md)   (index SQL)   (embed vectors)
              │              │              │
              ▼              ▼              ▼
          notes/*.md     index.db      .vectors/
       (source of truth)  (derived)    (derived)
```

**One-way flow.** Markdown is always the source of truth. SQLite and vectors are derived indexes that rebuild from markdown via `notely reindex`.

## Which Module to Modify

| If you need to... | Look at |
|-------------------|---------|
| Add a new data field to notes | `models.py` → `storage.py` (frontmatter) → `db.py` (schema) |
| Change how AI structures notes | `ai.py` (prompts and tool schemas) |
| Change duplicate detection thresholds | `routing.py` (CLI) or `mcp_server.py` (`find_similar`) |
| Add a new file type for extraction | `files.py` (`_TEXT_EXTENSIONS`, `extract_text()`) |
| Add a new MCP tool | `mcp_server.py` (add `@mcp.tool()` function) |
| Add a new CLI command | `commands/yourcommand.py` + register in `cli.py` |
| Change how notes are stored on disk | `storage.py` (`write_note`, `read_note`) |
| Change search behavior | `db.py` (FTS5 queries) or `vectors.py` (semantic search) |
| Change workspace config options | `config.py` (`NotelyConfig`, `SpaceConfig`) |
| Add a new secret store backend | `secrets.py` (`SecretsStore`) |
| Change folder chat behavior | `ai.py` (`chat_about_notes`, `_build_chat_system_prompt`), `open_cmd.py` (`_chat_mode`, `_make_chat_tool_handler`) |
| Add folder-scoped queries | `db.py` (`get_folder_context`) + `SearchFilters.folder` in `models.py` |

## Key Patterns

- **Fire-and-forget vectors**: every vector operation is wrapped in try/except. If LanceDB fails, the note is still saved to markdown + SQLite. Vectors rebuild on `reindex`.
- **`hash_source` parameter**: `db.upsert_note(note, hash_source=text)` — ensures the same text is used for both hash lookup and hash storage. Always pass the paste content, not the full typed input.
- **Shared status updates**: `update_action_status()` in `storage.py` is the single entry point for marking todos done/open. Used by `open_cmd.py`, `todo.py`, and `mcp_server.py`.
- **Folder filtering**: Group is in `file_path` (e.g. `projects/decipherhealth/slug.md`), NOT in `space_metadata`. Use `SearchFilters(folder=group_slug)` or `file_path LIKE '{space}/{group_slug}/%'`. Never use `json_extract(space_metadata, ...)` for group filtering.
- **Config auto-discovery**: `find_notely_root()` walks up from cwd looking for `config.toml`, like git finds `.git/`.

## Deep Architecture

For the full pipeline, data model, embedding design, and extension patterns, see [`docs/ARCHITECTURE.md`](../../docs/ARCHITECTURE.md).
