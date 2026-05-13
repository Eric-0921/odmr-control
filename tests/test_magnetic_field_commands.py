"""磁场控制命令测试

覆盖范围：
- FieldController.match_devices_to_axes 设备识别逻辑
- MAG_* 命令通过 CommandService 的路由与安全校验
- 边界条件：空列表、重复 IDN、绑定格式不一致、索引回退
"""

import unittest
from unittest.mock import MagicMock, patch

from PyQt5.QtCore import QCoreApplication

from core.command_service import CommandService, SafetyError
from core.commands import Command, CommandType
from core.instrument_controller import InstrumentController
from core.mag_field_controller import AXES, FieldController


# ---------------------------------------------------------------------------
# match_devices_to_axes 纯函数测试（不依赖 Qt / 硬件）
# ---------------------------------------------------------------------------

class TestMatchDevicesToAxes(unittest.TestCase):
    """FieldController.match_devices_to_axes 设备识别逻辑测试。"""

    # -- 空设备列表 -----------------------------------------------------------

    def test_empty_detected_with_bindings_returns_all_none(self):
        """扫描无设备但有绑定配置时，所有轴应返回 None。"""
        detected = []
        bindings = {"X": "PSU-X,SN001", "Y": "PSU-Y,SN002", "Z": "PSU-Z,SN003"}

        result = FieldController.match_devices_to_axes(detected, bindings)

        for axis in AXES:
            self.assertIsNone(result[axis], f"{axis} axis should be None when no devices detected")

    def test_empty_detected_without_bindings_returns_all_none(self):
        """扫描无设备且无绑定配置时，所有轴应返回 None。"""
        detected = []
        bindings = {"X": "", "Y": "", "Z": ""}

        result = FieldController.match_devices_to_axes(detected, bindings)

        for axis in AXES:
            self.assertIsNone(result[axis])

    # -- 重复 IDN ------------------------------------------------------------

    def test_duplicate_idn_last_port_wins(self):
        """两个设备返回相同 IDN 时，后者覆盖前者（dict 构造行为）。

        这是一个潜在的 bug：如果硬件固件异常导致重复 IDN，
        用户绑定的轴可能被错误地映射到另一个端口。
        """
        detected = [
            {"port": "COM3", "idn": "PSU,SN001"},
            {"port": "COM5", "idn": "PSU,SN001"},  # 重复 IDN
        ]
        bindings = {"X": "PSU,SN001", "Y": "", "Z": ""}

        result = FieldController.match_devices_to_axes(detected, bindings)

        # COM5 wins because it's the last entry with that IDN in the dict
        self.assertEqual(result["X"], "COM5")
        self.assertIsNone(result["Y"])
        self.assertIsNone(result["Z"])

    def test_duplicate_idn_different_axes_cannot_both_match(self):
        """两个轴绑定到同一 IDN 时，都会映射到最后一个端口。

        这暴露了 match_devices_to_axes 的一个设计局限：
        同一 IDN 无法区分两个不同物理设备。
        """
        detected = [
            {"port": "COM3", "idn": "PSU,SN001"},
            {"port": "COM5", "idn": "PSU,SN001"},
        ]
        bindings = {"X": "PSU,SN001", "Y": "PSU,SN001", "Z": ""}

        result = FieldController.match_devices_to_axes(detected, bindings)

        # Both map to COM5 (last-wins), which is incorrect for Y axis
        self.assertEqual(result["X"], "COM5")
        self.assertEqual(result["Y"], "COM5")

    # -- 绑定格式不一致 -------------------------------------------------------

    def test_partial_bindings_only_matched_axes_assigned(self):
        """只有部分轴有绑定时，未绑定的轴应为 None。"""
        detected = [
            {"port": "COM3", "idn": "PSU-X,SN001"},
            {"port": "COM4", "idn": "PSU-Y,SN002"},
            {"port": "COM5", "idn": "PSU-Z,SN003"},
        ]
        bindings = {"X": "PSU-X,SN001", "Y": "", "Z": ""}

        result = FieldController.match_devices_to_axes(detected, bindings)

        self.assertEqual(result["X"], "COM3")
        self.assertIsNone(result["Y"])
        self.assertIsNone(result["Z"])

    def test_binding_idn_not_in_detected_returns_none(self):
        """绑定的 IDN 不在检测列表中时，对应轴应为 None。"""
        detected = [
            {"port": "COM3", "idn": "PSU-X,SN001"},
        ]
        bindings = {"X": "PSU-X,SN001", "Y": "PSU-Y,SN999", "Z": ""}

        result = FieldController.match_devices_to_axes(detected, bindings)

        self.assertEqual(result["X"], "COM3")
        self.assertIsNone(result["Y"])  # SN999 not detected
        self.assertIsNone(result["Z"])

    def test_empty_string_binding_treated_as_no_binding(self):
        """绑定值为空字符串时，该轴不应参与 IDN 匹配。"""
        detected = [
            {"port": "COM3", "idn": "PSU-X,SN001"},
        ]
        # X has a binding, Y has empty string, Z not in dict at all
        bindings = {"X": "PSU-X,SN001", "Y": ""}

        result = FieldController.match_devices_to_axes(detected, bindings)

        self.assertEqual(result["X"], "COM3")
        self.assertIsNone(result["Y"])
        self.assertIsNone(result["Z"])

    # -- 索引-based 分配（回退） ----------------------------------------------

    def test_index_based_fallback_all_bindings_empty(self):
        """所有绑定为空时，按索引顺序分配端口。"""
        detected = [
            {"port": "COM3", "idn": "PSU-A"},
            {"port": "COM4", "idn": "PSU-B"},
            {"port": "COM5", "idn": "PSU-C"},
        ]
        bindings = {"X": "", "Y": "", "Z": ""}

        result = FieldController.match_devices_to_axes(detected, bindings)

        self.assertEqual(result["X"], "COM3")
        self.assertEqual(result["Y"], "COM4")
        self.assertEqual(result["Z"], "COM5")

    def test_index_based_fallback_fewer_devices_than_axes(self):
        """设备数量少于轴数量时，多余轴应为 None。"""
        detected = [
            {"port": "COM3", "idn": "PSU-A"},
        ]
        bindings = {"X": "", "Y": "", "Z": ""}

        result = FieldController.match_devices_to_axes(detected, bindings)

        self.assertEqual(result["X"], "COM3")
        self.assertIsNone(result["Y"])
        self.assertIsNone(result["Z"])

    def test_index_based_fallback_more_devices_than_axes(self):
        """设备数量多于轴数量时，多余设备被忽略。"""
        detected = [
            {"port": "COM3", "idn": "PSU-A"},
            {"port": "COM4", "idn": "PSU-B"},
            {"port": "COM5", "idn": "PSU-C"},
            {"port": "COM6", "idn": "PSU-D"},  # extra
        ]
        bindings = {"X": "", "Y": "", "Z": ""}

        result = FieldController.match_devices_to_axes(detected, bindings)

        self.assertEqual(result["X"], "COM3")
        self.assertEqual(result["Y"], "COM4")
        self.assertEqual(result["Z"], "COM5")
        # COM6 is not assigned to any axis

    def test_index_based_fallback_two_devices(self):
        """仅检测到 2 个设备时，X 和 Y 按顺序分配，Z 为 None。"""
        detected = [
            {"port": "COM3", "idn": "PSU-A"},
            {"port": "COM7", "idn": "PSU-B"},
        ]
        bindings = {"X": "", "Y": "", "Z": ""}

        result = FieldController.match_devices_to_axes(detected, bindings)

        self.assertEqual(result["X"], "COM3")
        self.assertEqual(result["Y"], "COM7")
        self.assertIsNone(result["Z"])

    # -- 混合场景 -------------------------------------------------------------

    def test_bindings_key_missing_from_dict(self):
        """bindings 字典缺少某些轴的键时，视为无绑定。"""
        detected = [
            {"port": "COM3", "idn": "PSU-A"},
            {"port": "COM4", "idn": "PSU-B"},
        ]
        # Only X is in bindings, Y and Z are missing entirely
        bindings = {"X": "PSU-A"}

        result = FieldController.match_devices_to_axes(detected, bindings)

        # has_bindings is True because X has a value
        self.assertEqual(result["X"], "COM3")
        # Y and Z have no binding, so they are not matched
        self.assertIsNone(result["Y"])
        self.assertIsNone(result["Z"])

    def test_all_bindings_none_values(self):
        """bindings 值为 None 时，应视为无绑定并回退到索引分配。"""
        detected = [
            {"port": "COM3", "idn": "PSU-A"},
            {"port": "COM4", "idn": "PSU-B"},
        ]
        # None values should be falsy
        bindings = {"X": None, "Y": None, "Z": None}

        result = FieldController.match_devices_to_axes(detected, bindings)

        # None is falsy, so has_bindings is False -> index-based
        self.assertEqual(result["X"], "COM3")
        self.assertEqual(result["Y"], "COM4")
        self.assertIsNone(result["Z"])


