# src/notely/commands/ — CLI Command Index

Each file is one CLI subcommand, registered in `cli.py`. Commands are thin wrappers — business logic lives in the core modules (`storage.py`, `db.py`, `routing.py`, `ai.py`).

## Command Map

### Interactive

| Command | File | What it does | Core modules used |
|---------|------|-------------|-------------------|
| `notely open` | `open_cmd.py` | Persistent session — paste notes, drag files, slash commands (`/chat`, `/done`, `/edit`, `/delete`, `/sync`, `/mkdir`, `/rmdir`), tab autocomplete | `routing.py`, `ai.py`, `storage.py`, `db.py`, `vectors.py`, `files.py` |
| `notely init` | `init.py` | Set up a new workspace — freeform questions, AI generates `config.toml` | `onboarding.py`, `config.py` |
| `notely edit <id>` | `edit.py` | Open note in `$EDITOR`, re-indexes to SQLite + vectors on save | `storage.py`, `db.py`, `vectors.py` |

### One-Shot Processing

| Command | File | What it does | Core modules used |
|---------|------|-------------|-------------------|
| `notely dump` | `dump.py` | Pipe text in, AI structures it, save. Supports `--file`, `--raw` (skip AI), merge prompts | `ai.py`, `routing.py`, `storage.py`, `db.py`, `files.py` |

### Read / Query

| Command | File | What it does | Core modules used |
|---------|------|-------------|-------------------|
| `notely search <query>` | `search_cmd.py` | Full-text search with space/tag/client filters | `db.py` |
| `notely query` | `query_cmd.py` | Structured JSON output for AI agents. Supports `--space`, `--client`, `--todos`, `--recent` | `db.py` |
| `notely list` | `list_cmd.py` | List recent notes, optionally filter by space | `db.py` |
| `notely show <id>` | `show.py` | Display a full note with metadata, action items, attachments | `storage.py`, `db.py` |
| `notely spaces` | `spaces.py` | Show configured spaces with note counts and recent activity | `db.py`, `config.py` |

### Management

| Command | File | What it does | Core modules used |
|---------|------|-------------|-------------------|
| `notely todo` | `todo.py` | View/manage action items — `list`, `done <id>`, `reopen <id>` | `db.py`, `storage.py` (`update_action_status`) |
| `notely ideas` | `ideas.py` | View/manage ideas pipeline — board view with status columns | `db.py` |
| `notely reindex` | `reindex.py` | Rebuild SQLite + vectors from `.md` files. Run after manual edits or to fix drift | `storage.py`, `db.py`, `vectors.py` |

## Adding a New Command

1. Create `src/notely/commands/yourcommand.py`
2. Define a click command:
   ```python
   """notely yourcommand — short description."""
   import click

   @click.command("yourcommand")
   @click.pass_context
   def yourcommand_cmd(ctx):
       """One-line help text shown in `notely --help`."""
       config = ctx.obj["config"]
       # ... your logic, calling core modules
   ```
3. Register in `cli.py`:
   ```python
   from .commands.yourcommand import yourcommand_cmd
   cli.add_command(yourcommand_cmd, "yourcommand")
   ```

## Slash Commands (inside `notely open`)

These are handled in `open_cmd.py`, not as separate command files:

| Slash command | What it does |
|--------------|-------------|
| `/chat <folder>` | Enter conversational AI mode scoped to a folder. Tab-complete folder names. `/back` to exit. |
| `/done <id>` | Mark action item as done |
| `/edit <id>` | Edit a note in `$EDITOR` |
| `/delete <id>` | Delete a note (with confirmation) |
| `/sync` | Re-read all `.md` files into SQLite + vectors |
| `/mkdir <path>` | Create a new group/subgroup directory |
| `/rmdir <path>` | Remove a directory (with confirmation) |
| `/help` | Show available slash commands |
| `/quit` | Exit the session |

### `/chat` details

Folder-scoped conversational AI. Loads all notes in the selected folder, then lets you ask questions or request deliverables.

- **Folder matching**: Fuzzy substring match on `display_name` and `group_slug`. Multiple matches show a numbered picker.
- **Small folders** (<20 notes): full note bodies pre-loaded into system prompt. Single API round trip.
- **Large folders** (≥20 notes): summaries in system prompt + `search_notes`/`get_note_body` tools. AI searches within the folder as needed (hybrid FTS + vector, both folder-scoped).
- **Tab autocomplete**: type `/chat ` then Tab to see available folders.
- **Exit**: `/back`, `/exit`, `/quit`, or `/q` returns to the main `>` prompt.
