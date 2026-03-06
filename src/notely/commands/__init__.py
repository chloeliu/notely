"""CLI subcommands for notely.

Each file is one `notely <command>`. Commands are thin wrappers around
core modules (storage, db, routing, ai). Business logic should NOT live
in command files — put it in the appropriate core module instead.

Commands are registered in cli.py via `cli.add_command()`.
See README.md in this directory for the full command index.
"""
