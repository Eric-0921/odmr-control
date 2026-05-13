"""实验自动化测试

覆盖范围：
- _validate_experiment_plan 验证逻辑（触发器类型、空步骤、多步骤、嵌套循环）
- _load_experiment_plan 加载逻辑（文件、参数、无效格式）
- 实验生命周期（启动、暂停、恢复、停止）
"""

import json
import os
import tempfile
import time
import unittest

from PyQt5.QtCore import QCoreApplication

from core.command_service import CommandService, SafetyError
from core.commands import Command, CommandType
from core.instrument_controller import InstrumentController


class TestExperimentPlanValidation(unittest.TestCase):
    """_validate_experiment_plan 边界条件与错误情况。"""

    def setUp(self):
        self._app = QCoreApplication.instance() or QCoreApplication([])
        self.ctrl = InstrumentController()
        self.service = CommandService(self.ctrl, config={})

    def tearDown(self):
        self.service._experiment_stop.set()
        if self.service._experiment_thread is not None:
            self.service._experiment_thread.join(timeout=1.0)
        self.ctrl.mag.disconnect_all()

    # -- 空步骤列表 -----------------------------------------------------------

    def test_validate_rejects_empty_sequence(self):
        result = self.service._execute(Command(
            CommandType.EXPERIMENT_VALIDATE,
            {"plan": {"metadata": {}, "sequence": {"steps": []}}},
        ))

        self.assertFalse(result["valid"])
        self.assertIn("sequence.steps", result["errors"][0])

    def test_validate_rejects_missing_sequence_key(self):
        """plan 中无 sequence 键时应报错。"""
        result = self.service._execute(Command(
            CommandType.EXPERIMENT_VALIDATE,
            {"plan": {"metadata": {}}},
        ))

        self.assertFalse(result["valid"])
        self.assertIn("sequence.steps", result["errors"][0])

    def test_validate_rejects_none_steps(self):
        """sequence.steps 为 None 时应报错。"""
        result = self.service._execute(Command(
            CommandType.EXPERIMENT_VALIDATE,
            {"plan": {"sequence": {"steps": None}}},
        ))

        self.assertFalse(result["valid"])

    # -- 无效触发器类型 -------------------------------------------------------

    def test_validate_rejects_invalid_start_trigger(self):
        result = self.service._execute(Command(
            CommandType.EXPERIMENT_VALIDATE,
            {"plan": {"sequence": {"steps": [{
                "name": "bad_start",
                "acquisition": {"start_trigger": "unknown_trigger", "stop_trigger": "hold_elapsed"},
            }]}}},
        ))

        self.assertFalse(result["valid"])
        self.assertTrue(any("start_trigger" in e for e in result["errors"]))

    def test_validate_rejects_invalid_stop_trigger(self):
        result = self.service._execute(Command(
            CommandType.EXPERIMENT_VALIDATE,
            {"plan": {"sequence": {"steps": [{
                "name": "bad_stop",
                "acquisition": {"start_trigger": "step_start", "stop_trigger": "nonexistent"},
            }]}}},
        ))

        self.assertFalse(result["valid"])
        self.assertTrue(any("stop_trigger" in e for e in result["errors"]))

    def test_validate_rejects_both_invalid_triggers(self):
        """同时包含无效 start_trigger 和 stop_trigger 时，应报告两个错误。"""
        result = self.service._execute(Command(
            CommandType.EXPERIMENT_VALIDATE,
            {"plan": {"sequence": {"steps": [{
                "name": "double_bad",
                "acquisition": {"start_trigger": "bad1", "stop_trigger": "bad2"},
            }]}}},
        ))

        self.assertFalse(result["valid"])
        self.assertEqual(len(result["errors"]), 2)

    def test_validate_accepts_all_valid_start_triggers(self):
        """所有合法的 start_trigger 值都应通过验证。"""
        valid_start_triggers = [
            "step_start", "setpoints_applied", "magnetic_settled",
            "microwave_output_on", "microwave_sweep_start", "disabled",
        ]
        for trigger in valid_start_triggers:
            with self.subTest(trigger=trigger):
                result = self.service._execute(Command(
                    CommandType.EXPERIMENT_VALIDATE,
                    {"plan": {"sequence": {"steps": [{
                        "name": f"test_{trigger}",
                        "acquisition": {"start_trigger": trigger, "stop_trigger": "hold_elapsed"},
                    }]}}},
                ))
                self.assertTrue(result["valid"], f"start_trigger='{trigger}' should be valid")

    def test_validate_accepts_all_valid_stop_triggers(self):
        """所有合法的 stop_trigger 值都应通过验证。"""
        valid_stop_triggers = [
            "hold_elapsed", "timed", "microwave_sweep_complete",
            "manual", "step_end", "disabled",
        ]
        for trigger in valid_stop_triggers:
            with self.subTest(trigger=trigger):
                result = self.service._execute(Command(
                    CommandType.EXPERIMENT_VALIDATE,
                    {"plan": {"sequence": {"steps": [{
                        "name": f"test_{trigger}",
                        "acquisition": {"start_trigger": "step_start", "stop_trigger": trigger},
                    }]}}},
                ))
                self.assertTrue(result["valid"], f"stop_trigger='{trigger}' should be valid")

    # -- 非 dict 步骤 ---------------------------------------------------------

    def test_validate_rejects_non_dict_step(self):
        """步骤列表中包含非 dict 元素时应报错。"""
        result = self.service._execute(Command(
            CommandType.EXPERIMENT_VALIDATE,
            {"plan": {"sequence": {"steps": ["not_a_dict", 123]}}},
        ))

        self.assertFalse(result["valid"])
        self.assertTrue(any("必须是对象" in e for e in result["errors"]))

    # -- 多步骤计划 -----------------------------------------------------------

    def test_validate_multi_step_plan(self):
        """多步骤计划应正确返回步骤数。"""
        plan = {
            "sequence": {
                "steps": [
                    {"name": "step1", "magnetic_field": {"x_nT": 0, "y_nT": 0, "z_nT": 0}},
                    {"name": "step2", "magnetic_field": {"x_nT": 100, "y_nT": 0, "z_nT": 0}},
                    {"name": "step3", "magnetic_field": {"x_nT": 0, "y_nT": 100, "z_nT": 0}},
                ]
            }
        }

        result = self.service._execute(Command(
            CommandType.EXPERIMENT_VALIDATE,
            {"plan": plan},
        ))

        self.assertTrue(result["valid"])
        self.assertEqual(result["steps"], 3)

    def test_validate_step_with_magnetic_field_spherical(self):
        """球坐标磁场定义应通过验证。"""
        plan = {
            "sequence": {
                "steps": [{
                    "name": "spherical",
                    "magnetic_field": {"magnitude_nT": 1000, "theta_deg": 45, "phi_deg": 90},
                }]
            }
        }

        result = self.service._execute(Command(
            CommandType.EXPERIMENT_VALIDATE,
            {"plan": plan},
        ))

        self.assertTrue(result["valid"])

    def test_validate_step_without_acquisition(self):
        """无 acquisition 字段的步骤应通过验证（acquisition 是可选的）。"""
        plan = {
            "sequence": {
                "steps": [{"name": "no_acq", "magnetic_field": {"x_nT": 0, "y_nT": 0, "z_nT": 0}}]
            }
        }

        result = self.service._execute(Command(
            CommandType.EXPERIMENT_VALIDATE,
            {"plan": plan},
        ))

        self.assertTrue(result["valid"])

    # -- 嵌套循环验证 ---------------------------------------------------------

    def test_validate_nested_loop_count(self):
        """loop_count > 1 的计划应通过验证。"""
        plan = {
            "sequence": {
                "loop_count": 5,
                "steps": [
                    {"name": "loop_step", "timing": {"settle_s": 0.0, "hold_s": 0.0}},
                ]
            }
        }

        result = self.service._execute(Command(
            CommandType.EXPERIMENT_VALIDATE,
            {"plan": plan},
        ))

        self.assertTrue(result["valid"])
        self.assertEqual(result["steps"], 1)

    def test_validate_infinite_loop_count_zero(self):
        """loop_count=0 表示无限循环，应通过验证。"""
        plan = {
            "sequence": {
                "loop_count": 0,
                "steps": [
                    {"name": "infinite_step", "timing": {"settle_s": 0.0, "hold_s": 0.0}},
                ]
            }
        }

        result = self.service._execute(Command(
            CommandType.EXPERIMENT_VALIDATE,
            {"plan": plan},
        ))

        self.assertTrue(result["valid"])

    def test_validate_flat_sequence_as_list(self):
        """sequence 直接为列表（非 dict 包装）时应能解析。"""
        plan = {
            "sequence": [
                {"name": "step1", "timing": {"settle_s": 0.0, "hold_s": 0.0}},
            ]
        }

        result = self.service._execute(Command(
            CommandType.EXPERIMENT_VALIDATE,
            {"plan": plan},
        ))

        self.assertTrue(result["valid"])
        self.assertEqual(result["steps"], 1)


