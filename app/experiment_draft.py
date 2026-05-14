"""Mutable experiment draft model used by the GUI before plan compilation."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Literal, Optional, Sequence, Union


ProcedureKind = Literal["odmr_sweep", "bxyz_grid", "b_theta_phi", "custom_steps"]
SweepMode = Literal["device", "software_step"]


@dataclass
class DraftMetadata:
    name: str = "nv_experiment"
    domain: str = "diamond_nv_odmr"
    mode: str = "odmr_sweep"
    description: str = ""
    operator: str = ""


@dataclass
class SequenceOptions:
    loop_count: int = 1
    loop_delay_s: float = 0.0
    return_to_zero: bool = False
    infinite_loop_confirmed: bool = False


@dataclass
class RecordingDefaults:
    output_dir: str = "./experiments"
    enabled: bool = True
    format: str = "csv"
    column_groups: Optional[List[str]] = None


@dataclass
class SafetySelection:
    profile_name: str = "default"
    on_error: str = "rf_off_mag_off_stop_recording"
    microwave_max_power_dbm_with_amp: float = 10.0
    microwave_max_power_dbm_no_amp: float = 25.0
    amplifier_installed: bool = True
    laser_max_power_mw: float = 150.0
    magnetic_max_abs_nT: float = 10_000_000.0
    min_dwell_time_constant_factor: float = 5.0


@dataclass
class DeviceRequirements:
    microwave: bool = False
    lockin: bool = True
    laser: bool = False
    magnetic_field: bool = False
    lockin_channel: int = 1


@dataclass
class TimingDefaults:
    settle_s: float = 0.5
    hold_s: float = 1.0
    dwell_ms: float = 50.0
    lockin_time_constant_s: Optional[float] = None


@dataclass
class AcquisitionDefaults:
    start_trigger: str = "step_start"
    stop_trigger: str = "hold_elapsed"
    start_recording: bool = True
    stop_recording: bool = True
    stop_acquire: bool = False


@dataclass
class BxyzGridDraft:
    kind: Literal["bxyz_grid"] = "bxyz_grid"
    x_start_nT: float = 0.0
    x_stop_nT: float = 0.0
    x_step_nT: float = 1000.0
    y_start_nT: float = 0.0
    y_stop_nT: float = 0.0
    y_step_nT: float = 1000.0
    z_start_nT: float = 0.0
    z_stop_nT: float = 0.0
    z_step_nT: float = 1000.0


@dataclass
class BThetaPhiDraft:
    kind: Literal["b_theta_phi"] = "b_theta_phi"
    b_start_nT: float = 0.0
    b_stop_nT: float = 10_000.0
    b_step_nT: float = 1000.0
    theta_start_deg: float = 90.0
    theta_stop_deg: float = 90.0
    theta_step_deg: float = 0.0
    phi_start_deg: float = 0.0
    phi_stop_deg: float = 0.0
    phi_step_deg: float = 0.0


@dataclass
class ODMRSweepDraft:
    kind: Literal["odmr_sweep"] = "odmr_sweep"
    start_hz: float = 2.82e9
    stop_hz: float = 2.92e9
    step_hz: float = 1e6
    power_dbm: float = -30.0
    dwell_ms: float = 50.0
    rf_output: bool = True
    execute: bool = True
    sweep_mode: SweepMode = "device"
    field_points: Optional[Union[BxyzGridDraft, BThetaPhiDraft]] = None


@dataclass
class CustomStepsDraft:
    kind: Literal["custom_steps"] = "custom_steps"
    steps: List[Dict[str, Any]] = field(default_factory=list)


ProcedureDraft = Union[ODMRSweepDraft, BxyzGridDraft, BThetaPhiDraft, CustomStepsDraft]


@dataclass
class ExperimentPlanDraft:
    metadata: DraftMetadata = field(default_factory=DraftMetadata)
    procedure: ProcedureDraft = field(default_factory=ODMRSweepDraft)
    sequence: SequenceOptions = field(default_factory=SequenceOptions)
    recording: RecordingDefaults = field(default_factory=RecordingDefaults)
    safety: SafetySelection = field(default_factory=SafetySelection)
    devices: DeviceRequirements = field(default_factory=DeviceRequirements)
    timing: TimingDefaults = field(default_factory=TimingDefaults)
    acquisition: AcquisitionDefaults = field(default_factory=AcquisitionDefaults)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def float_series(start: float, stop: float, step: float, *, max_points: int = 10000) -> List[float]:
    """Return an inclusive numeric series with direction inferred from start/stop."""
    if abs(stop - start) < 1e-12:
        return [start]
    if abs(step) < 1e-12:
        return [start]
    actual_step = abs(step) if stop >= start else -abs(step)
    values: List[float] = []
    value = start
    guard = 0
    while (value <= stop + 1e-9 if actual_step > 0 else value >= stop - 1e-9) and guard < max_points:
        values.append(value)
        value += actual_step
        guard += 1
    return values or [start]


def grid_points(grid: Union[BxyzGridDraft, BThetaPhiDraft]) -> List[Dict[str, float]]:
    if isinstance(grid, BxyzGridDraft):
        xs = float_series(grid.x_start_nT, grid.x_stop_nT, grid.x_step_nT)
        ys = float_series(grid.y_start_nT, grid.y_stop_nT, grid.y_step_nT)
        zs = float_series(grid.z_start_nT, grid.z_stop_nT, grid.z_step_nT)
        return [
            {"x_nT": x, "y_nT": y, "z_nT": z}
            for x in xs
            for y in ys
            for z in zs
        ]
    bs = float_series(grid.b_start_nT, grid.b_stop_nT, grid.b_step_nT)
    thetas = float_series(grid.theta_start_deg, grid.theta_stop_deg, grid.theta_step_deg)
    phis = float_series(grid.phi_start_deg, grid.phi_stop_deg, grid.phi_step_deg)
    return [
        {"magnitude_nT": b, "theta_deg": theta, "phi_deg": phi}
        for b in bs
        for theta in thetas
        for phi in phis
    ]


def custom_steps_draft(steps: Sequence[Dict[str, Any]], *, name: str = "custom_experiment") -> ExperimentPlanDraft:
    draft = ExperimentPlanDraft()
    draft.metadata.name = name
    draft.metadata.mode = "custom_steps"
    draft.procedure = CustomStepsDraft(steps=[dict(step) for step in steps])
    return draft
