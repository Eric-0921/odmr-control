"""MSL-U 系列激光器驱动

RS232 协议（9600/8N1），十六进制指令，单向发送无查询响应。
软件端缓存功率和输出状态。

指令格式：
- 设置功率: 55 AA 05 01 <H> <L> <CHK>
  - H/L = 功率值(十进制→十六进制) 的高/低字节
  - CHK = (05 + 01 + H + L) & 0xFF
- 关闭: 55 AA 03 00 03
- 开启: 55 AA 03 01 04
"""

from __future__ import annotations

import threading
from typing import Callable, Dict, List, Optional, Tuple

try:
    import serial
except ImportError:
    serial = None  # type: ignore[assignment]


class LaserMSLDriver:
    """MSL-U 激光器 RS232 驱动。"""

    DEFAULT_BAUDRATE = 9600
    DEFAULT_BYTESIZE = 8
    DEFAULT_PARITY = "N"
    DEFAULT_STOPBITS = 1
    DEFAULT_TIMEOUT = 1.0

    # 固定指令
    _CMD_OFF = b"\x55\xAA\x03\x00\x03"
    _CMD_ON = b"\x55\xAA\x03\x01\x04"

    def __init__(
        self,
        max_power_mw: int = 150,
        wavelength_nm: float = 532.12457,
        serial_number: str = "",
        model: str = "",
    ) -> None:
        self._serial: Optional[serial.Serial] = None  # type: ignore[name-defined]
        self._lock = threading.Lock()
        self._raw_log_cb: Optional[Callable[[str, bytes], None]] = None

        self._max_power_mw = max_power_mw
        self._wavelength_nm = wavelength_nm
        self._serial_number = serial_number
        self._model = model

        self._cached_power_mw: float = 0.0
        self._cached_output_on: bool = False

    # -- properties ----------------------------------------------------------

    @property
    def is_connected(self) -> bool:
        with self._lock:
            return self._serial is not None and self._serial.is_open

    @property
    def cached_power_mw(self) -> float:
        with self._lock:
            return self._cached_power_mw

    @property
    def cached_output_on(self) -> bool:
        with self._lock:
            return self._cached_output_on

    @property
    def max_power_mw(self) -> int:
        return self._max_power_mw

    @property
    def wavelength_nm(self) -> float:
        return self._wavelength_nm

    # -- callback ------------------------------------------------------------

    def set_raw_log_callback(self, cb: Optional[Callable[[str, bytes], None]]) -> None:
        self._raw_log_cb = cb

    # -- connection ----------------------------------------------------------

    def connect(
        self,
        port: str,
        baudrate: int = DEFAULT_BAUDRATE,
        bytesize: int = DEFAULT_BYTESIZE,
        parity: str = DEFAULT_PARITY,
        stopbits: int = DEFAULT_STOPBITS,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> str:
        if serial is None:
            raise RuntimeError("pyserial is not installed")
        self._serial = serial.Serial(
            port=port,
            baudrate=baudrate,
            bytesize=bytesize,
            parity=parity,
            stopbits=stopbits,
            timeout=timeout,
        )
        try:
            self._serial.reset_input_buffer()
            self._serial.reset_output_buffer()
        except Exception:
            try:
                self._serial.close()
            except Exception:
                pass
            self._serial = None
            raise
        # 激光器无 IDN 命令，返回配置中的标识
        idn = f"MSL-U Laser,SN:{self._serial_number},Model:{self._model}"
        return idn

    def close(self) -> None:
        with self._lock:
            if self._serial is not None:
                try:
                    self._serial.write(self._CMD_OFF)
                except Exception:
                    pass
                try:
                    self._serial.close()
                except Exception:
                    pass
                self._serial = None
            self._cached_output_on = False

    # -- class-level scan ----------------------------------------------------

    @staticmethod
    def scan_ports(
        baudrate: int = DEFAULT_BAUDRATE,
        timeout: float = 0.5,
    ) -> List[Tuple[str, str]]:
        """扫描所有串口，返回可用端口列表。

        激光器无 IDN 命令，无法验证设备身份，
        仅报告端口是否可达（发送关闭指令不报错）。
        """
        if serial is None:
            raise RuntimeError("pyserial is not installed")
        from serial.tools import list_ports as _list_ports

        results: List[Tuple[str, str]] = []
        for p in _list_ports.comports():
            port = p.device
            try:
                s = serial.Serial(
                    port=port,
                    baudrate=baudrate,
                    bytesize=8,
                    parity="N",
                    stopbits=1,
                    timeout=timeout,
                )
                s.reset_input_buffer()
                s.reset_output_buffer()
                s.write(LaserMSLDriver._CMD_OFF)
                s.close()
                results.append((port, "Laser (unverified)"))
            except Exception:
                pass
        return results

    # -- low-level I/O -------------------------------------------------------

    def _send(self, data: bytes) -> None:
        with self._lock:
            if self._serial is None or not self._serial.is_open:
                raise ConnectionError("Laser 未连接")
            self._serial.write(data)
            if self._raw_log_cb:
                try:
                    self._raw_log_cb("TX", data)
                except Exception:
                    pass

    # -- protocol ------------------------------------------------------------

    @staticmethod
    def _build_set_power_cmd(power_mw: int) -> bytes:
        """构造设置功率指令。

        指令格式: 55 AA 05 01 <H> <L> <CHK>
        CHK = (05 + 01 + H + L) & 0xFF
        """
        high = (power_mw >> 8) & 0xFF
        low = power_mw & 0xFF
        chk = (0x05 + 0x01 + high + low) & 0xFF
        return bytes([0x55, 0xAA, 0x05, 0x01, high, low, chk])

    # -- public API ----------------------------------------------------------

    def set_power(self, power_mw: int) -> None:
        """设置激光功率 (mW)。"""
        if power_mw < 0 or power_mw > self._max_power_mw:
            raise ValueError(
                f"功率 {power_mw} mW 超出范围 [0, {self._max_power_mw}] mW"
            )
        cmd = self._build_set_power_cmd(power_mw)
        self._send(cmd)
        with self._lock:
            self._cached_power_mw = float(power_mw)

    def set_output(self, on: bool) -> None:
        """开关激光输出。"""
        cmd = self._CMD_ON if on else self._CMD_OFF
        self._send(cmd)
        with self._lock:
            self._cached_output_on = on

    def get_power(self) -> float:
        with self._lock:
            return self._cached_power_mw

    def get_output(self) -> bool:
        with self._lock:
            return self._cached_output_on

    def emergency_stop(self) -> bool:
        """急停：关闭激光输出。返回是否成功。"""
        for _ in range(3):
            try:
                self.set_output(False)
                return True
            except Exception:
                pass
        return False