# ---------------------------------------------------------------------------
# 实验计划加载测试
# ---------------------------------------------------------------------------

class TestExperimentPlanLoading(unittest.TestCase):
    """_load_experiment_plan 文件/参数/无效格式测试。"""

    def setUp(self):
        self._app = QCoreApplication.instance() or QCoreApplication([])
        self.ctrl = InstrumentController()
        self.service = CommandService(self.ctrl, config={})
        self._temp_files = []

    def tearDown(self):
        self.service._experiment_stop.set()
        if self.service._experiment_thread is not None:
            self.service._experiment_thread.join(timeout=1.0)
        self.ctrl.mag.disconnect_all()
        for f in self._temp_files:
            try:
                os.unlink(f)
            except OSError:
                pass

    def _write_temp_json(self, data: dict) -> str:
        fd, path = tempfile.mkstemp(suffix=".json")
        self._temp_files.append(path)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        return path

    # -- 从文件加载 -----------------------------------------------------------

    def test_load_from_json_file(self):
        """从 JSON 文件加载实验计划。"""
        plan_data = {
            "metadata": {"name": "file_test"},
            "sequence": {
                "steps": [
                    {"name": "s1", "magnetic_field": {"x_nT": 0, "y_nT": 0, "z_nT": 0}},
                ]
            }
        }
        path = self._write_temp_json(plan_data)

        result = self.service._execute(Command(
            CommandType.EXPERIMENT_LOAD_JSON,
            {"path": path},
        ))

        self.assertTrue(result["valid"])
        self.assertEqual(result["steps"], 1)

    def test_load_from_json_file_with_chinese_characters(self):
        """文件路径或内容包含中文时应正确加载。"""
        plan_data = {
            "metadata": {"name": "中文测试"},
            "sequence": {
                "steps": [
                    {"name": "步骤1", "magnetic_field": {"x_nT": 0, "y_nT": 0, "z_nT": 0}},
                ]
            }
        }
        path = self._write_temp_json(plan_data)

        result = self.service._execute(Command(
            CommandType.EXPERIMENT_LOAD_JSON,
            {"path": path},
        ))

        self.assertTrue(result["valid"])

    def test_load_from_nonexistent_file_raises(self):
        """加载不存在的文件应抛出异常。"""
        with self.assertRaises(Exception):
            self.service._execute(Command(
                CommandType.EXPERIMENT_LOAD_JSON,
                {"path": "/nonexistent/path/to/plan.json"},
            ))

    # -- 从参数加载 -----------------------------------------------------------

    def test_load_from_plan_param(self):
        """从 params['plan'] 直接加载实验计划。"""
        plan = {
            "sequence": {
                "steps": [
                    {"name": "s1", "magnetic_field": {"x_nT": 100, "y_nT": 0, "z_nT": 0}},
                ]
            }
        }

        result = self.service._execute(Command(
            CommandType.EXPERIMENT_LOAD_JSON,
            {"plan": plan},
        ))

        self.assertTrue(result["valid"])
        self.assertEqual(result["steps"], 1)

    def test_load_from_params_directly(self):
        """当 params 中无 'plan' 和 'path' 时，params 本身作为 plan。"""
        plan = {
            "sequence": {
                "steps": [
                    {"name": "direct", "timing": {"settle_s": 0.0, "hold_s": 0.0}},
                ]
            }
        }

        result = self.service._execute(Command(
            CommandType.EXPERIMENT_LOAD_JSON,
            plan,  # params IS the plan
        ))

        self.assertTrue(result["valid"])

    # -- 无效 JSON 格式 -------------------------------------------------------

    def test_load_rejects_non_dict_plan_param(self):
        """params['plan'] 不是 dict 时应抛出 ValueError。"""
        with self.assertRaises(ValueError) as ctx:
            self.service._execute(Command(
                CommandType.EXPERIMENT_LOAD_JSON,
                {"plan": "not_a_dict"},
            ))
        self.assertIn("dict", str(ctx.exception))

    def test_load_rejects_list_plan_param(self):
        """params['plan'] 为列表时应抛出 ValueError。"""
        with self.assertRaises(ValueError):
            self.service._execute(Command(
                CommandType.EXPERIMENT_LOAD_JSON,
                {"plan": [{"name": "step1"}]},
            ))

    def test_load_rejects_int_plan_param(self):
        """params['plan'] 为整数时应抛出 ValueError。"""
        with self.assertRaises(ValueError):
            self.service._execute(Command(
                CommandType.EXPERIMENT_LOAD_JSON,
                {"plan": 42},
            ))

    def test_load_rejects_malformed_json_file(self):
        """JSON 文件内容非法时应抛出异常。"""
        fd, path = tempfile.mkstemp(suffix=".json")
        self._temp_files.append(path)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write("{invalid json content!!!")

        with self.assertRaises(Exception):
            self.service._execute(Command(
                CommandType.EXPERIMENT_LOAD_JSON,
                {"path": path},
            ))

    def test_load_json_file_with_utf8_bom(self):
        """带 UTF-8 BOM 的 JSON 文件应能正常加载（encoding='utf-8-sig' 自动跳过 BOM）。"""
        plan_data = {
            "sequence": {
                "steps": [
                    {"name": "bom_test", "timing": {"settle_s": 0.0, "hold_s": 0.0}},
                ]
            }
        }
        fd, path = tempfile.mkstemp(suffix=".json")
        self._temp_files.append(path)
        with os.fdopen(fd, "wb") as f:
            f.write(b'\xef\xbb\xbf')  # UTF-8 BOM
            f.write(json.dumps(plan_data, ensure_ascii=False).encode("utf-8"))

        result = self.service._execute(Command(
            CommandType.EXPERIMENT_LOAD_JSON,
            {"path": path},
        ))
        self.assertTrue(result["valid"])
        self.assertEqual(result["steps"], 1)


