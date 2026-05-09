from __future__ import annotations

import queue
import csv
import sys
import tempfile
import types
import unittest
from pathlib import Path

from instruments.magnet_coil import MagnetCoilController


def install_qtcore_stub() -> None:
    if "PyQt5.QtCore" in sys.modules:
        return

    class BoundSignal:
        def __init__(self) -> None:
            self._slots = []

        def connect(self, slot) -> None:
            self._slots.append(slot)

        def emit(self, *args) -> None:
            for slot in list(self._slots):
                slot(*args)

    class SignalDescriptor:
        def __set_name__(self, _owner, name: str) -> None:
            self.name = f"__signal_{name}"

        def __get__(self, instance, _owner):
            if instance is None:
                return self
            signal = instance.__dict__.get(self.name)
            if signal is None:
                signal = BoundSignal()
                instance.__dict__[self.name] = signal
            return signal

    class QObject:
        def __init__(self, parent=None) -> None:
            self.parent = parent

    class QThread(QObject):
        started = SignalDescriptor()
        finished = SignalDescriptor()

        def start(self) -> None:
            self.started.emit()

        def quit(self) -> None:
            self.finished.emit()

        def wait(self, _ms: int) -> bool:
            return True

        def deleteLater(self) -> None:
            pass

    qtcore = types.ModuleType("PyQt5.QtCore")
    qtcore.QObject = QObject
    qtcore.QThread = QThread
    qtcore.pyqtSignal = lambda *args, **kwargs: SignalDescriptor()

    pyqt5 = types.ModuleType("PyQt5")
    pyqt5.QtCore = qtcore
    sys.modules["PyQt5"] = pyqt5
    sys.modules["PyQt5.QtCore"] = qtcore


try:
    from PyQt5.QtCore import QObject as _QObject  # noqa: F401
except ImportError:
    install_qtcore_stub()

from core.field_controller import FieldController
from core.sequence_engine import _SequenceRunner
from data.recorder import MagnetFieldRecorder


class FakeSerial:
    def __init__(self, responses: list[bytes] | None = None) -> None:
        self.responses = queue.Queue()
        for response in responses or []:
            self.responses.put(response)
        self.writes: list[bytes] = []
        self.timeout = 0.1
        self.is_open = True
        self.dtr = False

    def write(self, data: bytes) -> int:
        self.writes.append(data)
        return len(data)

    def flush(self) -> None:
        pass

    def readline(self) -> bytes:
        try:
            return self.responses.get_nowait()
        except queue.Empty:
            return b""

    def reset_input_buffer(self) -> None:
        pass

    def reset_output_buffer(self) -> None:
        pass

    def close(self) -> None:
        self.is_open = False


def attach_fake_serial(controller: MagnetCoilController, responses: list[bytes] | None = None) -> FakeSerial:
    fake = FakeSerial(responses)
    controller._serial = fake  # type: ignore[assignment, attr-defined]
    return fake


def drain_axis_queue(field_controller: FieldController, axis: str) -> None:  # type: ignore[valid-type]
    controller = field_controller._controllers[axis]
    command_queue = field_controller._queues[axis]
    while not command_queue.empty():
        cmd, args = command_queue.get_nowait()
        getattr(controller, cmd)(*args)


class MagnetCoilControllerTests(unittest.TestCase):
    def test_set_current_sends_amps_with_lf(self) -> None:
        controller = MagnetCoilController("X")
        fake = attach_fake_serial(controller)

        controller.set_current(100.0)

        self.assertEqual(fake.writes, [b"CURR 0.10000\n"])

    def test_get_current_converts_amps_to_milliamps(self) -> None:
        controller = MagnetCoilController("X")
        attach_fake_serial(controller, [b"0.10000\n"])

        self.assertEqual(controller.get_current(), 100.0)

    def test_basic_command_text_matches_exe_protocol(self) -> None:
        controller = MagnetCoilController("X")
        fake = attach_fake_serial(controller)

        controller.set_voltage(75)
        controller.set_output(True)
        controller.set_output(False)
        controller.set_remote()
        controller.set_local()

        self.assertEqual(
            fake.writes,
            [
                b"VOLT 75\n",
                b"OUTP 1\n",
                b"OUTP 0\n",
                b"SYST:REM\n",
                b"SYST:LOC\n",
            ],
        )

    def test_low_level_current_is_clamped_to_power_max_current(self) -> None:
        controller = MagnetCoilController("X")
        fake = attach_fake_serial(controller)

        controller.set_current(6000.0)

        self.assertEqual(fake.writes, [b"CURR 5.00000\n"])


class FieldControllerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fc = FieldController()
        self.controller = self.fc._controllers["X"]
        self.fake = attach_fake_serial(self.controller)

    def drain(self) -> None:
        drain_axis_queue(self.fc, "X")

    def test_zero_offset_outputs_without_lock_zero(self) -> None:
        self.assertTrue(self.fc.set_zero_offset("X", 10.0))
        self.assertTrue(self.fc.set_current("X", 5.0))
        self.assertTrue(self.fc.set_output("X", True))

        self.drain()

        self.assertEqual(self.fake.writes, [b"CURR 0.01000\n", b"OUTP 1\n"])

    def test_lock_zero_adds_reproduction_current_to_zero_offset(self) -> None:
        self.assertTrue(self.fc.set_zero_offset("X", 10.0))
        self.assertTrue(self.fc.set_current("X", 5.0))
        self.assertTrue(self.fc.set_output("X", True))
        self.drain()
        self.fake.writes.clear()

        self.assertTrue(self.fc.lock_zero("X", True))
        self.drain()

        self.assertEqual(self.fake.writes, [b"CURR 0.01500\n"])

    def test_unlock_zero_returns_to_zero_offset_only(self) -> None:
        self.fc.set_zero_offset("X", 10.0)
        self.fc.set_current("X", 5.0)
        self.fc.set_output("X", True)
        self.drain()
        self.fc.lock_zero("X", True)
        self.drain()
        self.fake.writes.clear()

        self.assertTrue(self.fc.lock_zero("X", False))
        self.drain()

        self.assertEqual(self.fake.writes, [b"CURR 0.01000\n"])

    def test_output_off_sends_current_zero_before_output_off(self) -> None:
        self.fc.set_zero_offset("X", 10.0)
        self.fc.set_output("X", True)
        self.drain()
        self.fake.writes.clear()

        self.assertTrue(self.fc.set_output("X", False))
        self.drain()

        self.assertEqual(self.fake.writes, [b"CURR 0.00000\n", b"OUTP 0\n"])

    def test_negative_current_is_rejected_without_serial_write(self) -> None:
        errors: list[tuple[str, str]] = []
        self.fc.error_occurred.connect(lambda axis, msg: errors.append((axis, msg)))

        self.assertFalse(self.fc.set_current("X", -1.0))
        self.drain()

        self.assertEqual(self.fake.writes, [])
        self.assertEqual(errors[0][0], "X")
        self.assertIn("负电流", errors[0][1])

    def test_current_above_limit_is_rejected_without_serial_write(self) -> None:
        errors: list[tuple[str, str]] = []
        self.fc.error_occurred.connect(lambda axis, msg: errors.append((axis, msg)))

        self.assertFalse(self.fc.set_current("X", 5000.002))
        self.drain()

        self.assertEqual(self.fake.writes, [])
        self.assertIn("超出上限", errors[0][1])

    def test_field_to_current_uses_coil_constant(self) -> None:
        self.fc.set_coil_constant("X", 100.0)

        self.assertTrue(self.fc.set_field("X", 500.0))

        status = self.fc.get_status("X")
        self.assertEqual(status["recur_current_mA"], 5.0)
        self.assertEqual(status["target_field_nT"], 500.0)

    def test_locked_readback_estimates_reproduction_field(self) -> None:
        self.fc.set_coil_constant("X", 100.0)
        self.fc.set_zero_offset("X", 10.0)
        self.fc.set_current("X", 5.0)
        self.fc.set_output("X", True)
        self.drain()
        self.fc.lock_zero("X", True)
        self.drain()

        self.fc._on_current_read("X", 15.0)

        status = self.fc.get_status("X")
        self.assertEqual(status["estimated_recur_current_mA"], 5.0)
        self.assertEqual(status["estimated_field_nT"], 500.0)

    def test_unlocked_readback_does_not_estimate_field_from_zero_offset(self) -> None:
        self.fc.set_coil_constant("X", 100.0)
        self.fc.set_zero_offset("X", 10.0)
        self.fc.set_output("X", True)
        self.drain()

        self.fc._on_current_read("X", 10.0)

        status = self.fc.get_status("X")
        self.assertIsNone(status["estimated_recur_current_mA"])
        self.assertIsNone(status["estimated_field_nT"])

    def test_field_snapshot_marks_estimate_unavailable_until_all_axes_are_estimable(self) -> None:
        self.fc.set_coil_constant("X", 100.0)
        self.fc.set_zero_offset("X", 10.0)
        self.fc.set_current("X", 5.0)
        self.fc.set_output("X", True)
        self.drain()
        self.fc.lock_zero("X", True)
        self.drain()
        self.fc._on_current_read("X", 15.0)

        snapshot = self.fc.get_field_snapshot()

        self.assertFalse(snapshot["estimated_available"])
        self.assertIsNone(snapshot["estimated_field_nT"])
        self.assertEqual(snapshot["axes"]["X"]["estimated_field_nT"], 500.0)

    def test_poll_interval_can_be_updated_and_restarts_connected_axes(self) -> None:
        stopped: list[str] = []
        started: list[tuple[str, int]] = []
        self.controller._serial = self.fake  # type: ignore[attr-defined]

        def fake_stop(axis: str) -> None:
            stopped.append(axis)

        def fake_start(axis: str) -> None:
            started.append((axis, self.fc.get_poll_interval_ms()))

        self.fc._stop_poll = fake_stop  # type: ignore[method-assign]
        self.fc._start_poll = fake_start  # type: ignore[method-assign]

        self.fc.set_poll_interval_ms(1000)

        self.assertEqual(self.fc.get_poll_interval_ms(), 1000)
        self.assertEqual(stopped, ["X"])
        self.assertEqual(started, [("X", 1000)])

    def test_poll_interval_rejects_out_of_range_values(self) -> None:
        with self.assertRaises(ValueError):
            self.fc.set_poll_interval_ms(99)


