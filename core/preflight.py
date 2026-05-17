"""Preflight checks for compiled experiment plans."""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List

from core.safety_policy import SafetyEnvelope, SafetyPolicy


@dataclass
class PreflightCheck:
    name: str
    status: str
    message: str = ""


@dataclass
class PreflightReport:
    ok: bool
    checks: List[PreflightCheck] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "checks": [asdict(check) for check in self.checks],
        }


class ExperimentPreflight:
    def __init__(self, controller: Any, config: Dict[str, Any] | None = None) -> None:
        self._ctrl = controller
        self._config = config or {}

    def run(self, plan: Dict[str, Any]) -> PreflightReport:
        checks: List[PreflightCheck] = []
        sequence = plan.get("sequence", {})
        steps = sequence.get("steps", []) if isinstance(sequence, dict) else []
        metadata = plan.get("metadata", {}) if isinstance(plan.get("metadata"), dict) else {}
        recording_defaults = metadata.get("recording_defaults", {}) if isinstance(metadata.get("recording_defaults"), dict) else {}
        output_root = recording_defaults.get("output_dir") or self._config.get("acquisition", {}).get("save_dir", "./experiments")

        self._check_output_dir(output_root, checks)
        self._check_required_devices(steps, checks)
        self._check_safety_envelope(steps, checks)
        self._check_return_to_zero(sequence, checks)

        return PreflightReport(ok=all(check.status != "error" for check in checks), checks=checks)

    def _check_output_dir(self, output_root: str, checks: List[PreflightCheck]) -> None:
        try:
            path = Path(output_root)
            path.mkdir(parents=True, exist_ok=True)
            if os.access(path, os.W_OK):
                checks.append(PreflightCheck("output_dir", "ok", str(path)))
            else:
                checks.append(PreflightCheck("output_dir", "error", f"Output directory is not writable: {path}"))
        except Exception as exc:
            checks.append(PreflightCheck("output_dir", "error", str(exc)))

    def _check_required_devices(self, steps: List[Dict[str, Any]], checks: List[PreflightCheck]) -> None:
        needs_mw = any("microwave" in step for step in steps)
        needs_lockin = any("lockin" in step or "acquisition" in step for step in steps)
        needs_mag = any("magnetic_field" in step for step in steps)
        needs_laser = any("laser" in step for step in steps)

        device_checks = [
            ("microwave", needs_mw, getattr(self._ctrl, "is_smb_connected", False)),
            ("lockin", needs_lockin, getattr(self._ctrl, "is_lockin_connected", False)),
            ("magnetic_field", needs_mag, getattr(self._ctrl, "is_mag_connected", False)),
            ("laser", needs_laser, getattr(self._ctrl, "is_laser_connected", False)),
        ]
        for name, needed, connected in device_checks:
            if not needed:
                checks.append(PreflightCheck(f"device_{name}", "skipped", "not required"))
            elif connected:
                checks.append(PreflightCheck(f"device_{name}", "ok", "connected"))
            else:
                checks.append(PreflightCheck(f"device_{name}", "warning", "not connected in current controller state"))

    def _check_safety_envelope(self, steps: List[Dict[str, Any]], checks: List[PreflightCheck]) -> None:
        smb_cfg = self._config.get("smb", {})
        laser_cfg = self._config.get("laser", {})
        policy = SafetyPolicy(SafetyEnvelope(
            amplifier_installed=smb_cfg.get("amplifier_installed", True),
            laser_max_power_mw=laser_cfg.get("max_power_mw", 150),
        ))
        for idx, step in enumerate(steps):
            microwave = step.get("microwave", {})
            if "power_dbm" in microwave:
                for message in policy.check_microwave_power(float(microwave["power_dbm"])):
                    checks.append(PreflightCheck(f"step_{idx + 1}_microwave_power", message.severity, message.message))
            laser = step.get("laser", {})
            if "power_mw" in laser:
                for message in policy.check_laser_power(float(laser["power_mw"])):
                    checks.append(PreflightCheck(f"step_{idx + 1}_laser_power", message.severity, message.message))
        checks.append(PreflightCheck("safety_envelope", "ok", "basic safety envelope checked"))

    def _check_return_to_zero(self, sequence: Dict[str, Any], checks: List[PreflightCheck]) -> None:
        if sequence.get("return_to_zero"):
            checks.append(PreflightCheck("return_to_zero", "ok", "requested"))
        else:
            checks.append(PreflightCheck("return_to_zero", "skipped", "not requested"))
