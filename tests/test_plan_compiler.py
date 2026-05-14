import unittest

from PyQt5.QtCore import QCoreApplication

from app.experiment_draft import (
    AcquisitionDefaults,
    BThetaPhiDraft,
    BxyzGridDraft,
    DeviceRequirements,
    DraftMetadata,
    ExperimentPlanDraft,
    ODMRSweepDraft,
    RecordingDefaults,
    SafetySelection,
    SequenceOptions,
    TimingDefaults,
)
from app.plan_compiler import PlanCompiler
from core.command_service import CommandService
from core.commands import Command, CommandType
from core.instrument_controller import InstrumentController


class TestPlanCompiler(unittest.TestCase):
    def setUp(self):
        self.compiler = PlanCompiler()

    def _validate_with_command_service(self, plan):
        QCoreApplication.instance() or QCoreApplication([])
        ctrl = InstrumentController()
        service = CommandService(ctrl, config={})
        try:
            return service._execute(Command(CommandType.EXPERIMENT_VALIDATE, {"plan": plan}))
        finally:
            service._experiment_stop.set()
            if service._experiment_thread is not None:
                service._experiment_thread.join(timeout=1.0)
            ctrl.mag.disconnect_all()

    def _odmr_draft(self) -> ExperimentPlanDraft:
        return ExperimentPlanDraft(
            metadata=DraftMetadata(name="compiler_odmr", mode="odmr_sweep"),
            procedure=ODMRSweepDraft(
                start_hz=2.82e9,
                stop_hz=2.92e9,
                step_hz=1e6,
                power_dbm=-30.0,
                dwell_ms=50.0,
                field_points=BxyzGridDraft(
                    x_start_nT=0.0,
                    x_stop_nT=0.0,
                    y_start_nT=0.0,
                    y_stop_nT=0.0,
                    z_start_nT=0.0,
                    z_stop_nT=0.0,
                ),
            ),
            sequence=SequenceOptions(loop_count=1, loop_delay_s=0.2, return_to_zero=True),
            recording=RecordingDefaults(output_dir="./experiments", enabled=True, column_groups=["CH-A", "System"]),
            devices=DeviceRequirements(microwave=True, lockin=True, magnetic_field=True, lockin_channel=1),
            acquisition=AcquisitionDefaults(
                start_trigger="microwave_sweep_start",
                stop_trigger="microwave_sweep_complete",
            ),
        )

    def test_odmr_template_compiles_schema_v1_plan(self):
        compiled = self.compiler.compile(self._odmr_draft())

        self.assertTrue(compiled.valid, compiled.report())
        self.assertNotIn("recording", compiled.plan)
        self.assertEqual(set(compiled.plan.keys()), {"metadata", "sequence"})
        self.assertEqual(compiled.plan["metadata"]["recording_defaults"]["output_dir"], "./experiments")
        self.assertEqual(compiled.plan["sequence"]["loop_delay"], 0.2)
        self.assertTrue(compiled.plan["sequence"]["return_to_zero"])

        step = compiled.plan["sequence"]["steps"][0]
        self.assertIn("sweep", step["microwave"])
        self.assertEqual(step["acquisition"]["column_groups"], ["CH-A", "System"])

        validation = self._validate_with_command_service(compiled.plan)
        self.assertTrue(validation["valid"], validation["errors"])

    def test_bxyz_template_only_generates_cartesian_field(self):
        draft = ExperimentPlanDraft(
            metadata=DraftMetadata(name="bxyz", mode="bxyz_grid"),
            procedure=BxyzGridDraft(
                x_start_nT=0.0,
                x_stop_nT=1000.0,
                x_step_nT=1000.0,
                y_start_nT=0.0,
                y_stop_nT=0.0,
                z_start_nT=0.0,
                z_stop_nT=0.0,
            ),
            acquisition=AcquisitionDefaults(start_trigger="magnetic_settled", stop_trigger="hold_elapsed"),
        )

        compiled = self.compiler.compile(draft)

        self.assertTrue(compiled.valid, compiled.report())
        self.assertEqual(len(compiled.plan["sequence"]["steps"]), 2)
        for step in compiled.plan["sequence"]["steps"]:
            field = step["magnetic_field"]
            self.assertEqual(set(field.keys()), {"x_nT", "y_nT", "z_nT"})
            self.assertNotIn("microwave", step)

    def test_btheta_phi_template_only_generates_spherical_field(self):
        draft = ExperimentPlanDraft(
            metadata=DraftMetadata(name="vector", mode="b_theta_phi"),
            procedure=BThetaPhiDraft(
                b_start_nT=0.0,
                b_stop_nT=1000.0,
                b_step_nT=1000.0,
                theta_start_deg=90.0,
                theta_stop_deg=90.0,
                phi_start_deg=0.0,
                phi_stop_deg=0.0,
            ),
            acquisition=AcquisitionDefaults(start_trigger="magnetic_settled", stop_trigger="hold_elapsed"),
        )

        compiled = self.compiler.compile(draft)

        self.assertTrue(compiled.valid, compiled.report())
        self.assertEqual(len(compiled.plan["sequence"]["steps"]), 2)
        for step in compiled.plan["sequence"]["steps"]:
            field = step["magnetic_field"]
            self.assertEqual(set(field.keys()), {"magnitude_nT", "theta_deg", "phi_deg"})
            self.assertNotIn("microwave", step)

    def test_semantic_errors_for_invalid_odmr_sweep(self):
        draft = self._odmr_draft()
        draft.procedure.start_hz = 2.92e9
        draft.procedure.stop_hz = 2.82e9
        draft.procedure.step_hz = 0.0
        draft.procedure.power_dbm = 20.0
        draft.safety = SafetySelection(amplifier_installed=True, microwave_max_power_dbm_with_amp=10.0)

        compiled = self.compiler.compile(draft)
        codes = {message.code for message in compiled.errors}

        self.assertFalse(compiled.valid)
        self.assertIn("invalid_frequency_range", codes)
        self.assertIn("invalid_step_hz", codes)
        self.assertIn("microwave_power_limit", codes)

    def test_dwell_time_constant_warning(self):
        draft = self._odmr_draft()
        draft.timing = TimingDefaults(lockin_time_constant_s=0.1)
        draft.procedure.dwell_ms = 50.0

        compiled = self.compiler.compile(draft)
        codes = {message.code for message in compiled.warnings}

        self.assertTrue(compiled.valid, compiled.report())
        self.assertIn("dwell_vs_time_constant", codes)

    def test_loop_count_zero_requires_confirmation(self):
        draft = self._odmr_draft()
        draft.sequence.loop_count = 0
        draft.sequence.infinite_loop_confirmed = False

        compiled = self.compiler.compile(draft)
        self.assertFalse(compiled.valid)
        self.assertIn("infinite_loop_unconfirmed", {message.code for message in compiled.errors})

        draft.sequence.infinite_loop_confirmed = True
        confirmed = self.compiler.compile(draft)
        self.assertTrue(confirmed.valid, confirmed.report())

    def test_plan_hash_stable(self):
        draft = self._odmr_draft()

        first = self.compiler.compile(draft)
        second = self.compiler.compile(draft)

        self.assertEqual(first.plan_hash, second.plan_hash)
        self.assertEqual(first.draft_hash, second.draft_hash)


if __name__ == "__main__":
    unittest.main()
