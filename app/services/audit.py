from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class AuditLogger:
    def __init__(self, path: str, operator_env: str = "HPC_MANAGER_OPERATOR") -> None:
        self.path = Path(path)
        self.operator_env = operator_env

    def record(self, action: str, target: str, details: dict[str, Any] | None = None) -> None:
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "operator": os.getenv(self.operator_env, "unknown"),
            "action": action,
            "target": target,
            "details": details or {},
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(entry, sort_keys=True)
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(line + "\n")
        # Also emit to stdout so Kubernetes log collection can retain audit events.
        print(line, flush=True)
