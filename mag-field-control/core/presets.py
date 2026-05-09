"""Preset manager for saving/loading common magnetic field configurations."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import List, Optional

PRESETS_DIR = Path(__file__).resolve().parent.parent / "presets"


@dataclass
class FieldPreset:
    """A named magnetic field configuration."""

    name: str
    description: str = ""
    x_nT: float = 0.0
    y_nT: float = 0.0
    z_nT: float = 0.0
    # Optional spherical representation
    magnitude_nT: Optional[float] = None
    theta_deg: Optional[float] = None
    phi_deg: Optional[float] = None


BUILTIN_PRESETS: List[FieldPreset] = [
    FieldPreset("零场", "所有轴归零", 0, 0, 0),
    FieldPreset(
        "地磁补偿 (北京)",
        "抵消北京地区地磁场 (~50μT 水平分量)",
        -20000.0,
        -45000.0,
        -10000.0,
    ),
    FieldPreset(
        "沿 X 轴 10μT",
        "X 方向 10 微特斯拉",
        10000.0,
        0.0,
        0.0,
        magnitude_nT=10000.0,
        theta_deg=90.0,
        phi_deg=0.0,
    ),
    FieldPreset(
        "沿 Y 轴 10μT",
        "Y 方向 10 微特斯拉",
        0.0,
        10000.0,
        0.0,
        magnitude_nT=10000.0,
        theta_deg=90.0,
        phi_deg=90.0,
    ),
    FieldPreset(
        "沿 Z 轴 10μT",
        "Z 方向 10 微特斯拉",
        0.0,
        0.0,
        10000.0,
        magnitude_nT=10000.0,
        theta_deg=0.0,
        phi_deg=0.0,
    ),
]


class PresetManager:
    """Manage field presets stored as individual JSON files."""

    def __init__(self, directory: Path = PRESETS_DIR) -> None:
        self._dir = directory
        self._dir.mkdir(parents=True, exist_ok=True)

    @property
    def directory(self) -> Path:
        return self._dir

    def list_presets(self) -> List[FieldPreset]:
        """Return all presets (built-in + user-saved)."""
        presets = list(BUILTIN_PRESETS)
        for p in sorted(self._dir.glob("*.json")):
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                presets.append(FieldPreset(**data))
            except Exception:
                continue
        return presets

    def list_names(self) -> List[str]:
        """Return names of all available presets."""
        return [p.name for p in self.list_presets()]

    def get_preset(self, name: str) -> Optional[FieldPreset]:
        """Look up a preset by name."""
        for p in self.list_presets():
            if p.name == name:
                return p
        return None

    def save_preset(self, preset: FieldPreset) -> None:
        """Save a user preset to disk (overwrites if name exists)."""
        safe_name = preset.name.replace("/", "_").replace("\\", "_")
        path = self._dir / f"{safe_name}.json"
        path.write_text(
            json.dumps(asdict(preset), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    def delete_preset(self, name: str) -> bool:
        """Delete a user preset. Returns True if deleted, False if not found or built-in."""
        for bp in BUILTIN_PRESETS:
            if bp.name == name:
                return False  # cannot delete built-in
        safe_name = name.replace("/", "_").replace("\\", "_")
        path = self._dir / f"{safe_name}.json"
        if path.exists():
            path.unlink()
            return True
        return False
