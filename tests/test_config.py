"""Tests for config.py: NotelyConfig, find_notely_root, ensure_initialized."""

from __future__ import annotations

import pytest
from pathlib import Path

from notely.config import NotelyConfig, find_notely_root, DEFAULT_CONFIG


class TestFindNotelyRoot:
    def test_finds_root_in_current_dir(self, tmp_path: Path):
        (tmp_path / "config.toml").write_text(DEFAULT_CONFIG)
        assert find_notely_root(tmp_path) == tmp_path

    def test_finds_root_in_parent(self, tmp_path: Path):
        (tmp_path / "config.toml").write_text(DEFAULT_CONFIG)
        child = tmp_path / "sub" / "deep"
        child.mkdir(parents=True)
        assert find_notely_root(child) == tmp_path

    def test_returns_none_when_not_found(self, tmp_path: Path):
        # tmp_path has no config.toml
        assert find_notely_root(tmp_path) is None


class TestNotelyConfig:
    def test_loads_spaces(self, config: NotelyConfig):
        assert "clients" in config.spaces
        assert "ideas" in config.spaces

    def test_space_config(self, config: NotelyConfig):
        clients = config.get_space("clients")
        assert clients is not None
        assert clients.group_by == "client"
        assert clients.subgroup_by == "topic"

    def test_space_names(self, config: NotelyConfig):
        names = config.space_names()
        assert "clients" in names
        assert "ideas" in names

    def test_find_ideas_space(self, config: NotelyConfig):
        # "ideas" space has content_status field in DEFAULT_CONFIG
        result = config.find_ideas_space()
        assert result == "ideas"

    def test_ensure_initialized_passes(self, config: NotelyConfig):
        # Should not raise — config.toml exists
        config.ensure_initialized()

    def test_ensure_initialized_fails(self, tmp_path: Path):
        cfg = NotelyConfig(base_dir=tmp_path)
        with pytest.raises(SystemExit):
            cfg.ensure_initialized()

    def test_paths(self, config: NotelyConfig, tmp_workspace: Path):
        assert config.db_path == tmp_workspace / "index.db"
        assert config.notes_dir == tmp_workspace / "notes"
        assert config.raw_dir == tmp_workspace / ".raw"
        assert config.vectors_dir == tmp_workspace / ".vectors"


class TestSpaceDirectories:
    def test_space_dir(self, config: NotelyConfig, tmp_workspace: Path):
        assert config.space_dir("clients") == tmp_workspace / "notes" / "clients"

    def test_group_dir(self, config: NotelyConfig, tmp_workspace: Path):
        assert config.group_dir("clients", "acme") == tmp_workspace / "notes" / "clients" / "acme"

    def test_subgroup_dir(self, config: NotelyConfig, tmp_workspace: Path):
        expected = tmp_workspace / "notes" / "clients" / "acme" / "onboarding"
        assert config.subgroup_dir("clients", "acme", "onboarding") == expected
