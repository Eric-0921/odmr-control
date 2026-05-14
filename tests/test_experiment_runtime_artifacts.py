import json
import tempfile
import time
import unittest
from pathlib import Path

from PyQt5.QtCore import QCoreApplication

from core.command_service import CommandService, SafetyError
from core.commands import Command, CommandType
from core.instrument_controller import InstrumentController


class TestExperimentRuntimeArtifacts(unittest.TestCase):
    def setUp(self):
        self._app = QCoreApplication.instance() or QCoreApplication([])
        self.ctrl = InstrumentController()
        self.service = CommandService(self.ctrl, config={})

    def tearDown(self):
        self.service._experiment_stop.set()
        if self.service._experiment_thread is not None:
            self.service._experiment_thread.join(timeout=1.0)
        self.ctrl.mag.disconnect_all()

    def test_start_run_writes_manifest_plan_preflight_and_events(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            plan = {
                "metadata": {
                    "name": "artifact_smoke",
                    "recording_defaults": {"output_dir": tmpdir},
                },
                "sequence": {
                    "steps": [
                        {"name": "wait_once", "type": "wait", "duration_s": 0.01}
                    ]
                },
            }

            state = self.service._execute(Command(CommandType.EXPERIMENT_START, {"plan": plan}))
            run_dir = Path(state["run_dir"])
            deadline = time.time() + 2.0
            while self.service._experiment_state.get("running") and time.time() < deadline:
                time.sleep(0.02)
            if self.service._experiment_thread is not None:
                self.service._experiment_thread.join(timeout=1.0)

            self.assertTrue((run_dir / "compiled_plan.json").exists())
            self.assertTrue((run_dir / "draft_snapshot.json").exists())
            self.assertTrue((run_dir / "compile_report.json").exists())
            self.assertTrue((run_dir / "preflight_report.json").exists())
            self.assertTrue((run_dir / "events.jsonl").exists())

            manifest = json.loads((run_dir / "run_manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["status"], "completed")
            self.assertIn("plan_hash", manifest)
            events = (run_dir / "events.jsonl").read_text(encoding="utf-8")
            self.assertIn("run_started", events)
            self.assertIn("run_finished", events)

    def test_manual_dangerous_command_blocked_by_run_lease(self):
        self.service._resource_manager.acquire_for_run("run_under_test", ["microwave"])

        with self.assertRaises(SafetyError):
            self.service._enforce_resource_lease(Command(
                CommandType.SMB_SET_POWER,
                {"power_dbm": -30},
                source="gui",
            ))

        self.service._enforce_resource_lease(Command(
            CommandType.SMB_SET_POWER,
            {"power_dbm": -30},
            source="experiment",
        ))
        self.service._resource_manager.release("run_under_test")

    def test_resource_conflict_marks_manifest_failed(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            self.service._resource_manager.acquire_for_run("other_run", ["lockin"])
            plan = {
                "metadata": {
                    "name": "lease_conflict",
                    "recording_defaults": {"output_dir": tmpdir},
                },
                "sequence": {
                    "steps": [
                        {
                            "name": "recording_step",
                            "timing": {"settle_s": 0.0, "hold_s": 0.0},
                            "acquisition": {"start_recording": False, "stop_recording": False},
                        }
                    ]
                },
            }

            with self.assertRaises(RuntimeError):
                self.service._execute(Command(CommandType.EXPERIMENT_START, {"plan": plan}))

            run_dirs = [path for path in Path(tmpdir).iterdir() if path.is_dir()]
            self.assertEqual(len(run_dirs), 1)
            manifest = json.loads((run_dirs[0] / "run_manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["status"], "failed")
            events = (run_dirs[0] / "events.jsonl").read_text(encoding="utf-8")
            self.assertIn("Resources already leased", events)
            self.service._resource_manager.release("other_run")


if __name__ == "__main__":
    unittest.main()
