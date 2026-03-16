# Architecture

How notely works under the hood. Read this if you want to contribute, add a feature, or understand the design decisions.

## Design Philosophy

Notely is built around a few ideas that shape everything:

**Markdown is the source of truth.** Notes are plain `.md` files with YAML frontmatter. The SQLite index, vector embeddings, and CSV exports are all derived — you can nuke them and rebuild with `notely reindex`. Users can edit their notes in any text editor, and notely respects their changes.

**One-way data flow.** Data flows from markdown files → SQLite → LanceDB/CSV. Never the other direction. If you need to change a note's status, tags, or anything else, you modify the Note object, write the markdown, then re-index. This keeps things predictable and eliminates sync bugs.

**The AI structures, it doesn't route.** We tried having the AI pick folders — it was bad at it. Now the AI only formats content (title, summary, tags, body). Routing uses hash checks and vector search with user confirmation. Much more reliable.

**Databases are just filtered views.** All structured records (todos, contacts, whatever) live in one `snippets` table. The `snippet_type` column says which "database" a row belongs to. Simple, no schema migrations when users create new databases.

## Pipeline

When you paste text into notely:

```
Capture → Route → Classify + Format → Save + Index
```

1. **Capture** — text comes in (paste, file drag, URL clip)
2. **Route** — check for duplicates (hash → vector search), pick a folder
3. **Classify + Format** — AI decides: full note, quick todo, or database record. Structures it in one call
4. **Save + Index** — write markdown, update SQLite, sync vectors and CSVs

The AI also extracts database records inline (todos, contacts, etc.) during step 3 — one API call handles both the note and its associated records.

## Data Stores

```
Markdown files (.md)     ← source of truth, not derived
       ↓
SQLite (index.db)        ← derived, rebuildable
       ↓
LanceDB (.vectors/)      ← derived, fire-and-forget
CSV exports (_*.csv)     ← derived, auto-synced
```

LanceDB is treated as optional. If vector operations fail, the note still saves. Everything rebuilds from markdown via `notely reindex`.

## Workspace Layout

```
my-workspace/
├── config.toml           # Spaces, groups, user settings
├── index.db              # SQLite + FTS5 (derived)
├── .env                  # API keys (gitignored)
├── .secrets.toml         # Credentials (gitignored)
├── .raw/                 # Original unprocessed input
├── .vectors/             # LanceDB embeddings (derived)
├── _todos.csv            # Auto-synced from DB
├── _<db_name>.csv        # One per user-created database
├── _timelog.csv          # Time tracking
├── templates/            # Optional AI prompt overrides
├── agents/               # YAML workflow definitions
├── notes/                # The actual notes (source of truth)
│   └── <space>/<group>/[<subgroup>/]<date>_<slug>.md
└── attachments/          # File attachments
```

## Source Code

```
src/notely/
├── ai.py              # Claude API, prompt building, record parsing
├── config.py          # Config loading, workspace discovery
├── db.py              # SQLite, FTS5, all CRUD
├── storage.py         # Markdown I/O, CSV sync, save pipeline
├── routing.py         # Duplicate detection, folder routing
├── models.py          # Pydantic models (Note, ActionItem, etc.)
├── vectors.py         # LanceDB vector store
├── templates.py       # User-editable prompt templates
├── prompts.py         # Standardized CLI prompts
├── files.py           # File detection, PDF/image extraction
├── secrets.py         # .secrets.toml management
├── timer.py           # Time tracking
├── dedup.py           # Todo deduplication
├── web.py             # Web clipping (optional)
├── mcp_server.py      # MCP server for Claude Desktop
├── cli.py             # Click entry point
│
└── commands/
    ├── open_cmd/      # Interactive session (see below)
    ├── dump.py        # One-shot note processing
    ├── todo.py        # CLI todo management
    ├── search_cmd.py  # CLI search
    └── ...
```

### The `open_cmd` package

