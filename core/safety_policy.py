"""Experiment safety policy checks shared by compiler, preflight, and runtime."""

from __future__ import annotations

from dataclasses import dataclass
from typing import List


@dataclass
class SafetyMessage:
    severity: str
    code: str
    message: str


@dataclass
class SafetyEnvelope:
    amplifier_installed: bool = True
    microwave_max_power_dbm_with_amp: float = 10.0
    microwave_max_power_dbm_no_amp: float = 25.0
    laser_max_power_mw: float = 150.0
    magnetic_max_abs_nT: float = 10_000_000.0
    min_dwell_time_constant_factor: float = 5.0

    @property
    def microwave_max_power_dbm(self) -> float:
        if self.amplifier_installed:
            return self.microwave_max_power_dbm_with_amp
        return self.microwave_max_power_dbm_no_amp


class SafetyPolicy:
    def __init__(self, envelope: SafetyEnvelope | None = None) -> None:
        self.envelope = envelope or SafetyEnvelope()

    def check_microwave_power(self, power_dbm: float) -> List[SafetyMessage]:
        max_dbm = self.envelope.microwave_max_power_dbm
        if power_dbm > max_dbm:
            return [SafetyMessage("error", "microwave_power_limit", f"Microwave power {power_dbm} dBm exceeds safety limit {max_dbm} dBm")]
        return []

    def check_laser_power(self, power_mw: float) -> List[SafetyMessage]:
        if power_mw < 0 or power_mw > self.envelope.laser_max_power_mw:
            return [SafetyMessage("error", "laser_power_limit", f"Laser power {power_mw} mW is outside [0, {self.envelope.laser_max_power_mw}] mW")]
        return []

    def check_magnetic_field(self, *fields_nT: float) -> List[SafetyMessage]:
        limit = self.envelope.magnetic_max_abs_nT
        if any(abs(value) > limit for value in fields_nT):
            return [SafetyMessage("error", "magnetic_field_limit", f"Magnetic field exceeds configured limit {limit} nT")]
        return []

    def check_dwell_time_constant(self, dwell_ms: float, time_constant_s: float | None) -> List[SafetyMessage]:
        if time_constant_s is None or time_constant_s <= 0:
            return []
        required_ms = time_constant_s * self.envelope.min_dwell_time_constant_factor * 1000.0
        if dwell_ms < required_ms:
            return [SafetyMessage("warning", "dwell_vs_time_constant", f"Dwell {dwell_ms:g} ms is below recommended {required_ms:g} ms for lock-in time constant {time_constant_s:g} s")]
        return []
