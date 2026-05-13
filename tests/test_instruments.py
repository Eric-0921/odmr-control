"""单元测试

基于 FakeVISA / FakeSerial stub，无需真实硬件或 PyQt5 即可运行。
"""

from __future__ import annotations

import struct
import unittest
from io import BytesIO

import numpy as np

from instruments.oe1022d import OE1022DDriver, RALL_PARAMS, RALL_TOTAL_BYTES, SAMPLES_PER_BATCH
from instruments.smb100a import SMB100ADriver


class FakeResource:
    """Mock pyvisa resource for testing."""

    def __init__(self):
        self.timeout = 10000
        self.write_termination = "\n"
        self.read_termination = "\n"
        self._output_on = False
        self._mode = "CW"
        self._freq = 2.82e9
        self._power = -30.0

    def clear(self):
        pass

    def write(self, cmd: str):
        if cmd == "OUTP OFF":
            self._output_on = False
        elif cmd == "OUTP ON":
            self._output_on = True
        elif cmd == "FREQ:MODE CW":
            self._mode = "CW"
        elif cmd == "SOUR:FREQ:MODE SWEEP":
            self._mode = "SWEEP"
        elif cmd.startswith("FREQ:CW "):
            self._freq = float(cmd.split()[1])
        elif cmd.startswith("POW:LEV "):
            self._power = float(cmd.split()[1])

    def query(self, cmd: str) -> str:
        if cmd == "*IDN?":
            return "Rohde&Schwarz,SMB100A,123456,1.0"
        elif cmd == "FREQ:CW?":
            return str(self._freq)
        elif cmd == "POW:LEV?":
            return str(self._power)
        elif cmd == "OUTP?":
            return "1" if self._output_on else "0"
        elif cmd == "FREQ:MODE?":
            return self._mode
        return "0"

    def close(self):
        pass


class FakeResourceManager:
    def open_resource(self, addr: str):
        return FakeResource()

    def release_all_resources(self):
        pass


class FakeSerial:
    """Mock serial port for testing."""

    def __init__(self, **kwargs):
        self.is_open = True
        self.timeout = kwargs.get("timeout", 2.0)
        self._buffer = b""
        self._rall_counter = 0

    def reset_input_buffer(self):
        pass

    def reset_output_buffer(self):
        pass

    def write(self, data: bytes):
        cmd = data.decode("ascii", errors="replace").strip()
        if cmd == "*IDND?":
            self._buffer = b"SSI LIA-OE1022D,SN123456,Ver1.0\r\n"
        elif cmd == "SNAPD? 1,0,1,2,3":
            self._buffer = b"0.001,0.002,0.003,45.0\r\n"
        elif cmd == "RALL?":
            # Generate fake 12288 bytes
            raw = bytearray(RALL_TOTAL_BYTES)
            for i in range(len(RALL_PARAMS)):
                offset = i * 400
                for j in range(SAMPLES_PER_BATCH):
                    val = 0.001 * (self._rall_counter + j)
                    struct.pack_into("<d", raw, offset + j * 8, val)
            self._buffer = bytes(raw)
            self._rall_counter += 1

    def read(self, n: int) -> bytes:
        chunk = self._buffer[:n]
        self._buffer = self._buffer[n:]
        return chunk

    def readline(self) -> bytes:
        idx = self._buffer.find(b"\n")
        if idx == -1:
            chunk = self._buffer
            self._buffer = b""
            return chunk
        chunk = self._buffer[: idx + 1]
        self._buffer = self._buffer[idx + 1 :]
        return chunk

    def close(self):
        self.is_open = False


class TestSMB100ADriver(unittest.TestCase):
    def setUp(self):
        self.driver = SMB100ADriver()
        # Monkey-patch pyvisa
        import instruments.smb100a as smb_mod

        smb_mod.pyvisa = type("mod", (), {"ResourceManager": FakeResourceManager})()

    def test_connect(self):
        idn = self.driver.connect("FAKE::ADDR")
        self.assertIn("SMB100A", idn)
        self.assertTrue(self.driver.is_connected)

    def test_freq_validation(self):
        self.driver.validate_frequency(1e9)
        with self.assertRaises(ValueError):
            self.driver.validate_frequency(100e12)

    def test_power_validation(self):
        self.driver.validate_power(-10)
        with self.assertRaises(ValueError):
            self.driver.validate_power(100)

    def test_sweep_params_validation(self):
        self.driver.validate_sweep_params(1e9, 2e9, 1e6)
        with self.assertRaises(ValueError):
            self.driver.validate_sweep_params(2e9, 1e9, 1e6)

    def test_emergency_stop(self):
        self.driver.connect("FAKE::ADDR")
        self.driver.set_output(True)
        self.driver.set_freq_mode("SWEEP")
        self.driver.emergency_stop()
        self.assertFalse(self.driver.cached_output_on)
        self.assertEqual(self.driver.cached_mode, "CW")


class TestOE1022DDriver(unittest.TestCase):
    def setUp(self):
        self.driver = OE1022DDriver()
        # Monkey-patch serial
        self.driver._serial = FakeSerial()

    def test_connect(self):
        # connect already sets fake serial in setUp
        idn = self.driver.identify()
        self.assertIn("OE1022D", idn)

    def test_snapd(self):
        data = self.driver.snapd(1, 0, 1, 2, 3)
        self.assertIn("X", data)
        self.assertIn("Y", data)
        self.assertIn("R", data)
        self.assertIn("theta", data)
        # V to mV conversion
        self.assertAlmostEqual(data["X"], 1.0, places=3)

    def test_rall_parse(self):
        self.driver.start_rall_stream()
        raw = self.driver.read_rall_batch()
        self.assertEqual(len(raw), RALL_TOTAL_BYTES)
        batch = self.driver.parse_rall(raw)
        self.assertIn("lockin_A_X_mv", batch)
        self.assertEqual(len(batch["lockin_A_X_mv"]), SAMPLES_PER_BATCH)


class TestCircularBuffer(unittest.TestCase):
    def test_capacity_and_overwrite(self):
        from data.circular_buffer import CircularBuffer

        buf = CircularBuffer(["X", "Y"], capacity=5)
        for i in range(10):
            buf.append({"X": float(i), "Y": float(i * 2)})
        ts, vals = buf.get("X")
        self.assertEqual(len(vals), 5)
        self.assertEqual(vals[0], 5.0)
        self.assertEqual(vals[-1], 9.0)


if __name__ == "__main__":
    unittest.main()
