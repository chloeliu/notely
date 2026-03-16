# src/notely/commands/ — CLI Command Index

Each file is one CLI subcommand, registered in `cli.py`. Commands are thin wrappers — business logic lives in the core modules (`storage.py`, `db.py`, `routing.py`, `ai.py`).

## Command Map

| Command | File | What it does |
|---------|------|-------------|
| `notely init` | `init.py` | Set up a new workspace — interactive wizard |
| `notely open` | `open_cmd/` | Interactive session — paste notes, slash commands, everything |
| `notely dump` | `dump.py` | One-shot: pipe text in, AI structures, save |
| `notely search` | `search_cmd.py` | Full-text + semantic search from the terminal |
| `notely todo` | `todo.py` | View/manage action items |
| `notely edit <id>` | `edit.py` | Open note in `$EDITOR`, re-indexes on save |
| `notely query` | `query_cmd.py` | JSON query API for agents and scripts |
| `notely reindex` | `reindex.py` | Rebuild SQLite + vectors from `.md` files |

Most of these are also available as slash commands inside `notely open` (`/search`, `/todo`, `/edit`, etc.).

## The `open_cmd` Package

`notely open` is the main interactive session. It's a Python package with focused modules:

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
| `_shared.py` | Shared utilities (leaf module) |

Import graph is acyclic — `_shared.py` is the leaf, `_session.py` is the root.

## Adding a New Command

1. Create `src/notely/commands/yourcommand.py`
2. Define a click command and register in `cli.py`

For slash commands inside `notely open`, add the handler in `_handlers.py` and dispatch in `_session.py`.
