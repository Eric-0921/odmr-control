"""Compile GUI experiment drafts into schema-compatible experiment plans."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Tuple

from app.experiment_draft import (
    BThetaPhiDraft,
    BxyzGridDraft,
    CustomStepsDraft,
    ExperimentPlanDraft,
    ODMRSweepDraft,
    float_series,
    grid_points,
)
from core.safety_policy import SafetyEnvelope, SafetyMessage, SafetyPolicy


@dataclass
class ValidationMessage:
    severity: str
    code: str
    message: str
    path: str = ""


@dataclass
class CompiledPlan:
    plan: Dict[str, Any]
    warnings: List[ValidationMessage]
    errors: List[ValidationMessage]
    plan_hash: str
    draft_hash: str

    @property
    def valid(self) -> bool:
        return not self.errors

    def report(self) -> Dict[str, Any]:
        return {
            "valid": self.valid,
            "plan_hash": self.plan_hash,
            "draft_hash": self.draft_hash,
            "warnings": [message.__dict__ for message in self.warnings],
            "errors": [message.__dict__ for message in self.errors],
        }


class PlanCompiler:
    def compile(self, draft: ExperimentPlanDraft) -> CompiledPlan:
        warnings: List[ValidationMessage] = []
        errors: List[ValidationMessage] = []

        policy = SafetyPolicy(SafetyEnvelope(
            amplifier_installed=draft.safety.amplifier_installed,
            microwave_max_power_dbm_with_amp=draft.safety.microwave_max_power_dbm_with_amp,
            microwave_max_power_dbm_no_amp=draft.safety.microwave_max_power_dbm_no_amp,
            laser_max_power_mw=draft.safety.laser_max_power_mw,
            magnetic_max_abs_nT=draft.safety.magnetic_max_abs_nT,
            min_dwell_time_constant_factor=draft.safety.min_dwell_time_constant_factor,
        ))

        if draft.sequence.loop_count < 0:
            errors.append(ValidationMessage("error", "loop_count_negative", "sequence.loop_count must be >= 0", "sequence.loop_count"))
        if draft.sequence.loop_count == 0 and not draft.sequence.infinite_loop_confirmed:
            errors.append(ValidationMessage("error", "infinite_loop_unconfirmed", "loop_count=0 requires explicit confirmation", "sequence.loop_count"))

        steps = self._compile_steps(draft, policy, warnings, errors)
        metadata = {
            "name": draft.metadata.name,
            "domain": draft.metadata.domain,
            "mode": draft.metadata.mode,
            "description": draft.metadata.description,
            "generated_by": "odmr-control-plan-compiler",
            "schema_target": "experiment_plan_v1",
            "draft_type": getattr(draft.procedure, "kind", "unknown"),
            "recording_defaults": {
                "enabled": draft.recording.enabled,
                "output_dir": draft.recording.output_dir,
                "format": draft.recording.format,
                "column_groups": draft.recording.column_groups,
            },
            "safety_profile": draft.safety.profile_name,
            "device_requirements": {
                "microwave": draft.devices.microwave,
                "lockin": draft.devices.lockin,
                "laser": draft.devices.laser,
                "magnetic_field": draft.devices.magnetic_field,
                "lockin_channel": draft.devices.lockin_channel,
            },
        }
        if draft.metadata.operator:
            metadata["operator"] = draft.metadata.operator

        plan = {
            "metadata": metadata,
            "sequence": {
                "loop_count": draft.sequence.loop_count,
                "loop_delay": draft.sequence.loop_delay_s,
                "return_to_zero": draft.sequence.return_to_zero,
                "steps": steps,
            },
        }

        if not steps:
            errors.append(ValidationMessage("error", "empty_sequence", "Compiled plan has no steps", "sequence.steps"))

        plan_hash = stable_hash(plan)
        draft_hash = stable_hash(draft.to_dict())
        return CompiledPlan(plan=plan, warnings=warnings, errors=errors, plan_hash=plan_hash, draft_hash=draft_hash)

    def _compile_steps(
        self,
        draft: ExperimentPlanDraft,
        policy: SafetyPolicy,
        warnings: List[ValidationMessage],
        errors: List[ValidationMessage],
    ) -> List[Dict[str, Any]]:
        procedure = draft.procedure
        if isinstance(procedure, ODMRSweepDraft):
            return self._compile_odmr(draft, procedure, policy, warnings, errors)
        if isinstance(procedure, BxyzGridDraft):
            return self._compile_field_grid(draft, grid_points(procedure), "bxyz", policy, warnings, errors)
        if isinstance(procedure, BThetaPhiDraft):
            return self._compile_field_grid(draft, grid_points(procedure), "vector", policy, warnings, errors)
        if isinstance(procedure, CustomStepsDraft):
            return [dict(step) for step in procedure.steps]
        errors.append(ValidationMessage("error", "unknown_procedure", f"Unsupported procedure {type(procedure).__name__}", "procedure"))
        return []

    def _compile_odmr(
        self,
        draft: ExperimentPlanDraft,
        procedure: ODMRSweepDraft,
        policy: SafetyPolicy,
        warnings: List[ValidationMessage],
        errors: List[ValidationMessage],
    ) -> List[Dict[str, Any]]:
        if procedure.start_hz >= procedure.stop_hz:
            errors.append(ValidationMessage("error", "invalid_frequency_range", "start_hz must be smaller than stop_hz", "procedure.start_hz"))
        if procedure.step_hz <= 0:
            errors.append(ValidationMessage("error", "invalid_step_hz", "step_hz must be > 0", "procedure.step_hz"))
        errors.extend(to_validation(policy.check_microwave_power(procedure.power_dbm), "procedure.power_dbm"))
        warnings.extend(to_validation(policy.check_dwell_time_constant(procedure.dwell_ms, draft.timing.lockin_time_constant_s), "procedure.dwell_ms"))

        if procedure.sweep_mode == "device":
            if draft.acquisition.start_trigger not in {"microwave_sweep_start", "setpoints_applied", "step_start", "disabled"}:
                errors.append(ValidationMessage("error", "trigger_incompatible_with_device_sweep", "Device sweep requires acquisition start trigger compatible with microwave sweep", "acquisition.start_trigger"))
            if draft.acquisition.stop_trigger not in {"microwave_sweep_complete", "hold_elapsed", "timed", "step_end", "disabled"}:
                errors.append(ValidationMessage("error", "trigger_incompatible_with_device_sweep", "Device sweep requires acquisition stop trigger compatible with microwave sweep", "acquisition.stop_trigger"))

        fields = grid_points(procedure.field_points) if procedure.field_points is not None else [{"x_nT": 0.0, "y_nT": 0.0, "z_nT": 0.0}]
        if procedure.sweep_mode == "software_step":
            freqs = float_series(procedure.start_hz, procedure.stop_hz, procedure.step_hz)
            steps = []
            index = 0
            for field in fields:
                self._check_field(policy, field, errors)
                for freq_hz in freqs:
                    index += 1
                    steps.append(self._base_step(draft, f"odmr_sw_{index:04d}", field, {
                        "frequency_hz": freq_hz,
                        "power_dbm": procedure.power_dbm,
                        "rf_output": procedure.rf_output,
                    }))
            return steps

        steps = []
        for index, field in enumerate(fields, start=1):
            self._check_field(policy, field, errors)
            steps.append(self._base_step(draft, f"odmr_{index:04d}", field, {
                "power_dbm": procedure.power_dbm,
                "rf_output": procedure.rf_output,
                "sweep": {
                    "start_hz": procedure.start_hz,
                    "stop_hz": procedure.stop_hz,
                    "step_hz": procedure.step_hz,
                    "dwell_ms": procedure.dwell_ms,
                    "shape": "SAWTOOTH",
                    "retrace": False,
                    "trigger": "IMM",
                    "execute": procedure.execute,
                },
            }))
        return steps

    def _compile_field_grid(
        self,
        draft: ExperimentPlanDraft,
        fields: List[Dict[str, float]],
        prefix: str,
        policy: SafetyPolicy,
        warnings: List[ValidationMessage],
        errors: List[ValidationMessage],
    ) -> List[Dict[str, Any]]:
        steps = []
        for index, field in enumerate(fields, start=1):
            self._check_field(policy, field, errors)
            steps.append(self._base_step(draft, f"{prefix}_{index:04d}", field, None))
        return steps

    def _base_step(
        self,
        draft: ExperimentPlanDraft,
        name: str,
        field: Dict[str, float],
        microwave: Dict[str, Any] | None,
    ) -> Dict[str, Any]:
        step: Dict[str, Any] = {
            "name": name,
            "magnetic_field": dict(field),
            "timing": {
                "settle_s": draft.timing.settle_s,
                "hold_s": draft.timing.hold_s,
                "trigger": "immediate",
            },
            "lockin": {"channel": draft.devices.lockin_channel},
            "acquisition": {
                "start_trigger": draft.acquisition.start_trigger,
                "stop_trigger": draft.acquisition.stop_trigger,
                "start_recording": draft.recording.enabled and draft.acquisition.start_recording,
                "stop_recording": draft.recording.enabled and draft.acquisition.stop_recording,
                "output_dir": str(Path(draft.recording.output_dir) / name),
                "stop_acquire": draft.acquisition.stop_acquire,
            },
        }
        if draft.recording.column_groups:
            step["acquisition"]["column_groups"] = list(draft.recording.column_groups)
        if microwave is not None:
            step["microwave"] = microwave
        return step

    @staticmethod
    def _check_field(policy: SafetyPolicy, field: Dict[str, float], errors: List[ValidationMessage]) -> None:
        if all(axis in field for axis in ("x_nT", "y_nT", "z_nT")):
            errors.extend(to_validation(policy.check_magnetic_field(field["x_nT"], field["y_nT"], field["z_nT"]), "magnetic_field"))
        elif "magnitude_nT" in field:
            errors.extend(to_validation(policy.check_magnetic_field(field["magnitude_nT"]), "magnetic_field.magnitude_nT"))


def to_validation(messages: List[SafetyMessage], path: str) -> List[ValidationMessage]:
    return [ValidationMessage(message.severity, message.code, message.message, path) for message in messages]


def stable_hash(data: Dict[str, Any]) -> str:
    encoded = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
