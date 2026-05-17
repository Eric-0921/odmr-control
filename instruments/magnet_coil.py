from __future__ import annotations

import threading
import time
from typing import Callable, Optional

try:
    import serial
except ImportError:
    serial = None  # type: ignore[assignment]


class MagnetCoilController:
    """SCPI-like serial driver for a single-axis Helmholtz coil current source."""

    MAX_CURRENT_MA: float = 5000.0  # POWER_MAX_CURR in the original exe, in mA

    def __init__(self, axis: str = "X") -> None:
        self._axis = axis
        self._serial: Optional[serial.Serial] = None
        self._coil_constant: float = 143.26  # nT/mA
        self._zero_offset: float = 0.0  # mA
        self._lock_zero: bool = False
        self._output_on: bool = False
        self._current_lock = threading.Lock()
        self._cached_current: float = 0.0  # last polled value
        self._raw_log_cb: Optional[Callable[[str, str, bytes], None]] = None

    # -- properties ----------------------------------------------------------

    @property
    def axis(self) -> str:
        return self._axis

    @property
    def is_connected(self) -> bool:
        return self._serial is not None and self._serial.is_open

    @property
    def coil_constant(self) -> float:
        return self._coil_constant

    @coil_constant.setter
    def coil_constant(self, value: float) -> None:
        self._coil_constant = value

    @property
    def zero_offset(self) -> float:
        return self._zero_offset

    @zero_offset.setter
    def zero_offset(self, value: float) -> None:
        self._zero_offset = value

    @property
    def lock_zero(self) -> bool:
        return self._lock_zero

    @lock_zero.setter
    def lock_zero(self, value: bool) -> None:
        self._lock_zero = value

    @property
    def output_on(self) -> bool:
        return self._output_on

    @property
    def current_mA(self) -> float:
        with self._current_lock:
            return self._cached_current

    def update_cached_current(self, ma: float) -> None:
        with self._current_lock:
            self._cached_current = ma

    def set_raw_log_callback(self, cb: Optional[Callable[[str, str, bytes], None]]) -> None:
        self._raw_log_cb = cb

    # -- connection ----------------------------------------------------------

    def connect(self, port: str, baudrate: int = 9600, timeout: float = 0.1) -> str:
        if serial is None:
            raise RuntimeError("pyserial is not installed")
        self._serial = serial.Serial(
            port=port,
            baudrate=baudrate,
            bytesize=8,
            parity="N",
            stopbits=1,
            timeout=timeout,
            dsrdtr=False,
        )
        self._serial.dtr = True
        self._serial.reset_input_buffer()
        self._serial.reset_output_buffer()
        return self.identify()

    def close(self) -> None:
        if self._serial is not None:
            try:
                if self._output_on:
                    self.set_current(0.0)
                    self.set_output(False)
                self.set_local()
            except Exception:
                pass
            finally:
                try:
                    self._serial.close()
                except Exception:
                    pass
                self._serial = None
                self._output_on = False

    # -- low-level I/O -------------------------------------------------------

    def exchange(self, command: str, timeout: float = 0.1) -> str:
        if self._serial is None or not self._serial.is_open:
            raise ConnectionError(f"[{self._axis}] 串口未连接")
        tx_data = (command + "\n").encode("ascii")
        self._serial.write(tx_data)
        self._serial.flush()
        if self._raw_log_cb:
            try:
                self._raw_log_cb(self._axis, "TX", tx_data)
            except Exception:
                pass
        time.sleep(0.02)
        old_timeout = self._serial.timeout
        self._serial.timeout = timeout
        try:
            raw = self._serial.readline()
        finally:
            self._serial.timeout = old_timeout
        if self._raw_log_cb:
            try:
                self._raw_log_cb(self._axis, "RX", raw)
            except Exception:
                pass
        return raw.decode("ascii", errors="replace").strip()

    # -- SCPI commands -------------------------------------------------------

    def identify(self) -> str:
        return self.exchange("*IDN?")

    def set_current(self, ma: float) -> None:
        # The original exe accepts a total current in mA, sends amps to the PSU,
        # and applies Math.Abs at the final write boundary.
        ma = min(abs(ma), self.MAX_CURRENT_MA)
        amps = ma / 1000.0
        self.exchange(f"CURR {amps:.5f}")

    def get_current(self) -> float:
        resp = self.exchange("MEAS:CURR?")
        try:
            return float(resp) * 1000.0
        except ValueError:
            raise ValueError(f"[{self._axis}] 无法解析电流值: {resp!r}")

    def set_output(self, on: bool) -> None:
        self.exchange(f"OUTP {1 if on else 0}")
        self._output_on = on

    def get_output_state(self) -> bool:
        resp = self.exchange("OUTP?")
        return resp.strip() == "1"

    def set_voltage(self, v: float = 75.0) -> None:
        self.exchange(f"VOLT {v:.0f}")

    def set_remote(self) -> None:
        self.exchange("SYST:REM")

    def set_local(self) -> None:
        self.exchange("SYST:LOC")
