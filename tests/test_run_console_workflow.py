import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5.QtCore import QObject, pyqtSignal
from PyQt5.QtWidgets import QApplication

from app.gui import ODMRControlGUI
from core.commands import CommandType
from core.instrument_controller import InstrumentController


class FakeCommandService(QObject):
    command_completed = pyqtSignal(str, bool, str, dict)
    command_error = pyqtSignal(str, str)
    smb_state_broadcast = pyqtSignal(dict)
    lockin_data_broadcast = pyqtSignal(dict)
    lockin_status_broadcast = pyqtSignal(dict)
    lockin_batch_broadcast = pyqtSignal(dict)
    laser_state_broadcast = pyqtSignal(dict)
    mag_state_broadcast = pyqtSignal(dict)
    error_occurred = pyqtSignal(str)
    log_requested = pyqtSignal(str)

    def __init__(self):
        super().__init__()
        self._ctrl = InstrumentController()
        self.commands = []

    def submit(self, command):
        self.commands.append(command)
        return command.request_id


class TestRunConsoleWorkflow(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.service = FakeCommandService()
        self.gui = ODMRControlGUI(cmd_service=self.service)

    def tearDown(self):
        self.gui._display_timer.stop()
        self.service._ctrl.mag.disconnect_all()
        self.gui.deleteLater()

    def _complete_last(self, success=True, result=None):
        command = self.service.commands[-1]
        self.gui._on_command_completed(command.request_id, success, "OK" if success else "failed", result or {})

    def test_start_run_queues_load_only_then_chains_validate_preflight_start(self):
        self.gui._exp_start_plan()

        self.assertEqual([cmd.cmd_type for cmd in self.service.commands], [CommandType.EXPERIMENT_LOAD_JSON])

        self._complete_last(result={"valid": True, "errors": [], "steps": 1})
        self.assertEqual(self.service.commands[-1].cmd_type, CommandType.EXPERIMENT_VALIDATE)

        self._complete_last(result={"valid": True, "errors": [], "steps": 1})
        self.assertEqual(self.service.commands[-1].cmd_type, CommandType.EXPERIMENT_PREFLIGHT)

        self._complete_last(result={"ok": True, "checks": []})
        self.assertEqual(self.service.commands[-1].cmd_type, CommandType.EXPERIMENT_START)

    def test_validation_failure_stops_before_preflight(self):
        self.gui._exp_start_plan()
        self._complete_last(result={"valid": True, "errors": [], "steps": 1})
        self._complete_last(result={"valid": False, "errors": ["bad"], "steps": 0})

        self.assertEqual([cmd.cmd_type for cmd in self.service.commands], [
            CommandType.EXPERIMENT_LOAD_JSON,
            CommandType.EXPERIMENT_VALIDATE,
        ])
        self.assertEqual(self.gui._exp_run_workflow, {})

    def test_preflight_failure_stops_before_start(self):
        self.gui._exp_start_plan()
        self._complete_last(result={"valid": True, "errors": [], "steps": 1})
        self._complete_last(result={"valid": True, "errors": [], "steps": 1})
        self._complete_last(result={"ok": False, "checks": [{"name": "output_dir", "status": "error"}]})

        self.assertEqual([cmd.cmd_type for cmd in self.service.commands], [
            CommandType.EXPERIMENT_LOAD_JSON,
            CommandType.EXPERIMENT_VALIDATE,
            CommandType.EXPERIMENT_PREFLIGHT,
        ])
        self.assertEqual(self.gui._exp_run_workflow, {})

    def test_query_state_updates_run_state_panel(self):
        self.gui._submit_experiment_command(CommandType.EXPERIMENT_QUERY_STATE, {}, "experiment_query_state")
        self._complete_last(result={
            "run_id": "run_123",
            "run_dir": "/tmp/does-not-exist",
            "running": True,
            "paused": False,
            "step_index": 2,
            "loop_index": 1,
            "step_name": "odmr_0003",
            "leased_resources": ["microwave", "lockin"],
            "error": "",
        })

        self.assertEqual(self.gui._exp_run_state_labels["run_id"].text(), "run_123")
        self.assertEqual(self.gui._exp_run_state_labels["status"].text(), "running")
        self.assertIn("odmr_0003", self.gui._exp_run_state_labels["step"].text())
        self.assertIn("microwave", self.gui._exp_run_state_labels["leases"].text())
        self.assertFalse(self.gui._exp_open_run_folder_btn.isEnabled())

    def test_preflight_result_updates_operator_panel(self):
        result = {
            "ok": False,
            "checks": [
                {"name": "output_dir", "status": "ok", "message": "./experiments"},
                {"name": "device_lockin", "status": "warning", "message": "not connected"},
                {"name": "safety_envelope", "status": "error", "message": "power limit"},
                {"name": "return_to_zero", "status": "skipped", "message": "not requested"},
            ],
        }

        self.gui._exp_update_preflight_report(result)

        self.assertIn("BLOCKED", self.gui._exp_preflight_status.text())
        text = self.gui._exp_preflight_report.toPlainText()
        self.assertIn("OK", text)
        self.assertIn("WARNING", text)
        self.assertIn("ERROR", text)
        self.assertIn("SKIPPED", text)

    def test_compile_updates_loaded_plan_and_validation_panel(self):
        self.gui._exp_compile_draft_preview()

        self.assertEqual(self.gui._exp_plan_summary_labels["name"].text(), "nv_experiment")
        self.assertNotEqual(self.gui._exp_plan_summary_labels["hash"].text(), "--")
        self.assertIn("VALID", self.gui._exp_validation_status.text())


if __name__ == "__main__":
    unittest.main()
