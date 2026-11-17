from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


@dataclass
class Settings:
    raw: dict[str, Any]

    @classmethod
    def load(cls, path: str | Path) -> "Settings":
        with open(path, "r", encoding="utf-8") as stream:
            raw = yaml.safe_load(stream) or {}
        return cls(raw=raw)

    def section(self, name: str) -> dict[str, Any]:
        value = self.raw.get(name, {})
        return value if isinstance(value, dict) else {}

    def get(self, dotted: str, default: Any = None) -> Any:
        value: Any = self.raw
        for part in dotted.split("."):
            if not isinstance(value, dict) or part not in value:
                return default
            value = value[part]
        return value
