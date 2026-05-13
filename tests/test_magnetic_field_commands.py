import unittest

from PyQt5.QtCore import QCoreApplication

from core.command_service import CommandService
from core.commands import Command, CommandType
from core.instrument_controller import InstrumentController


class TestMagneticFieldCommands(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
