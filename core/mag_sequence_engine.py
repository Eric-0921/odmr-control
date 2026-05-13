from __future__ import annotations

import csv
import datetime
import json
import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable, Dict, List, Optional

from PyQt5.QtCore import QObject, QThread, pyqtSignal


RECORD_FIELDS = [
    "timestamp",
    "step",
    "loop",
    "X_target_field_nT",
    "X_total_current_mA",
    "X_estimated_recur_current_mA",
    "X_estimated_field_nT",
    "Y_target_field_nT",
    "Y_total_current_mA",
    "Y_estimated_recur_current_mA",
    "Y_estimated_field_nT",
    "Z_target_field_nT",
    "Z_total_current_mA",
    "Z_estimated_recur_current_mA",
    "Z_estimated_field_nT",
]


class TriggerType(str, Enum):
    IMMEDIATE = "immediate"
    DELAY = "delay"
    MANUAL = "manual"


@dataclass
class FieldStep:
    """One step in a magnetic field sequence."""
    name: str = ""
    field_x_nT: float = 0.0
    field_y_nT: float = 0.0
    field_z_nT: float = 0.0
    hold_seconds: float = 0.0       # 0 = hold indefinitely (until manual advance)
    settle_seconds: float = 0.5     # wait for field to stabilize after setting
    trigger: str = "immediate"       # TriggerType value
    delay_seconds: float = 0.0


@dataclass
class FieldSequence:
    """An ordered list of field steps."""
    name: str = "Untitled"
    steps: List[FieldStep] = field(default_factory=list)
    loop_count: int = 1             # 0 = infinite loop
    loop_delay: float = 0.0
    return_to_zero: bool = True     # set field to zero when sequence ends


class _SequenceRunner(QObject):
    """Worker that executes a FieldSequence in a background thread."""

    step_started = pyqtSignal(int, str)      # (index, step_name)
    step_completed = pyqtSignal(int, str)
    sequence_finished = pyqtSignal(str)      # sequence_name
    paused = pyqtSignal()
    resumed = pyqtSignal()
    error = pyqtSignal(str)
    progress = pyqtSignal(int, int, int)     # (loop_num, step_index, total_steps)
    data_point = pyqtSignal(dict)

    def __init__(
        self,
        sequence: FieldSequence,
        set_field_fn: Callable,
        get_connected_fn: Callable,
        get_snapshot_fn: Callable,
        set_field_3d_zero_fn: Optional[Callable] = None,
        settle_seconds: float = 0.5,
    ) -> None:
        super().__init__()
        self._seq = sequence
        self._set_field = set_field_fn
        self._get_connected = get_connected_fn
        self._get_snapshot = get_snapshot_fn
        self._set_field_3d_zero = set_field_3d_zero_fn
        self._default_settle = settle_seconds
        self._stop = False
        self._paused = False
        self._advance = False

    def run(self) -> None:
        try:
            total_loops = self._seq.loop_count if self._seq.loop_count > 0 else float("inf")
            loop_num = 0
            while loop_num < total_loops and not self._stop:
                for idx, step in enumerate(self._seq.steps):
                    if self._stop:
                        break
                    self._wait_unpaused()
                    if self._stop:
                        break

                    self.step_started.emit(idx, step.name)
                    self.progress.emit(loop_num, idx, len(self._seq.steps))

                    # trigger logic
                    if step.trigger == TriggerType.DELAY and step.delay_seconds > 0:
                        self._interruptible_sleep(step.delay_seconds)
                    elif step.trigger == TriggerType.MANUAL:
                        self._advance = False
                        while not self._advance and not self._stop:
                            self._wait_unpaused()
                            time.sleep(0.1)

                    if self._stop:
                        break

                    # apply field values
                    connected = self._get_connected()
                    for axis, nT in zip(
                        ("X", "Y", "Z"),
                        (step.field_x_nT, step.field_y_nT, step.field_z_nT),
                    ):
                        if axis in connected:
                            self._set_field(axis, nT)

                    # wait for field to settle
                    settle = step.settle_seconds if step.settle_seconds > 0 else self._default_settle
                    self._interruptible_sleep(settle)

                    # record data point after settling
                    self._emit_data_point(idx, loop_num)

                    # hold
                    if step.hold_seconds > 0:
                        self._interruptible_sleep(step.hold_seconds)

                    # record data point at end of hold
                    self._emit_data_point(idx, loop_num)

                    self.step_completed.emit(idx, step.name)

                loop_num += 1
                if loop_num < total_loops and not self._stop and self._seq.loop_delay > 0:
                    self._interruptible_sleep(self._seq.loop_delay)

        except Exception as exc:
            self.error.emit(str(exc))
        finally:
            self.sequence_finished.emit(self._seq.name)

    def stop(self) -> None:
        self._stop = True

    def pause(self) -> None:
        self._paused = True
        self.paused.emit()

    def resume(self) -> None:
        self._paused = False
        self.resumed.emit()

    def advance(self) -> None:
        self._advance = True

    def _wait_unpaused(self) -> None:
        while self._paused and not self._stop:
            time.sleep(0.1)

    def _interruptible_sleep(self, seconds: float) -> None:
        elapsed = 0.0
        while elapsed < seconds and not self._stop:
            self._wait_unpaused()
            step = min(0.1, seconds - elapsed)
            time.sleep(step)
            elapsed += step

    def _emit_data_point(self, step_idx: int, loop_num: int) -> None:
        connected = self._get_connected()
        point: Dict[str, object] = {
            "timestamp": datetime.datetime.now().isoformat(timespec="milliseconds"),
            "step": step_idx,
            "loop": loop_num,
        }
        for axis in ("X", "Y", "Z"):
            point[f"{axis}_target_field_nT"] = None
            point[f"{axis}_total_current_mA"] = None
            point[f"{axis}_estimated_recur_current_mA"] = None
            point[f"{axis}_estimated_field_nT"] = None
        try:
            snapshot = self._get_snapshot()
            axes = snapshot.get("axes", {})
            for axis in ("X", "Y", "Z"):
                if axis not in connected:
                    continue
                status = axes.get(axis, {})
                point[f"{axis}_target_field_nT"] = self._round_optional(status.get("target_field_nT"), 2)
                point[f"{axis}_total_current_mA"] = self._round_optional(status.get("total_current_mA"), 5)
                point[f"{axis}_estimated_recur_current_mA"] = self._round_optional(
                    status.get("estimated_recur_current_mA"), 5
                )
                point[f"{axis}_estimated_field_nT"] = self._round_optional(status.get("estimated_field_nT"), 2)
        except Exception:
            pass
        self.data_point.emit(point)

    @staticmethod
    def _round_optional(value: object, digits: int) -> Optional[float]:
        if value is None:
            return None
        return round(float(value), digits)


