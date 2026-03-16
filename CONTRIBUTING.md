# Contributing to Notely

Thanks for your interest in contributing! Notely is a structured note system for AI-powered retrieval. This guide will help you get set up and make effective contributions.

## Setup

```bash
# Clone the repo
git clone https://github.com/chloeliu/notely.git
cd notely

# Create a virtual environment
python -m venv .venv
source .venv/bin/activate

# Install in dev mode with test dependencies
pip install -e ".[dev]"

# Run the test suite
python -m pytest tests/ -v
```

You'll need Python 3.10+. The test suite doesn't require an API key or initialized workspace.

## Running Tests

```bash
# All tests
python -m pytest tests/ -v

# Specific test file
python -m pytest tests/test_db.py -v

# With coverage
python -m pytest tests/ --cov=notely --cov-report=term-missing
```

All PRs must pass the existing test suite. Add tests for new functionality.

## Project Structure

```
src/notely/
  cli.py              # CLI entry point
  mcp_server.py       # MCP server for Claude Desktop
  config.py           # Config loading, workspace discovery
  models.py           # Pydantic data models
  db.py               # SQLite + FTS5 search
  storage.py          # Markdown file I/O, CSV sync
  ai.py               # Anthropic API integration
  templates.py        # User-editable prompt templates
  routing.py          # Duplicate detection + folder routing
  vectors.py          # LanceDB vector store
  commands/
    open_cmd/         # `notely open` interactive session (package)
    dump.py           # `notely dump` one-shot processing
    todo.py           # `notely todo` management
    ...
tests/                # pytest test suite
```

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for a deep dive into the pipeline, data model, and extension patterns.

## Key Principles

1. **Markdown is the source of truth.** Data flows one way: `.md` files -> SQLite -> vectors/CSV. Never update the DB and sync back.

2. **Shared logic in shared modules.** If two commands need the same operation, put it in `storage.py`, `db.py`, or `routing.py` -- not duplicated across command files.

3. **Don't over-engineer.** Only build what's needed. Three similar lines are better than a premature abstraction.

4. **Test what matters.** DB operations, routing logic, data transformations. Don't mock everything -- use the real SQLite in-memory DB.

## Common Contribution Patterns

### Adding a CLI command

1. Create `src/notely/commands/yourcommand.py`
2. Register it in `src/notely/cli.py`
3. Add tests in `tests/test_yourcommand.py`

### Adding a slash command (inside `notely open`)

1. Add the handler function in `commands/open_cmd/_handlers.py`
2. Add dispatch in `commands/open_cmd/_session.py`
3. Add tab completion in `commands/open_cmd/_completers.py`

### Adding an MCP tool

Add a `@mcp.tool()` function in `mcp_server.py`. Follow the existing patterns for config/DB initialization.

### Customizing AI behavior

Edit templates in a workspace's `templates/` directory -- no code changes needed. See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md#customizing-ai-behavior) for placeholder reference.

## Code Style

- Standard Python conventions (PEP 8)
- Type hints on public functions
- Docstrings on public APIs, not on every internal helper
- Use `rich` for terminal output, `click` for CLI arguments
- Use `logging` (not print) for debug output

## Pull Requests

1. Fork the repo and create a feature branch
2. Make your changes with tests
3. Run `python -m pytest tests/ -v` -- all tests must pass
4. Open a PR with a clear description of what changed and why
5. Keep PRs focused -- one feature or fix per PR

## Reporting Issues

Open an issue on GitHub with:
- What you expected to happen
- What actually happened
- Steps to reproduce
- Python version and OS

## License

By contributing, you agree that your contributions will be licensed under the MIT License.