# ---------------------------------------------------------------------------
# 实验生命周期测试
# ---------------------------------------------------------------------------

class TestExperimentLifecycle(unittest.TestCase):
    """实验启动、暂停、恢复、停止的端到端测试。"""

    def setUp(self):
        self._app = QCoreApplication.instance() or QCoreApplication([])
        self.ctrl = InstrumentController()
        self.service = CommandService(self.ctrl, config={})

    def tearDown(self):
        self.service._experiment_stop.set()
        if self.service._experiment_thread is not None:
            self.service._experiment_thread.join(timeout=2.0)
        self.ctrl.mag.disconnect_all()

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
        time.sleep(0.3)
        state = self.service._execute(Command(CommandType.EXPERIMENT_QUERY_STATE))
        self.assertFalse(state["running"])
        self.assertEqual(state["step_index"], 0)

    def test_multi_step_experiment_runs_all_steps(self):
        """多步骤实验应依次执行所有步骤。"""
        plan = {
            "sequence": {
                "loop_count": 1,
                "steps": [
                    {"name": "step_a", "timing": {"settle_s": 0.0, "hold_s": 0.0}},
                    {"name": "step_b", "timing": {"settle_s": 0.0, "hold_s": 0.0}},
                    {"name": "step_c", "timing": {"settle_s": 0.0, "hold_s": 0.0}},
                ],
            },
        }

        self.service._execute(Command(CommandType.EXPERIMENT_LOAD_JSON, {"plan": plan}))
        self.service._execute(Command(CommandType.EXPERIMENT_START))
        time.sleep(0.5)

        state = self.service._execute(Command(CommandType.EXPERIMENT_QUERY_STATE))
        self.assertFalse(state["running"])
        # step_index should be the last step index
        self.assertEqual(state["step_index"], 2)

    def test_experiment_pause_and_resume(self):
        """暂停后恢复实验应继续执行。"""
        plan = {
            "sequence": {
                "loop_count": 1,
                "steps": [
                    {
                        "name": "manual_step",
                        "trigger": "manual",
                        "timing": {"settle_s": 0.0, "hold_s": 0.0},
                    },
                ],
            },
        }

        self.service._execute(Command(CommandType.EXPERIMENT_LOAD_JSON, {"plan": plan}))
        self.service._execute(Command(CommandType.EXPERIMENT_START))
        time.sleep(0.2)

        # Should be paused waiting for manual advance
        state = self.service._execute(Command(CommandType.EXPERIMENT_QUERY_STATE))
        self.assertTrue(state["running"])

        # Resume (which also triggers manual advance)
        self.service._execute(Command(CommandType.EXPERIMENT_RESUME))
        time.sleep(0.3)

        state = self.service._execute(Command(CommandType.EXPERIMENT_QUERY_STATE))
        self.assertFalse(state["running"])

    def test_experiment_stop_terminates_running_plan(self):
        """正在运行的实验应能被停止。"""
        plan = {
            "sequence": {
                "loop_count": 1,
                "steps": [
                    {
                        "name": "wait_step",
                        "trigger": "manual",
                        "timing": {"settle_s": 0.0, "hold_s": 10.0},
                    },
                ],
            },
        }

        self.service._execute(Command(CommandType.EXPERIMENT_LOAD_JSON, {"plan": plan}))
        self.service._execute(Command(CommandType.EXPERIMENT_START))
        time.sleep(0.2)

        state = self.service._execute(Command(CommandType.EXPERIMENT_QUERY_STATE))
        self.assertTrue(state["running"])

        self.service._execute(Command(CommandType.EXPERIMENT_STOP))
        time.sleep(0.3)

        state = self.service._execute(Command(CommandType.EXPERIMENT_QUERY_STATE))
        self.assertFalse(state["running"])

    def test_start_experiment_without_loaded_plan_raises(self):
        """未加载计划时启动实验应抛出异常。"""
        with self.assertRaises(ValueError) as ctx:
            self.service._execute(Command(CommandType.EXPERIMENT_START))
        self.assertIn("未加载", str(ctx.exception))

    def test_validate_without_loaded_plan_raises(self):
        """未加载计划时验证应抛出异常。"""
        self.service._experiment_plan = None
        with self.assertRaises(ValueError) as ctx:
            self.service._execute(Command(CommandType.EXPERIMENT_VALIDATE, {}))
        self.assertIn("未加载", str(ctx.exception))

    def test_start_experiment_with_inline_plan(self):
        """通过 EXPERIMENT_START 的 params 直接传入计划。"""
        plan = {
            "sequence": {
                "loop_count": 1,
                "steps": [
                    {
                        "name": "inline_manual",
                        "trigger": "manual",
                        "timing": {"settle_s": 0.0, "hold_s": 0.0},
                    },
                ],
            },
        }

        state = self.service._execute(Command(CommandType.EXPERIMENT_START, {"plan": plan}))
        self.assertTrue(state["running"])

        # Stop the experiment to clean up
        self.service._execute(Command(CommandType.EXPERIMENT_STOP))
        time.sleep(0.3)

        state = self.service._execute(Command(CommandType.EXPERIMENT_QUERY_STATE))
        self.assertFalse(state["running"])

    def test_experiment_with_loop_count_runs_multiple_loops(self):
        """loop_count=3 应执行 3 次循环。"""
        step_counter = {"count": 0}
        original_apply = self.service._apply_experiment_step

        def counting_apply(step):
            step_counter["count"] += 1
            # Don't actually apply hardware commands in tests

        self.service._apply_experiment_step = counting_apply

        plan = {
            "sequence": {
                "loop_count": 3,
                "steps": [
                    {"name": "looped_step", "timing": {"settle_s": 0.0, "hold_s": 0.0}},
                ],
            },
        }

        self.service._execute(Command(CommandType.EXPERIMENT_LOAD_JSON, {"plan": plan}))
        self.service._execute(Command(CommandType.EXPERIMENT_START))
        time.sleep(0.5)

        state = self.service._execute(Command(CommandType.EXPERIMENT_QUERY_STATE))
        self.assertFalse(state["running"])
        self.assertEqual(step_counter["count"], 3)

    def test_experiment_safety_stop_on_error(self):
        """实验执行中出错时应触发安全停止。"""
        original_apply = self.service._apply_experiment_step

        def failing_apply(step):
            raise RuntimeError("Simulated hardware failure")

        self.service._apply_experiment_step = failing_apply

        plan = {
            "sequence": {
                "loop_count": 1,
                "steps": [
                    {"name": "failing_step", "timing": {"settle_s": 0.0, "hold_s": 0.0}},
                ],
            },
        }

        self.service._execute(Command(CommandType.EXPERIMENT_LOAD_JSON, {"plan": plan}))
        self.service._execute(Command(CommandType.EXPERIMENT_START))
        time.sleep(0.5)

        state = self.service._execute(Command(CommandType.EXPERIMENT_QUERY_STATE))
        self.assertFalse(state["running"])
        self.assertIn("Simulated hardware failure", state.get("error", ""))


