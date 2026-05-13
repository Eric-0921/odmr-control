"""Acquisition command routing tests."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from core.command_service import CommandService
from core.commands import Command, CommandType
from core.instrument_controller import InstrumentController


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
            self.assertTrue((Path(tmp) / "data.parquet").exists())
            self.assertTrue((Path(tmp) / "columns.json").exists())

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
