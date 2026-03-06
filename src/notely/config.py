"""Configuration loading and path management."""

from __future__ import annotations

import os
try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib  # type: ignore[no-redef]
from pathlib import Path
from typing import Any

# Sentinel file that marks a directory as a notely root
CONFIG_FILENAME = "config.toml"


def load_env(base_dir: Path) -> None:
    """Load .env file from a notely directory if it exists."""
    env_path = base_dir / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip()
            # Don't overwrite existing env vars (explicit export takes priority)
            if key not in os.environ:
                os.environ[key] = value


def find_notely_root(start: Path | None = None) -> Path | None:
    """Walk up from `start` (default: cwd) looking for a config.toml.

    Works like git — the first parent directory containing config.toml
    is the notely root. Returns None if nothing is found.
    """
    current = (start or Path.cwd()).resolve()
    for directory in [current, *current.parents]:
        if (directory / CONFIG_FILENAME).exists():
            return directory
    return None

DEFAULT_CONFIG = """\
# Notely configuration
# user_name = "Your Name"

[spaces.clients]
display_name = "Client Work"
description = "Meeting notes, Slack threads, project work for client engagements"
group_by = "client"
subgroup_by = "topic"
fields = ["participants", "action_items"]

[spaces.ideas]
display_name = "Ideas & Content"
description = "Personal ideas, podcast notes, article reactions for content creation"
group_by = "category"
fields = ["content_status", "source_ref", "source_url"]
"""


class SpaceConfig:
    """Configuration for a single space."""

    def __init__(self, name: str, data: dict[str, Any]) -> None:
        self.name = name
        self.display_name: str = data.get("display_name", name.title())
        self.description: str = data.get("description", "")
        self.group_by: str = data.get("group_by", "group")
        self.subgroup_by: str | None = data.get("subgroup_by")
        self.fields: list[str] = data.get("fields", [])


class NotelyConfig:
    """Full application configuration."""

    def __init__(self, base_dir: Path | None = None) -> None:
        if base_dir is not None:
            self.base_dir = base_dir.resolve()
        else:
            # Auto-discover: walk up from cwd looking for config.toml
            found = find_notely_root()
            self.base_dir = found if found else Path.cwd().resolve()
        self.config_path = self.base_dir / "config.toml"
        self.db_path = self.base_dir / "index.db"
        self.notes_dir = self.base_dir / "notes"
        self.raw_dir = self.base_dir / ".raw"
        self.vectors_dir = self.base_dir / ".vectors"
        self.secrets_path = self.base_dir / ".secrets.toml"
        self.references_path = self.base_dir / "references.toml"
        self.spaces: dict[str, SpaceConfig] = {}
        self.user_name: str | None = None
        self._raw: dict[str, Any] = {}

        # Load .env (API keys etc.) before anything else
        load_env(self.base_dir)

        if self.config_path.exists():
            self._load()

    def _load(self) -> None:
        text = self.config_path.read_text()
        self._raw = tomllib.loads(text)
        self.user_name = self._raw.get("user_name")
        spaces_data = self._raw.get("spaces", {})
        for name, data in spaces_data.items():
            self.spaces[name] = SpaceConfig(name, data)

    def space_dir(self, space: str) -> Path:
        return self.notes_dir / space

    def group_dir(self, space: str, group: str) -> Path:
        return self.space_dir(space) / group

    def subgroup_dir(self, space: str, group: str, subgroup: str) -> Path:
        return self.group_dir(space, group) / subgroup

    def get_space(self, name: str) -> SpaceConfig | None:
        return self.spaces.get(name)

    def space_names(self) -> list[str]:
        return list(self.spaces.keys())

    def find_ideas_space(self) -> str | None:
        """Find the space configured for ideas/content (has content_status field)."""
        for name, space in self.spaces.items():
            if "content_status" in space.fields:
                return name
        for name in ("ideas", "content", "thoughts"):
            if name in self.spaces:
                return name
        return None

    def ensure_initialized(self) -> None:
        """Exit with error if workspace is not initialized."""
        if not self.config_path.exists():
            from rich.console import Console
            Console().print("[red]Not initialized. Run 'notely init' first.[/red]")
            raise SystemExit(1)
