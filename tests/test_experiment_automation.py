import time
import unittest

from core.command_service import CommandService
from core.commands import Command, CommandType
from core.instrument_controller import InstrumentController


class TestExperimentAutomation(unittest.TestCase):
    def setUp(self):
        self.ctrl = InstrumentController()
        self.service = CommandService(self.ctrl, config={})

    def tearDown(self):
        self.service._experiment_stop.set()
        if self.service._experiment_thread is not None:
            self.service._experiment_thread.join(timeout=1.0)
        self.ctrl.mag.disconnect_all()

    def test_validate_rejects_empty_sequence(self):
        result = self.service._execute(Command(
            CommandType.EXPERIMENT_VALIDATE,
            {"plan": {"metadata": {}, "sequence": {"steps": []}}},
        ))

        self.assertFalse(result["valid"])
        self.assertIn("sequence.steps", result["errors"][0])

    def test_validate_rejects_invalid_acquisition_trigger(self):
        result = self.service._execute(Command(
            CommandType.EXPERIMENT_VALIDATE,
            {"plan": {"sequence": {"steps": [{
                "name": "bad",
                "acquisition": {"start_trigger": "unknown", "stop_trigger": "hold_elapsed"},
            }]}}},
        ))

        self.assertFalse(result["valid"])
        self.assertIn("start_trigger", result["errors"][0])

    def test_smb_sweep_config_can_execute_sweep(self):
        calls = []
        self.ctrl.smb.set_power = lambda power: calls.append(("power", power))
        self.ctrl.smb.configure_sweep = lambda *args: calls.append(("configure_sweep", args))
        self.ctrl.smb.set_sweep_shape = lambda shape: calls.append(("shape", shape))
        self.ctrl.smb.set_sweep_retrace = lambda retrace: calls.append(("retrace", retrace))
        self.ctrl.smb.set_sweep_trigger_source = lambda source: calls.append(("trigger", source))
        self.ctrl.smb.set_output = lambda on: calls.append(("output", on))
        self.ctrl.smb.start_sweep = lambda: calls.append(("start_sweep", None))
        self.ctrl.smb.query_config = lambda: {"ok": True}

        result = self.service._execute(Command(CommandType.SMB_APPLY_CONFIG, {
            "power_dbm": -30,
            "sweep": {
                "start_hz": 2.82e9,
                "stop_hz": 2.92e9,
                "step_hz": 1e6,
                "dwell_ms": 50,
                "shape": "SAWTOOTH",
                "retrace": True,
                "trigger": "IMM",
                "execute": True,
            },
        }))

        self.assertTrue(result["ok"])
        self.assertIn(("start_sweep", None), calls)
        self.assertIn(("output", True), calls)

    def test_load_and_run_minimal_experiment_plan(self):
        plan = {
            "metadata": {"name": "unit"},
            "sequence": {
                "loop_count": 1,
                "steps": [
                    {
                        "name": "zero",
                        "magnetic_field": {"x_nT": 0, "y_nT": 0, "z_nT": 0},
                        "timing": {"settle_s": 0.0, "hold_s": 0.0},
                    }
                ],
            },
        }

        loaded = self.service._execute(Command(CommandType.EXPERIMENT_LOAD_JSON, {"plan": plan}))
        self.assertTrue(loaded["valid"])

        state = self.service._execute(Command(CommandType.EXPERIMENT_START))
        self.assertIn("running", state)
        time.sleep(0.2)
        state = self.service._execute(Command(CommandType.EXPERIMENT_QUERY_STATE))
        self.assertFalse(state["running"])
        self.assertEqual(state["step_index"], 0)


if __name__ == "__main__":
    unittest.main()