class RecorderTests(unittest.TestCase):
    def test_recorder_uses_explicit_fields_and_preserves_unavailable_estimates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "record.csv"
            recorder = MagnetFieldRecorder()
            recorder.init_file(str(path))
            recorder.append_row({
                "X_target_field_nT": 500.0,
                "X_total_current_mA": 15.0,
                "X_estimated_recur_current_mA": 5.0,
                "X_estimated_field_nT": 500.0,
                "Y_target_field_nT": 0.0,
                "Y_total_current_mA": 10.0,
                "Y_estimated_recur_current_mA": None,
                "Y_estimated_field_nT": None,
                "Z_target_field_nT": 0.0,
                "Z_total_current_mA": 0.0,
                "Z_estimated_recur_current_mA": None,
                "Z_estimated_field_nT": None,
            })
            recorder.close()

            with path.open(encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))

        self.assertEqual(rows[0]["X_total_current_mA"], "15.00000")
        self.assertEqual(rows[0]["X_estimated_field_nT"], "500.00000")
        self.assertEqual(rows[0]["Y_estimated_field_nT"], "")


class SequenceRecordingTests(unittest.TestCase):
    def test_data_point_uses_snapshot_semantics_instead_of_total_current_times_constant(self) -> None:
        points: list[dict] = []
        snapshot = {
            "axes": {
                "X": {
                    "target_field_nT": 500.0,
                    "total_current_mA": 15.0,
                    "estimated_recur_current_mA": 5.0,
                    "estimated_field_nT": 500.0,
                },
                "Y": {
                    "target_field_nT": 200.0,
                    "total_current_mA": 10.0,
                    "estimated_recur_current_mA": None,
                    "estimated_field_nT": None,
                },
                "Z": {
                    "target_field_nT": 0.0,
                    "total_current_mA": 0.0,
                    "estimated_recur_current_mA": None,
                    "estimated_field_nT": None,
                },
            }
        }
        runner = _SequenceRunner(
            sequence=None,  # type: ignore[arg-type]
            set_field_fn=lambda *_args: True,
            get_connected_fn=lambda: ["X", "Y"],
            get_snapshot_fn=lambda: snapshot,
        )
        runner.data_point.connect(points.append)

        runner._emit_data_point(2, 3)

        self.assertEqual(points[0]["step"], 2)
        self.assertEqual(points[0]["loop"], 3)
        self.assertEqual(points[0]["X_total_current_mA"], 15.0)
        self.assertEqual(points[0]["X_estimated_field_nT"], 500.0)
        self.assertIsNone(points[0]["Y_estimated_field_nT"])
        self.assertIsNone(points[0]["Z_target_field_nT"])


class ConfigTests(unittest.TestCase):
    def test_config_load_defaults_poll_interval_when_missing(self) -> None:
        from app.gui import load_config

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.xml"
            path.write_text(
                """<?xml version='1.0' encoding='utf-8'?>
<Root>
  <Commports><PortX Port="COM3" BaudRate="9600" /></Commports>
  <DataSaveDir>./data</DataSaveDir>
  <CoilConstant X="143.26" Y="141.77" Z="156.15" />
  <ZeroOffset X="0.00000" Y="0.00000" Z="0.00000" />
</Root>
""",
                encoding="utf-8",
            )

            cfg = load_config(path)

        self.assertEqual(cfg["poll_interval_ms"], 500)

    def test_config_save_and_reload_poll_interval(self) -> None:
        from app.gui import load_config, save_config

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.xml"
            cfg = {
                "ports": {"X": "COM3", "Y": "COM4", "Z": "COM5"},
                "baudrate": 9600,
                "coil_constant": {"X": 143.26, "Y": 141.77, "Z": 156.15},
                "zero_offset": {"X": 0.0, "Y": 0.0, "Z": 0.0},
                "data_save_dir": "./data",
                "poll_interval_ms": 1000,
            }

            save_config(cfg, path)
            loaded = load_config(path)

        self.assertEqual(loaded["poll_interval_ms"], 1000)


if __name__ == "__main__":
    unittest.main()