# ---------------------------------------------------------------------------
# CommandService MAG_* 命令路由测试
# ---------------------------------------------------------------------------

class TestMagneticFieldCommands(unittest.TestCase):
    """MAG_* 命令通过 CommandService 的路由与安全校验。"""

    def setUp(self):
        self._app = QCoreApplication.instance() or QCoreApplication([])
        self.ctrl = InstrumentController()
        self.service = CommandService(
            self.ctrl,
            config={
                "magnetic_field": {
                    "axes": {
                        "X": {"coil_constant": 100.0, "zero_offset_mA": 2.0},
                    }
                }
            },
        )

    def tearDown(self):
        self.ctrl.mag.disconnect_all()

    def test_set_field_uses_axis_coil_constant(self):
        self.service._execute(
            Command(CommandType.MAG_SET_COIL_CONSTANT, {"axis": "X", "coil_constant": 200.0})
        )

        result = self.service._execute(
            Command(CommandType.MAG_SET_FIELD, {"axis": "X", "field_nT": 1000.0})
        )

        self.assertAlmostEqual(result["recur_current_mA"], 5.0)
        self.assertAlmostEqual(result["target_field_nT"], 1000.0)

    def test_negative_field_is_rejected(self):
        with self.assertRaises(Exception):
            self.service._execute(
                Command(CommandType.MAG_SET_FIELD, {"axis": "X", "field_nT": -1.0})
            )

    def test_agent_style_query_returns_three_axis_snapshot(self):
        result = self.service._execute(Command(CommandType.MAG_QUERY_STATE))

        self.assertIn("axes", result)
        self.assertEqual(sorted(result["axes"].keys()), ["X", "Y", "Z"])
        self.assertFalse(result["axes"]["X"]["connected"])

    def test_auto_detect_matches_axis_bindings_by_idn(self):
        devices = [
            {"port": "/dev/tty.x", "idn": "PSU-X,SN001"},
            {"port": "/dev/tty.y", "idn": "PSU-Y,SN002"},
        ]
        self.ctrl.mag.scan_device_ports = lambda baudrate=9600: devices

        result = self.service._execute(Command(
            CommandType.MAG_AUTO_DETECT,
            {"bindings": {"X": "PSU-X,SN001", "Y": "PSU-Y,SN002"}},
        ))

        self.assertEqual(result["matched"]["X"]["port"], "/dev/tty.x")
        self.assertEqual(result["matched"]["Y"]["idn"], "PSU-Y,SN002")

    def test_connect_all_rejects_duplicate_ports_before_hardware(self):
        with self.assertRaises(Exception):
            self.service._execute(Command(
                CommandType.MAG_CONNECT_ALL,
                {"ports": {"X": "/dev/tty.same", "Y": "/dev/tty.same", "Z": ""}},
            ))

    def test_prepare_zero_lock_command_routes_to_field_controller(self):
        calls = {}

        def prepare(axis, capture_readback=False):
            calls["axis"] = axis
            calls["capture_readback"] = capture_readback
            return True

        self.ctrl.mag.prepare_zero_lock = prepare
        self.ctrl.mag.get_status = lambda axis: {"axis": axis, "output_on": True, "lock_zero": True}

        result = self.service._execute(Command(
            CommandType.MAG_PREPARE_ZERO_LOCK,
            {"axis": "X", "capture_readback": True},
        ))

        self.assertEqual(calls, {"axis": "X", "capture_readback": True})
        self.assertTrue(result["output_on"])
        self.assertTrue(result["lock_zero"])

    # -- _normalize_axis 测试 -------------------------------------------------

    def test_normalize_axis_accepts_lowercase(self):
        result = self.service._execute(
            Command(CommandType.MAG_QUERY_STATE, {"axis": "x"})
        )
        self.assertIn("axis", result)
        self.assertEqual(result["axis"], "X")

    def test_normalize_axis_rejects_invalid_value(self):
        with self.assertRaises(ValueError) as ctx:
            self.service._execute(
                Command(CommandType.MAG_SET_FIELD, {"axis": "W", "field_nT": 100.0})
            )
        self.assertIn("W", str(ctx.exception))

    def test_normalize_axis_rejects_numeric_input(self):
        with self.assertRaises(ValueError):
            self.service._execute(
                Command(CommandType.MAG_SET_FIELD, {"axis": 1, "field_nT": 100.0})
            )

    # -- 安全校验测试 ---------------------------------------------------------

    def test_coil_constant_must_be_positive(self):
        with self.assertRaises(SafetyError) as ctx:
            self.service._execute(
                Command(CommandType.MAG_SET_COIL_CONSTANT, {"axis": "X", "coil_constant": -1.0})
            )
        self.assertIn("正数", str(ctx.exception))

    def test_coil_constant_zero_is_rejected(self):
        with self.assertRaises(SafetyError):
            self.service._execute(
                Command(CommandType.MAG_SET_COIL_CONSTANT, {"axis": "X", "coil_constant": 0.0})
            )

    def test_mag_set_output_rejects_disconnected_axis(self):
        with self.assertRaises(SafetyError) as ctx:
            self.service._execute(
                Command(CommandType.MAG_SET_OUTPUT, {"axis": "Y", "enabled": True})
            )
        self.assertIn("未连接", str(ctx.exception))

    def test_duplicate_ports_in_connect_all_raises_safety_error(self):
        """MAG_CONNECT_ALL 应在发送硬件命令前检测重复端口。"""
        with self.assertRaises(SafetyError) as ctx:
            self.service._execute(Command(
                CommandType.MAG_CONNECT_ALL,
                {"ports": {"X": "COM3", "Y": "COM3", "Z": "COM5"}},
            ))
        self.assertIn("重复", str(ctx.exception))

    # -- MAG_SCAN_PORTS 测试 --------------------------------------------------

    def test_scan_ports_returns_device_list(self):
        self.ctrl.mag.scan_device_ports = lambda baudrate=9600: [
            {"port": "COM3", "idn": "PSU-X,SN001"},
        ]

        result = self.service._execute(Command(CommandType.MAG_SCAN_PORTS))

        self.assertIn("devices", result)
        self.assertEqual(len(result["devices"]), 1)
        self.assertEqual(result["devices"][0]["port"], "COM3")

    def test_scan_ports_returns_empty_when_no_devices(self):
        self.ctrl.mag.scan_device_ports = lambda baudrate=9600: []

        result = self.service._execute(Command(CommandType.MAG_SCAN_PORTS))

        self.assertEqual(result["devices"], [])

    # -- MAG_AUTO_DETECT 测试 -------------------------------------------------

    def test_auto_detect_with_empty_devices(self):
        self.ctrl.mag.scan_device_ports = lambda baudrate=9600: []

        result = self.service._execute(Command(
            CommandType.MAG_AUTO_DETECT,
            {"bindings": {"X": "PSU-X,SN001", "Y": "", "Z": ""}},
        ))

        self.assertEqual(result["devices"], [])
        self.assertEqual(result["matched"]["X"]["port"], "")
        self.assertEqual(result["matched"]["Y"]["port"], "")

    def test_auto_detect_uses_config_bindings_when_none_provided(self):
        """当 params 中无 bindings 时，应从 config 读取。"""
        self.service._config = {
            "magnetic_field": {
                "bindings": {
                    "X": {"idn": "PSU-X,SN001", "port": "COM3"},
                    "Y": {"idn": "", "port": ""},
                    "Z": {"idn": "", "port": ""},
                }
            }
        }
        self.ctrl.mag.scan_device_ports = lambda baudrate=9600: [
            {"port": "COM3", "idn": "PSU-X,SN001"},
        ]

        result = self.service._execute(Command(CommandType.MAG_AUTO_DETECT, {}))

        self.assertEqual(result["matched"]["X"]["port"], "COM3")

    # -- MAG_BIND_AXIS_IDN 测试 -----------------------------------------------

    def test_bind_axis_idn_stores_in_config(self):
        result = self.service._execute(Command(
            CommandType.MAG_BIND_AXIS_IDN,
            {"axis": "X", "idn": "PSU-X,SN001", "port": "COM3"},
        ))

        self.assertEqual(result["axis"], "X")
        self.assertEqual(result["binding"]["idn"], "PSU-X,SN001")
        self.assertEqual(self.service._config["magnetic_field"]["bindings"]["X"]["idn"], "PSU-X,SN001")

    # -- MAG_SET_FIELD_3D 测试 ------------------------------------------------

    def test_set_field_3d_skips_disconnected_axes(self):
        """set_field_3d 不应对未连接的轴报错。"""
        # No axes connected, should not raise
        result = self.service._execute(Command(
            CommandType.MAG_SET_FIELD_3D,
            {"x_nT": 100.0, "y_nT": 200.0, "z_nT": 300.0},
        ))
        self.assertIn("axes", result)

    # -- MAG_EMERGENCY_STOP 测试 -----------------------------------------------

    def test_emergency_stop_returns_verification(self):
        """emergency_stop 应返回验证结果（仅包含已连接的轴）。"""
        result = self.service._execute(Command(CommandType.MAG_EMERGENCY_STOP))

        # No axes connected, so result is empty but should not raise
        self.assertIsInstance(result, dict)

    def test_emergency_stop_with_connected_axis(self):
        """已连接轴的 emergency_stop 应返回该轴的验证信息。"""
        # Mock a connected axis
        original_is_connected = self.ctrl.mag.is_connected
        self.ctrl.mag.is_connected = lambda axis: axis == "X"
        original_verify = self.ctrl.mag.verify_emergency_stop
        self.ctrl.mag.verify_emergency_stop = lambda: {
            "X": {"output_on": False, "current_mA": 0.0, "ok": True}
        }
        try:
            result = self.service._execute(Command(CommandType.MAG_EMERGENCY_STOP))
            self.assertIn("X", result)
            self.assertTrue(result["X"]["ok"])
        finally:
            self.ctrl.mag.is_connected = original_is_connected
            self.ctrl.mag.verify_emergency_stop = original_verify


if __name__ == "__main__":
    unittest.main()
