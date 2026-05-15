"""单位选择器单元转换测试

验证 UnitSelector 及其子类的数值转换逻辑。
不需要硬件或 GUI —— 仅测试纯数值逻辑。
"""

import math
import unittest
import sys
import os

# 确保项目根目录在 sys.path 中
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.unit_selector import (
    UNIT_CONVERSIONS,
    dbm_to_mw,
    mw_to_dbm,
)


class TestUnitConversions(unittest.TestCase):
    """验证 UNIT_CONVERSIONS 字典中的系数正确性"""

    # --- 频率 ---

    def test_frequency_hz_to_ghz(self):
        self.assertEqual(UNIT_CONVERSIONS["frequency"]["GHz"], 1e9)

    def test_frequency_mhz_to_hz(self):
        # 1 MHz = 1e6 Hz
        self.assertEqual(UNIT_CONVERSIONS["frequency"]["MHz"], 1e6)

    def test_frequency_khz(self):
        self.assertEqual(UNIT_CONVERSIONS["frequency"]["kHz"], 1e3)

    # --- 磁场 ---

    def test_magnetic_field_t_to_nt(self):
        # 1 T = 1e9 nT
        self.assertEqual(UNIT_CONVERSIONS["magnetic_field"]["T"], 1e9)

    def test_magnetic_field_mt(self):
        self.assertEqual(UNIT_CONVERSIONS["magnetic_field"]["mT"], 1e6)

    def test_magnetic_field_gauss(self):
        # 1 G = 100,000 nT
        self.assertEqual(UNIT_CONVERSIONS["magnetic_field"]["G"], 1e5)

    def test_magnetic_field_oe(self):
        # 1 Oe = 100,000 nT (in vacuum)
        self.assertEqual(UNIT_CONVERSIONS["magnetic_field"]["Oe"], 1e5)

    # --- 时间 ---

    def test_time_s(self):
        self.assertEqual(UNIT_CONVERSIONS["time"]["s"], 1.0)

    def test_time_ms(self):
        self.assertAlmostEqual(UNIT_CONVERSIONS["time"]["ms"], 1e-3)

    def test_time_us(self):
        self.assertAlmostEqual(UNIT_CONVERSIONS["time"]["us"], 1e-6)

    def test_time_min(self):
        self.assertEqual(UNIT_CONVERSIONS["time"]["min"], 60.0)

    def test_time_h(self):
        self.assertEqual(UNIT_CONVERSIONS["time"]["h"], 3600.0)

    # --- 电压 ---

    def test_voltage_v(self):
        self.assertEqual(UNIT_CONVERSIONS["voltage"]["V"], 1.0)

    def test_voltage_mv(self):
        self.assertAlmostEqual(UNIT_CONVERSIONS["voltage"]["mV"], 1e-3)


class TestDbmMwConversion(unittest.TestCase):
    """验证 dBm <-> mW 对数转换"""

    def test_dbm_to_mw_0dBm(self):
        """0 dBm = 1 mW"""
        self.assertAlmostEqual(dbm_to_mw(0.0), 1.0)

    def test_dbm_to_mw_10dBm(self):
        """10 dBm = 10 mW"""
        self.assertAlmostEqual(dbm_to_mw(10.0), 10.0)

    def test_dbm_to_mw_neg10dBm(self):
        """-10 dBm = 0.1 mW"""
        self.assertAlmostEqual(dbm_to_mw(-10.0), 0.1)

    def test_dbm_to_mw_30dBm(self):
        """30 dBm = 1000 mW = 1 W"""
        self.assertAlmostEqual(dbm_to_mw(30.0), 1000.0)

    def test_mw_to_dbm_1mw(self):
        """1 mW = 0 dBm"""
        self.assertAlmostEqual(mw_to_dbm(1.0), 0.0)

    def test_mw_to_dbm_10mw(self):
        """10 mW = 10 dBm"""
        self.assertAlmostEqual(mw_to_dbm(10.0), 10.0)

    def test_mw_to_dbm_0_1mw(self):
        """0.1 mW = -10 dBm"""
        self.assertAlmostEqual(mw_to_dbm(0.1), -10.0)

    def test_mw_to_dbm_zero(self):
        """0 mW -> -inf"""
        self.assertEqual(mw_to_dbm(0.0), float("-inf"))

    def test_roundtrip_dbm_mw_dbm(self):
        """dBm -> mW -> dBm 往返精度"""
        for dbm_val in [-30.0, -10.0, 0.0, 5.5, 10.0, 20.0, 30.0]:
            mw = dbm_to_mw(dbm_val)
            back = mw_to_dbm(mw)
            self.assertAlmostEqual(back, dbm_val, places=10,
                                   msg=f"Roundtrip failed for {dbm_val} dBm")


class TestFrequencyConversionLogic(unittest.TestCase):
    """验证频率单位转换的数值逻辑（不创建 GUI 组件）"""

    def test_ghz_to_hz(self):
        """2.87 GHz = 2,870,000,000 Hz"""
        freq_ghz = 2.87
        freq_hz = freq_ghz * UNIT_CONVERSIONS["frequency"]["GHz"]
        self.assertAlmostEqual(freq_hz, 2.87e9)

    def test_mhz_to_hz(self):
        """100 MHz = 100,000,000 Hz"""
        freq_mhz = 100.0
        freq_hz = freq_mhz * UNIT_CONVERSIONS["frequency"]["MHz"]
        self.assertAlmostEqual(freq_hz, 1e8)

    def test_hz_to_ghz(self):
        """1e9 Hz -> 1.0 GHz"""
        freq_hz = 1e9
        freq_ghz = freq_hz / UNIT_CONVERSIONS["frequency"]["GHz"]
        self.assertAlmostEqual(freq_ghz, 1.0)


class TestTimeConversionLogic(unittest.TestCase):
    """验证时间单位转换的数值逻辑"""

    def test_us_to_s(self):
        """1000 us = 0.001 s"""
        t_us = 1000.0
        t_s = t_us * UNIT_CONVERSIONS["time"]["us"]
        self.assertAlmostEqual(t_s, 0.001)

    def test_min_to_s(self):
        """5 min = 300 s"""
        t_min = 5.0
        t_s = t_min * UNIT_CONVERSIONS["time"]["min"]
        self.assertAlmostEqual(t_s, 300.0)

    def test_h_to_s(self):
        """1 h = 3600 s"""
        t_h = 1.0
        t_s = t_h * UNIT_CONVERSIONS["time"]["h"]
        self.assertAlmostEqual(t_s, 3600.0)


class TestMagneticFieldConversionLogic(unittest.TestCase):
    """验证磁场单位转换的数值逻辑"""

    def test_mt_to_nt(self):
        """1 mT = 1,000,000 nT"""
        val_mt = 1.0
        val_nt = val_mt * UNIT_CONVERSIONS["magnetic_field"]["mT"]
        self.assertAlmostEqual(val_nt, 1e6)

    def test_t_to_nt(self):
        """0.5 T = 500,000,000 nT"""
        val_t = 0.5
        val_nt = val_t * UNIT_CONVERSIONS["magnetic_field"]["T"]
        self.assertAlmostEqual(val_nt, 5e8)

    def test_gauss_to_nt(self):
        """10 G = 1,000,000 nT"""
        val_g = 10.0
        val_nt = val_g * UNIT_CONVERSIONS["magnetic_field"]["G"]
        self.assertAlmostEqual(val_nt, 1e6)


if __name__ == "__main__":
    unittest.main()
