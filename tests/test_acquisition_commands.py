"""Acquisition command routing tests."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import numpy as np

from core.command_service import CommandService
from core.commands import Command, CommandType
from core.instrument_controller import InstrumentController
from instruments.oe1022d import RALL_PARAMS, SAMPLES_PER_BATCH


class TestAcquisitionCommands(unittest.TestCase):
    def setUp(self):
        self.ctrl = InstrumentController()
        self.service = CommandService(self.ctrl, config={})
        self.started_recorder = None
        self.stop_called = False
        self.detach_called = False

        def start_lockin_acquire(recorder=None):
            self.started_recorder = recorder
            self.ctrl._acquiring = True

        def stop_lockin_acquire():
            self.stop_called = True
            if self.started_recorder is not None:
                self.started_recorder.stop_recording()
            self.ctrl._acquiring = False

        def detach_lockin_recorder():
            self.detach_called = True
            if self.started_recorder is not None:
                self.started_recorder.stop_recording()
            self.ctrl._acquiring = True

        self.ctrl.start_lockin_acquire = start_lockin_acquire
        self.ctrl.stop_lockin_acquire = stop_lockin_acquire
        self.ctrl.detach_lockin_recorder = detach_lockin_recorder

    def test_start_recording_creates_recorder_and_output_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = self.service._execute(Command(
                CommandType.ACQ_START_RECORDING,
                {"output_dir": tmp},
            ))

            self.assertTrue(result["recording"])
            self.assertEqual(result["output_dir"], tmp)
            self.assertIsNotNone(self.started_recorder)
            self.assertTrue((Path(tmp) / "data.csv").exists())
            self.assertTrue((Path(tmp) / "columns.json").exists())

            batch = {
                name: np.full(SAMPLES_PER_BATCH, 0.001)
                for name, *_ in RALL_PARAMS
            }
            self.started_recorder.write_batch(
                batch,
                smb_freq_hz=2.87e9,
                smb_power_dbm=-30.0,
                smb_rf_on=True,
                laser_power_mw=50.0,
                laser_on=True,
            )
            self.started_recorder.stop_recording()
            lines = (Path(tmp) / "data.csv").read_text(encoding="utf-8-sig").splitlines()
            self.assertIn("当前日期:", lines[0])
            self.assertIn("CH-A", lines[1])
            self.assertIn("MagneticField", lines[1])
            self.assertIn("ADC1", lines[2])
            self.assertIn("X_target_field_nT", lines[2])
            self.assertGreaterEqual(len(lines), 4)

    def test_stop_recording_can_detach_without_stopping_acquire(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.service._execute(Command(
                CommandType.ACQ_START_RECORDING,
                {"output_dir": tmp},
            ))
            result = self.service._execute(Command(
                CommandType.ACQ_STOP_RECORDING,
                {"stop_acquire": False},
            ))

            self.assertFalse(result["recording"])
            self.assertTrue(self.detach_called)
            self.assertFalse(self.stop_called)
            self.assertTrue((Path(tmp) / "metadata.json").exists())


if __name__ == "__main__":
    unittest.main()
