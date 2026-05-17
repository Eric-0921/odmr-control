"""Run manifest helpers for immutable experiment execution records."""

from __future__ import annotations

import datetime as _dt
import json
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict


@dataclass
class RunManifest:
    run_id: str
    plan_hash: str
    draft_hash: str = ""
    schema_version: str = "experiment_plan_v1"
    software_version: str = ""
    git_commit: str = ""
    device_idn: Dict[str, Any] = field(default_factory=dict)
    start_time: str = field(default_factory=lambda: _dt.datetime.now(_dt.UTC).isoformat())
    operator: str = ""
    safety_profile: str = "default"
    status: str = "created"

    @classmethod
    def create(cls, plan_hash: str, *, draft_hash: str = "", name: str = "run") -> "RunManifest":
        timestamp = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_name = "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in name).strip("_") or "run"
        return cls(run_id=f"{timestamp}_{safe_name}_{uuid.uuid4().hex[:6]}", plan_hash=plan_hash, draft_hash=draft_hash)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def write(self, output_dir: str | Path) -> Path:
        path = Path(output_dir) / "run_manifest.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        return path
