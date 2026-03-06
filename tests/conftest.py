"""Shared test fixtures for notely."""

from __future__ import annotations

import pytest
from pathlib import Path

from notely.config import NotelyConfig, DEFAULT_CONFIG
from notely.db import Database


@pytest.fixture
def tmp_workspace(tmp_path: Path) -> Path:
    """Create a minimal notely workspace in a temp directory."""
    config_file = tmp_path / "config.toml"
    config_file.write_text(DEFAULT_CONFIG)
    (tmp_path / "notes").mkdir()
    (tmp_path / ".raw").mkdir()
    return tmp_path


@pytest.fixture
def config(tmp_workspace: Path) -> NotelyConfig:
    """Return a NotelyConfig pointed at the temp workspace."""
    return NotelyConfig(base_dir=tmp_workspace)


@pytest.fixture
def db(tmp_workspace: Path) -> Database:
    """Return an initialized Database in the temp workspace."""
    db = Database(tmp_workspace / "index.db")
    db.initialize()
    yield db
    db.close()
