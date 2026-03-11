```
 ███╗   ██╗ ██████╗ ████████╗███████╗██╗  ██╗   ██╗
 ████╗  ██║██╔═══██╗╚══██╔══╝██╔════╝██║  ╚██╗ ██╔╝
 ██╔██╗ ██║██║   ██║   ██║   █████╗  ██║   ╚████╔╝
 ██║╚██╗██║██║   ██║   ██║   ██╔══╝  ██║    ╚██╔╝
 ██║ ╚████║╚██████╔╝   ██║   ███████╗███████╗██║
 ╚═╝  ╚═══╝ ╚═════╝    ╚═╝   ╚══════╝╚══════╝╚═╝
 A notes system that works for you and your AI.
```

[![PyPI](https://img.shields.io/pypi/v/notely)](https://pypi.org/project/notely/)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-green.svg)](https://www.python.org/downloads/)
[![Tests](https://github.com/chloeliu/notely/actions/workflows/test.yml/badge.svg)](https://github.com/chloeliu/notely/actions/workflows/test.yml)

Paste your meeting notes, Slack threads, or quick thoughts. AI organizes them into searchable markdown files. You never have to sort, tag, or file anything yourself.

```
You paste this:                          You get this:

  hey just got off the call with          notes/clients/acme/2026-03-05_acme-kickoff.md
  Jake from Acme. they want to            ---
  launch by Q3, need us to scope          title: Acme Kickoff Call
  the API integration first.              summary: Acme wants Q3 launch, API integration
  Jake will send the spec by                scoping needed first.
  Friday. budget is 50k.                  tags: [kickoff, api, acme]
                                          participants: [Jake]
                                          action_items:
                                            - task: Send API spec
                                              owner: Jake
                                              due: 2026-03-07
                                          ---
                                          Full structured notes here...
```

Notely handles the rest: duplicate detection (won't save the same paste twice), folder routing (figures out where it goes), action item extraction, and full-text + semantic search across everything.

## Quick Start

```bash
pip install notely

# Verify it installed
notely --help

# Pick any folder — this is where your notes will live
mkdir my-notes && cd my-notes

# Interactive setup — asks for your API key and designs your filing system
notely init

# Start capturing
notely open
```

Notely uses the Anthropic API (Claude) to structure your notes. `notely init` will ask for your API key if you don't have one — get it at [console.anthropic.com](https://console.anthropic.com/).

> **`notely` command not found?** Try `python -m notely --help`. If that works, your Python scripts directory isn't in your PATH. Run `pip show notely` to find where it's installed.

### What `notely init` does

`notely init` is an interactive wizard. You describe your work, and the AI creates a folder structure that fits:

```
$ notely init

  What kind of notes will you be taking?
  > I manage 3 client accounts and have personal stuff too

  Creating workspace...

  my-notes/
  ├── config.toml
  └── notes/
      ├── clients/
      │   ├── acme/
      │   └── globex/
      └── personal/
```

You can always add folders later with `/mkdir` inside `notely open`, or let the AI create new folders automatically as your notes come in.

After init, run `notely open` to start an interactive session. Paste anything — meeting notes, Slack threads, quick thoughts. The AI structures it and files it in the right folder as a clean markdown file.

## What It Looks Like

### Capturing notes

```
notely-notetaker> [paste your meeting notes, Slack thread, anything]

  Preview
  ──────────────────────────────────────
  Acme Kickoff Call
  Acme wants Q3 launch, API integration scoping needed first.
  tags: kickoff, api, acme
  participants: Jake

  Action items:
    [ ] Send API spec — Jake, due Fri
  ──────────────────────────────────────

  [Y]es, save / [e]dit first / [n]o, skip: y
  Saved: clients/acme/2026-03-05_acme-kickoff.md
```

### Managing todos

```
notely-notetaker> /todo

  ★ Today
  ─────────────────────────────────────
    1. Deploy v2 to staging              Chloe · due today
    2. Fix auth bug                      Chloe · due Fri

  Acme
  ─────────────────────────────────────
    3. Send API spec                     Jake · due Fri
    4. Review SOW                        Chloe · due Mon

  4 open — done · add · today · due · timer · q
```

### Searching

```bash
notely search "API integration"

  1. Acme Kickoff Call (2026-03-05) [clients/acme]
     Acme wants Q3 launch, API integration scoping needed first.

  2. Platform Architecture (2026-02-28) [projects/vault]
     REST API design decisions for the Vault project.
```

### Chatting with your notes

```
notely-notetaker> /chat acme

notely-chat (Acme)> what are the open items for Acme?

  Based on your notes, here are the open items:
  1. Jake needs to send the API spec (due Friday)
  2. SOW review is pending (due Monday)
  3. No timeline set for scoping yet
```

## How It Works

```mermaid
flowchart TD
    A["You paste raw text"] --> B{"AI classifies it<br/>and files it"}
    B -->|"Structured content"| C["Full note<br/>(title, summary, tags,<br/>action items)"]
    B -->|"Quick task or idea"| D["Todo or Idea<br/>(one-liner, due date)"]
    B -->|"Reference data"| E["Database record<br/>(contact, NPI, account)"]
    C --> F{"Duplicate?"}
    F -->|"Match found"| G["Merge into<br/>existing note"]
    F -->|"No match"| H["Route to folder"]
    G --> I["Saved as Markdown"]
    H --> I
    D --> I
    E --> J["Saved to index"]
    I --> K["Indexed + searchable"]
    J --> K

    style B fill:#e3f2fd,stroke:#1976d2
    style I fill:#d4edda,stroke:#28a745,stroke-width:2px
```

The AI decides what your input is and files it in the right place — you never have to. Meeting notes become structured notes with action items and database records extracted in the same call, filed into the right client folder. "Call dentist Friday" becomes a todo. An account number becomes a searchable database record. Duplicates are caught automatically before anything is saved.

**Markdown files are the source of truth.** Everything else (search index, vectors, CSV exports) is derived and can be rebuilt with `notely reindex`. You can edit your notes by hand in any text editor — notely respects your changes.

### Data Architecture

```mermaid
flowchart LR
    subgraph Source of Truth
        MD["Markdown files<br/>notes/**/*.md"]
    end
    subgraph Derived - rebuildable
        DB["SQLite + FTS5<br/>index.db"]
        VEC["LanceDB<br/>.vectors/"]
        CSV["CSV exports<br/>_todos.csv, _contacts.csv, ..."]
    end

    MD --> DB --> VEC
    DB --> CSV

    style MD fill:#d4edda,stroke:#28a745,stroke-width:2px
    style DB fill:#fff3cd,stroke:#ffc107
    style VEC fill:#fff3cd,stroke:#ffc107
    style CSV fill:#fff3cd,stroke:#ffc107
```

## Two Ways to Use It

### CLI (default)

Uses the Anthropic API to structure your notes. Requires an API key.

```bash
notely open          # Interactive session
notely dump < file   # One-shot: pipe text in, get structured note out
```

### MCP Server (Claude Desktop / Claude Max)

Claude becomes the AI — no API calls, no cost. Add to your Claude Desktop config:

```json
{
  "mcpServers": {
    "notely": {
      "command": "python",
      "args": ["-m", "notely.mcp_server"],
      "cwd": "/path/to/your/workspace"
    }
  }
}
```

Both paths produce the same markdown files and search index.

## Commands

| Command | What it does |
|---------|-------------|
| `notely open` | Interactive session — paste notes, drag files, slash commands |
| `notely dump` | One-shot: pipe text in, AI structures, save |
| `notely search <query>` | Full-text search across all notes |
| `notely todo` | View and manage action items |
| `notely list` | List recent notes |
| `notely show <id>` | Display a full note |
| `notely edit <id>` | Open in your editor, re-indexes on save |
| `notely init` | Set up a new workspace |
| `notely reindex` | Rebuild search index from markdown files |

### Inside `notely open`

| Command | What it does |
|---------|-------------|
| `/todo` | Interactive todo mode — mark done, add tasks, flag for today, start timers |
| `/chat <folder>` | AI chat scoped to a folder's notes |
| `/timer <folder> <desc>` | Time tracking |
| `/clip <url>` | Save a web page as a note |
| `/<db_name>` | Enter database interactive mode — add, update, delete, browse records |
| `/secret` | View stored secrets (`/secret service key` to reveal a value) |
| `/folder <name>` | Set a working folder for the session |
| `/edit <id>` | Edit a note in your editor |

## Key Features

**Smart classification** — The AI decides what your input is. Paste meeting notes and it creates a structured note with title, summary, tags, and action items. Type "call dentist Friday" and it creates a todo. Paste an account number or NPI and it stores a searchable database record. You never have to tell it which type — it figures it out.

**Databases** — Notely has a built-in lightweight database system for structured records. Todos, contacts, providers, plain facts — each is a "database" you can query, browse, and export. The AI extracts records inline when structuring notes (one call produces both the note and its todos/contacts). Type `/<name>` (e.g. `/contacts`, `/todo`) to enter interactive mode. Create new databases on the fly — just paste data and notely walks you through setup (name, fields, auto-extract from future notes). Each database gets its own CSV export and full-text search.

**Duplicate detection** — Three layers: exact hash, snippet hash, and semantic search. Notely won't let you save the same meeting notes twice. If it finds a match, it offers to merge the new information in.

**Secret masking** — Wrap sensitive data in `|||secret|||` markers. The values are replaced with `[REDACTED]` before any text is sent to the AI. Secrets are stored in `.secrets.toml` — a local file, completely separate from the database system. Your secrets never leave your machine.

```
You paste:   pypi token |||pypi-AgEIcHl...|||
AI sees:     pypi token [REDACTED]
Saved to:    .secrets.toml → [pypi] api_token = "pypi-AgEIcHl..."
```

Retrieve secrets with `/secret` inside `notely open` — tab-completes service and key names, only shows values when you specify both.

**Folder routing** — AI figures out where each note belongs based on your workspace structure. At any routing prompt, you can type a folder path directly (e.g. `clients/acme`) instead of picking a number — notely resolves it or creates the folder on the spot.

**Web clipping** — `/clip <url>` saves any web page as a structured note. Requires the optional Firecrawl dependency (`pip install "notely[web]"`) and a [Firecrawl API key](https://firecrawl.dev).

**File attachments** — Drag or paste file paths. Supports text, PDF (with table extraction), and images (described via Vision API).

**Customizable AI prompts** — Override how notely classifies, structures, and merges notes by placing template files in your workspace's `templates/` directory. See [Customizing AI Prompts](docs/ARCHITECTURE.md#customizing-ai-behavior) for details.

## Workspace Structure

After running `notely init`, your workspace looks like:

```
my-workspace/
├── config.toml         # Your spaces and settings
├── notes/              # Markdown files (source of truth)
│   ├── clients/
│   │   └── acme/       # One folder per client/project
│   └── personal/
├── index.db            # Search index (auto-generated)
├── _todos.csv          # Todo list (auto-generated)
├── _contacts.csv       # Per-database CSV exports (auto-generated)
└── .env                # Your API key (gitignored)
```

**Spaces** are top-level categories (clients, projects, personal). **Groups** are folders within a space (one per client, project, etc.). Define them in `config.toml` or let `notely init` set them up interactively.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for setup, testing, and PR guidelines. See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the pipeline, data model, and how to extend notely.

```bash
# Developer setup
pip install -e ".[dev]"
python -m pytest tests/ -v    # 170+ tests
```

## License

MIT. See [LICENSE](LICENSE).
