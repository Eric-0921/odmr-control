from __future__ import annotations

import datetime as _dt
import json
from pathlib import Path
from typing import Any


DEFAULT_PRESET_FILE = Path(__file__).resolve().parents[1] / "presets.json"


class PresetStore:
    def __init__(self, path: Path = DEFAULT_PRESET_FILE) -> None:
        self.path = path

    def load_all(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"version": 1, "presets": []}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"配置文件 JSON 损坏: {exc}") from exc
        if not isinstance(data, dict) or not isinstance(data.get("presets"), list):
            raise ValueError("配置文件格式无效")
        return data

    def names(self) -> list[str]:
        data = self.load_all()
        return [str(item.get("name")) for item in data["presets"] if item.get("name")]

    def get(self, name: str) -> dict[str, Any] | None:
        data = self.load_all()
        for item in data["presets"]:
            if item.get("name") == name:
                return item
        return None

    def save(self, name: str, payload: dict[str, Any]) -> None:
        data = self.load_all()
        presets = [item for item in data["presets"] if item.get("name") != name]
        payload = dict(payload)
        payload["name"] = name
        payload["updated_at"] = _dt.datetime.now().isoformat(timespec="seconds")
        presets.append(payload)
        presets.sort(key=lambda item: str(item.get("name", "")).lower())
        data = {"version": 1, "presets": presets}
        self.path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def delete(self, name: str) -> None:
        data = self.load_all()
        presets = [item for item in data["presets"] if item.get("name") != name]
        self.path.write_text(
            json.dumps({"version": 1, "presets": presets}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