`notely open` is the main interactive session. It was getting too big as a single file, so it's split into focused modules:

| Module | What it does |
|--------|-------------|
| `_session.py` | Main loop, command dispatch |
| `_input.py` | Note capture pipeline (paste → AI → preview → save) |
| `_handlers.py` | Slash command handlers |
| `_todo_mode.py` | Interactive `/todo` sub-mode |
| `_database_mode.py` | Interactive `/<db>` sub-mode |
| `_completers.py` | Tab completion for everything |
| `_agent.py` | `/agent` and `/chat` modes |
| `_inbox.py` | Inbox review flow |
| `_shared.py` | Shared utilities (leaf module, no internal deps) |

Import graph is acyclic — `_shared.py` is the leaf, `_session.py` is the root.

## Key Design Decisions

### Folder queries use `file_path`, not metadata

The `space` column on notes is unreliable (empty after reindex). Folder-scoped queries use `WHERE file_path LIKE 'space/group/%'` instead. This is centralized in `SearchFilters.folder`.

### Duplicate detection is three layers

1. **Exact hash** — SHA256 of full paste content
2. **Snippet hash** — SHA256 of first 300 chars (catches edits at the end)
3. **Vector search** — semantic similarity via LanceDB

Hashes use paste content only, not typed context. So `"meeting notes [paste]"` and `"[paste]"` match the same hash.

### Databases share one table

All databases — todos, contacts, facts, user-created — live in the `snippets` table with `snippet_type` as the discriminator. Database metadata (description, fields, auto-extract flag) is stored as `_meta` entity rows in the same table.

### Secrets are separate from everything

`.secrets.toml` is a standalone TOML file. Not in the database, not in markdown. `|||markers|||` trigger masking before AI calls and auto-capture to the file.

## How to Contribute

### Adding a slash command

1. Write the handler in `_handlers.py`
2. Add the dispatch case in `_session.py`
3. Add tab completion in `_completers.py`

### Adding an interactive sub-mode

Look at `_todo_mode.py` as a reference. The pattern is: create a `PromptSession` with a custom completer, run a `while True` loop dispatching commands, break on `q` or `/back`.

```python
def _your_mode(config: NotelyConfig) -> None:
    session = PromptSession(completer=YourCompleter())
    while True:
        try:
            text = session.prompt("\nnotely-yourmode> ")
        except (EOFError, KeyboardInterrupt):
            break
        if text.strip().lower() in ("q", "/back"):
            break
        # handle commands
```

### Working with the database

```python
from ..db import Database

with Database(config.db_path) as db:
    db.initialize()
    items = db.get_open_todos()
    # db.close() called automatically
```

### Customizing AI prompts

Users can override AI behavior by placing template files in `templates/`:

- `classifier.md` — how input is classified (note vs todo vs database record)
- `formatter.md` — how notes are structured (title, summary, tags, body)
- `merger.md` — how new content merges into existing notes

Templates use `{placeholder}` syntax filled at runtime. See the built-in defaults in `templates.py` for the full list of available placeholders. Override one without affecting the others.

## MCP Server

21 tools for Claude Desktop / Claude Max integration. Claude becomes the AI — no API calls, no cost. Key tools: `find_similar` (duplicate check), `save_note`/`update_note`, `search_notes`, `store_record`/`get_records`, `add_todo`/`complete_todo`.

Write tools only fire when the user explicitly asks. Read tools are safe for proactive use.

## Testing & CI

```bash
pip install -e ".[dev]"
python -m pytest tests/ -v    # 240+ tests
```

CI runs on every push via GitHub Actions (Python 3.10–3.13). PyPI publishing is automated on `v*` tags via Trusted Publishing.

## Dependencies

**Core:** click, rich, anthropic, pydantic, python-frontmatter, python-slugify, mcp, prompt-toolkit, lancedb, fastembed

**Optional:** `pip install "notely[pdf]"` for PDF extraction, `pip install "notely[web]"` for web clipping
