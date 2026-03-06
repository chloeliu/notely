"""Secrets storage — auto-captures |||secret||| values into .secrets.toml."""

from __future__ import annotations

import re
from pathlib import Path


class SecretsStore:
    """Read/write secrets in a TOML file with [service] sections."""

    def __init__(self, secrets_path: Path) -> None:
        self._path = secrets_path

    def store(self, service: str, key: str, value: str) -> None:
        """Store a single secret under [service].key."""
        data = self._read()
        if service not in data:
            data[service] = {}
        data[service][key] = value
        self._write(data)

    def store_mapping(self, mapping: dict[str, str], service_hint: str = "auto") -> None:
        """Store a {placeholder: value} mapping from mask_secrets().

        Keys like [REDACTED_1] are normalized to redacted_1.
        """
        if not mapping:
            return
        data = self._read()
        if service_hint not in data:
            data[service_hint] = {}
        for placeholder, value in mapping.items():
            key = placeholder.strip("[]").lower().replace(" ", "_")
            data[service_hint][key] = value
        self._write(data)

    def get(self, service: str) -> dict[str, str] | None:
        """Get all key-value pairs for a service."""
        data = self._read()
        return data.get(service)

    def list_services(self) -> list[str]:
        """List all service names."""
        return list(self._read().keys())

    def get_all(self) -> dict[str, dict[str, str]]:
        """Get the full secrets structure."""
        return self._read()

    def _read(self) -> dict[str, dict[str, str]]:
        """Parse .secrets.toml into nested dict."""
        if not self._path.exists():
            return {}
        text = self._path.read_text(encoding="utf-8")
        return _parse_toml(text)

    def _write(self, data: dict[str, dict[str, str]]) -> None:
        """Write nested dict as TOML."""
        if self._path.name == ".secrets.toml":
            header = "# Notely secrets — auto-generated, do not commit"
        else:
            header = f"# Notely {self._path.stem} — auto-generated"
        lines = [header, ""]
        for service, kvs in sorted(data.items()):
            lines.append(f"[{service}]")
            for key, value in sorted(kvs.items()):
                escaped = value.replace("\\", "\\\\").replace('"', '\\"')
                lines.append(f'{key} = "{escaped}"')
            lines.append("")
        self._path.write_text("\n".join(lines), encoding="utf-8")


def _parse_toml(text: str) -> dict[str, dict[str, str]]:
    """Minimal TOML parser for flat [section] + key = "value" format."""
    data: dict[str, dict[str, str]] = {}
    current_section: str | None = None

    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue

        # Section header
        section_match = re.match(r"^\[([^\]]+)\]$", line)
        if section_match:
            current_section = section_match.group(1)
            if current_section not in data:
                data[current_section] = {}
            continue

        # Key = value
        if current_section and "=" in line:
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip()
            # Strip quotes
            if value.startswith('"') and value.endswith('"'):
                value = value[1:-1]
                value = value.replace('\\"', '"').replace("\\\\", "\\")
            data[current_section][key] = value

    return data