# ---------------------------------------------------------------------------
# _resolve_field_step 测试
# ---------------------------------------------------------------------------

class TestResolveFieldStep(unittest.TestCase):
    """磁场步骤解析：球坐标 ↔ 笛卡尔坐标转换。"""

    def test_cartesian_passthrough(self):
        """笛卡尔坐标直接透传。"""
        result = CommandService._resolve_field_step({"x_nT": 100, "y_nT": 200, "z_nT": 300})
        self.assertAlmostEqual(result["x_nT"], 100.0)
        self.assertAlmostEqual(result["y_nT"], 200.0)
        self.assertAlmostEqual(result["z_nT"], 300.0)

    def test_spherical_conversion_theta_zero(self):
        """theta=0 表示沿 Z 轴方向。"""
        result = CommandService._resolve_field_step({
            "magnitude_nT": 1000, "theta_deg": 0, "phi_deg": 0
        })
        self.assertAlmostEqual(result["x_nT"], 0.0, places=5)
        self.assertAlmostEqual(result["y_nT"], 0.0, places=5)
        self.assertAlmostEqual(result["z_nT"], 1000.0, places=5)

    def test_spherical_conversion_theta_90_phi_0(self):
        """theta=90, phi=0 表示沿 X 轴方向。"""
        result = CommandService._resolve_field_step({
            "magnitude_nT": 500, "theta_deg": 90, "phi_deg": 0
        })
        self.assertAlmostEqual(result["x_nT"], 500.0, places=5)
        self.assertAlmostEqual(result["y_nT"], 0.0, places=5)
        self.assertAlmostEqual(result["z_nT"], 0.0, places=5)

    def test_spherical_conversion_theta_90_phi_90(self):
        """theta=90, phi=90 表示沿 Y 轴方向。"""
        result = CommandService._resolve_field_step({
            "magnitude_nT": 500, "theta_deg": 90, "phi_deg": 90
        })
        self.assertAlmostEqual(result["x_nT"], 0.0, places=5)
        self.assertAlmostEqual(result["y_nT"], 500.0, places=5)
        self.assertAlmostEqual(result["z_nT"], 0.0, places=5)

    def test_cartesian_with_field_x_nT_alias(self):
        """field_x_nT 别名应正确解析。"""
        result = CommandService._resolve_field_step({
            "field_x_nT": 10, "field_y_nT": 20, "field_z_nT": 30
        })
        self.assertAlmostEqual(result["x_nT"], 10.0)
        self.assertAlmostEqual(result["y_nT"], 20.0)
        self.assertAlmostEqual(result["z_nT"], 30.0)

    def test_cartesian_defaults_to_zero(self):
        """缺失的坐标默认为 0。"""
        result = CommandService._resolve_field_step({})
        self.assertAlmostEqual(result["x_nT"], 0.0)
        self.assertAlmostEqual(result["y_nT"], 0.0)
        self.assertAlmostEqual(result["z_nT"], 0.0)


if __name__ == "__main__":
    unittest.main()