class SequenceEngine(QObject):
    """High-level API for loading, running, and controlling field sequences."""

    step_started = pyqtSignal(int, str)
    step_completed = pyqtSignal(int, str)
    sequence_finished = pyqtSignal(str)
    sequence_paused = pyqtSignal()
    sequence_resumed = pyqtSignal()
    error_occurred = pyqtSignal(str)
    progress = pyqtSignal(int, int, int)
    data_point = pyqtSignal(dict)

    def __init__(self, field_controller, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._fc = field_controller
        self._sequence: Optional[FieldSequence] = None
        self._runner: Optional[_SequenceRunner] = None
        self._thread: Optional[QThread] = None
        self._record_file = None
        self._record_writer = None

    @property
    def is_running(self) -> bool:
        return self._runner is not None

    @property
    def is_paused(self) -> bool:
        return self._runner is not None and self._runner._paused

    @property
    def current_sequence(self) -> Optional[FieldSequence]:
        return self._sequence

    def load_sequence(self, seq: FieldSequence) -> None:
        if self.is_running:
            raise RuntimeError("Cannot load while running")
        self._sequence = seq

    def start(self, record_path: Optional[str] = None) -> None:
        if self._sequence is None:
            raise RuntimeError("No sequence loaded")
        if self.is_running:
            return
        # setup data recording
        if record_path:
            self._record_file = open(record_path, "w", newline="", encoding="utf-8")
            self._record_writer = csv.writer(self._record_file)
            self._record_writer.writerow(RECORD_FIELDS)
        runner = _SequenceRunner(
            self._sequence,
            set_field_fn=self._fc.set_field,
            get_connected_fn=self._fc.connected_axes,
            get_snapshot_fn=self._fc.get_field_snapshot,
            set_field_3d_zero_fn=lambda: self._fc.set_field_3d(0, 0, 0),
        )
        thread = QThread(self)
        runner.moveToThread(thread)
        thread.started.connect(runner.run)
        runner.step_started.connect(self.step_started)
        runner.step_completed.connect(self.step_completed)
        runner.sequence_finished.connect(self._on_finished)
        runner.error.connect(self.error_occurred)
        runner.progress.connect(self.progress)
        runner.paused.connect(self.sequence_paused)
        runner.resumed.connect(self.sequence_resumed)
        runner.data_point.connect(self._on_data_point)
        runner.sequence_finished.connect(thread.quit)
        runner.sequence_finished.connect(runner.deleteLater)
        thread.finished.connect(thread.deleteLater)
        self._runner = runner
        self._thread = thread
        thread.start()

    def pause(self) -> None:
        if self._runner is not None:
            self._runner.pause()

    def resume(self) -> None:
        if self._runner is not None:
            self._runner.resume()

    def stop(self) -> None:
        if self._runner is not None:
            self._runner.stop()

    def advance(self) -> None:
        if self._runner is not None:
            self._runner.advance()

    def _on_data_point(self, point: dict) -> None:
        self.data_point.emit(point)
        if self._record_writer:
            self._record_writer.writerow([
                self._format_record_value(point.get(field))
                for field in RECORD_FIELDS
            ])
            if self._record_file:
                self._record_file.flush()

    @staticmethod
    def _format_record_value(value: object) -> str:
        if value is None:
            return ""
        return str(value)

    def _on_finished(self, name: str) -> None:
        # return to zero if configured
        if self._sequence and self._sequence.return_to_zero:
            try:
                self._fc.set_field_3d(0, 0, 0)
            except Exception:
                pass
        # close record file
        if self._record_file:
            try:
                self._record_file.close()
            except Exception:
                pass
            self._record_file = None
            self._record_writer = None
        self._runner = None
        self._thread = None
        self.sequence_finished.emit(name)

    # -- persistence ---------------------------------------------------------

    @staticmethod
    def save_sequence(seq: FieldSequence, path: str) -> None:
        data = {
            "name": seq.name,
            "loop_count": seq.loop_count,
            "loop_delay": seq.loop_delay,
            "return_to_zero": seq.return_to_zero,
            "steps": [asdict(s) for s in seq.steps],
        }
        Path(path).write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

    @staticmethod
    def load_sequence_file(path: str) -> FieldSequence:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        steps = [FieldStep(**s) for s in data.get("steps", [])]
        return FieldSequence(
            name=data.get("name", "Untitled"),
            steps=steps,
            loop_count=data.get("loop_count", 1),
            loop_delay=data.get("loop_delay", 0.0),
            return_to_zero=data.get("return_to_zero", True),
        )
