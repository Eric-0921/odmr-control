import unittest

from instruments.oe1022d import OE1022DDriver, RALL_TOTAL_BYTES


class TestLockinConfigHelpers(unittest.TestCase):
    def test_display_interval_follows_time_constant_policy(self):
        self.assertEqual(OE1022DDriver.display_interval_for_time_constant(6), 50)
        self.assertEqual(OE1022DDriver.display_interval_for_time_constant(8), 100)
        self.assertEqual(OE1022DDriver.display_interval_for_time_constant(10), 300)
        self.assertEqual(OE1022DDriver.display_interval_for_time_constant(17), 300)

    def test_parse_rall_config_snapshot(self):
        raw = bytearray(RALL_TOTAL_BYTES)
        raw[8390] = 10
        raw[8391] = 2
        raw[8404] = 8
        raw[8405] = 3
        raw[8406] = 1
        raw[8481] = 1
        raw[8390 + 96] = 12
        raw[8404 + 96] = 9

        cfg = OE1022DDriver.parse_rall_config(bytes(raw))

        self.assertEqual(cfg["A"]["sensitivity"], "2 uV")
        self.assertEqual(cfg["A"]["time_constant"], "100 ms")
        self.assertEqual(cfg["A"]["filter_slope"], "24 dB/oct")
        self.assertTrue(cfg["A"]["sync_filter"])
        self.assertTrue(cfg["A"]["pll_locked"])
        self.assertEqual(cfg["B"]["sensitivity_index"], 12)
        self.assertEqual(cfg["B"]["time_constant_s"], 0.300)


if __name__ == "__main__":
    unittest.main()
