"""OE1022D 串口驱动

支持双模式：
- SNAPD? 实时监控（4 参数同步读取，无时间偏斜）
- RALL? 高速采集（20 参数 × 50 点/批次，二进制，仅 USB）

严格遵循操作手册：
- 命令终结符为 \r (0x0D)
- 多命令单行不超过 256 字符
- RALL? 仅 USB 2.0 可用
"""

from __future__ import annotations

import struct
import threading
import time
from typing import Callable, Dict, List, Optional, Tuple

try:
    import serial
except ImportError:
    serial = None  # type: ignore[assignment]

import numpy as np

# ── RALL? 参数定义 ──────────────────────────────────────────────
# 格式: (col_name, raw_unit, unit, scale, source, quantity, description, tags)
RALL_PARAMS: List[Tuple] = [
    ("lockin_A_X_mv",     "V",  "mV", 1000.0, "lockin_A", "X",     "CH-A 同相分量 X",       ["lockin", "channel_A", "quadrature", "primary"]),
    ("lockin_A_Y_mv",     "V",  "mV", 1000.0, "lockin_A", "Y",     "CH-A 正交分量 Y",       ["lockin", "channel_A", "quadrature", "primary"]),
    ("lockin_A_freq_hz",  "Hz", "Hz", 1.0,    "lockin_A", "freq",  "CH-A 参考频率",         ["lockin", "channel_A", "frequency"]),
    ("lockin_A_noise_mv", "V",  "mV", 1000.0, "lockin_A", "noise", "CH-A 噪声",             ["lockin", "channel_A", "noise"]),
    ("lockin_A_Xh1_mv",   "V",  "mV", 1000.0, "lockin_A", "Xh1",   "CH-A 1次谐波 X",        ["lockin", "channel_A", "harmonic"]),
    ("lockin_A_Yh1_mv",   "V",  "mV", 1000.0, "lockin_A", "Yh1",   "CH-A 1次谐波 Y",        ["lockin", "channel_A", "harmonic"]),
    ("lockin_A_Xh2_mv",   "V",  "mV", 1000.0, "lockin_A", "Xh2",   "CH-A 2次谐波 X",        ["lockin", "channel_A", "harmonic"]),
    ("lockin_A_Yh2_mv",   "V",  "mV", 1000.0, "lockin_A", "Yh2",   "CH-A 2次谐波 Y",        ["lockin", "channel_A", "harmonic"]),
    ("lockin_B_X_mv",     "V",  "mV", 1000.0, "lockin_B", "X",     "CH-B 同相分量 X",       ["lockin", "channel_B", "quadrature", "primary"]),
    ("lockin_B_Y_mv",     "V",  "mV", 1000.0, "lockin_B", "Y",     "CH-B 正交分量 Y",       ["lockin", "channel_B", "quadrature", "primary"]),
    ("lockin_B_freq_hz",  "Hz", "Hz", 1.0,    "lockin_B", "freq",  "CH-B 参考频率",         ["lockin", "channel_B", "frequency"]),
    ("lockin_B_noise_mv", "V",  "mV", 1000.0, "lockin_B", "noise", "CH-B 噪声",             ["lockin", "channel_B", "noise"]),
    ("lockin_B_Xh1_mv",   "V",  "mV", 1000.0, "lockin_B", "Xh1",   "CH-B 1次谐波 X",        ["lockin", "channel_B", "harmonic"]),
    ("lockin_B_Yh1_mv",   "V",  "mV", 1000.0, "lockin_B", "Yh1",   "CH-B 1次谐波 Y",        ["lockin", "channel_B", "harmonic"]),
    ("lockin_B_Xh2_mv",   "V",  "mV", 1000.0, "lockin_B", "Xh2",   "CH-B 2次谐波 X",        ["lockin", "channel_B", "harmonic"]),
    ("lockin_B_Yh2_mv",   "V",  "mV", 1000.0, "lockin_B", "Yh2",   "CH-B 2次谐波 Y",        ["lockin", "channel_B", "harmonic"]),
    ("aux_adc1_v",        "V",  "V",  1.0,    "aux",      "adc1",  "辅助 ADC 输入 1",       ["aux", "adc"]),
    ("aux_adc2_v",        "V",  "V",  1.0,    "aux",      "adc2",  "辅助 ADC 输入 2",       ["aux", "adc"]),
    ("aux_adc3_v",        "V",  "V",  1.0,    "aux",      "adc3",  "辅助 ADC 输入 3",       ["aux", "adc"]),
    ("aux_adc4_v",        "V",  "V",  1.0,    "aux",      "adc4",  "辅助 ADC 输入 4",       ["aux", "adc"]),
]

RALL_TOTAL_BYTES = 12288
SAMPLES_PER_BATCH = 50
RALL_PARAM_COUNT = len(RALL_PARAMS)

# SNAPD? 返回字段映射 (channel, param_index)
# SNAPD? 1,0,1,2,3  ->  CH-A: X=0, Y=1, R=2, θ=3
SNAPD_FIELDS = ["X", "Y", "R", "theta"]

TIME_CONSTANTS = {
    0: ("10 us", 0.00001),
    1: ("30 us", 0.00003),
    2: ("100 us", 0.0001),
    3: ("300 us", 0.0003),
    4: ("1 ms", 0.001),
    5: ("3 ms", 0.003),
    6: ("10 ms", 0.010),
    7: ("30 ms", 0.030),
    8: ("100 ms", 0.100),
    9: ("300 ms", 0.300),
    10: ("1 s", 1.0),
    11: ("3 s", 3.0),
    12: ("10 s", 10.0),
    13: ("30 s", 30.0),
    14: ("100 s", 100.0),
    15: ("300 s", 300.0),
    16: ("1 ks", 1000.0),
    17: ("3 ks", 3000.0),
}

SENSITIVITY_LABELS = {
    0: "1 nV", 1: "2 nV", 2: "5 nV", 3: "10 nV", 4: "20 nV", 5: "50 nV",
    6: "100 nV", 7: "200 nV", 8: "500 nV", 9: "1 uV", 10: "2 uV", 11: "5 uV",
    12: "10 uV", 13: "20 uV", 14: "50 uV", 15: "100 uV", 16: "200 uV", 17: "500 uV",
    18: "1 mV", 19: "2 mV", 20: "5 mV", 21: "10 mV", 22: "20 mV", 23: "50 mV",
    24: "100 mV", 25: "200 mV", 26: "500 mV", 27: "1 V",
}

FILTER_SLOPES = {0: "6 dB/oct", 1: "12 dB/oct", 2: "18 dB/oct", 3: "24 dB/oct"}
RESERVE_LABELS = {0: "Low", 1: "Normal", 2: "High"}


class OE1022DDriver:
    """OE1022D DSP Lock-In Amplifier serial driver."""

    DEFAULT_BAUDRATE = 921600
    DEFAULT_BYTESIZE = 8
    DEFAULT_PARITY = "N"
    DEFAULT_STOPBITS = 1
    DEFAULT_TIMEOUT = 2.0

    def __init__(self) -> None:
        self._serial: Optional[serial.Serial] = None
        self._lock = threading.RLock()
        self._cached_data: Dict[str, float] = {}
        self._raw_log_cb: Optional[Callable[[str, bytes], None]] = None
        self._transport = "rs232"

    # -- properties ----------------------------------------------------------

    @property
    def is_connected(self) -> bool:
        return self._serial is not None and self._serial.is_open

    @property
    def cached_data(self) -> Dict[str, float]:
        with self._lock:
            return self._cached_data.copy()

    @property
    def transport(self) -> str:
        return self._transport

    @property
    def rall_supported(self) -> bool:
        return self._transport == "usb2"

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
        transport: str = "rs232",
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
        self._transport = transport.lower()
        try:
            self._serial.reset_input_buffer()
            self._serial.reset_output_buffer()
            idn = self.identify()
            return idn
        except Exception:
            try:
                self._serial.close()
            except Exception:
                pass
            self._serial = None
            raise

    def close(self) -> None:
        with self._lock:
            if self._serial is not None:
                try:
                    self._serial.close()
                except Exception:
                    pass
                self._serial = None

    # -- class-level scan ----------------------------------------------------

    IDN_PATTERN = "SSI LIA-OE1022D"

    @staticmethod
    def scan_ports_with_idn(
        baudrate: int = DEFAULT_BAUDRATE,
        timeout: float = 0.5,
    ) -> list:
        """扫描所有串口，自动识别 OE1022D 设备。

        Returns:
            [(port_name, idn_string_or_empty), ...]
            idn_string 为空表示该端口不是 OE1022D 或无法通信。
        """
        if serial is None:
            raise RuntimeError("pyserial is not installed")
        from serial.tools import list_ports as _list_ports

        results = []
        for p in _list_ports.comports():
            port = p.device
            idn = ""
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
                s.write(b"*IDND?\r")
                resp = s.read_until(b"\r").decode("ascii", errors="ignore").strip()
                if OE1022DDriver.IDN_PATTERN in resp:
                    idn = resp
                s.close()
            except Exception:
                pass
            results.append((port, idn))
        return results

    # -- low-level I/O -------------------------------------------------------

    def _send(self, cmd: bytes) -> None:
        with self._lock:
            if self._serial is None or not self._serial.is_open:
                raise ConnectionError("OE1022D 未连接")
            self._serial.write(cmd)
            if self._raw_log_cb:
                try:
                    self._raw_log_cb("TX", cmd)
                except Exception:
                    pass

    def _readline(self) -> bytes:
        with self._lock:
            if self._serial is None or not self._serial.is_open:
                raise ConnectionError("OE1022D 未连接")
            line = self._serial.readline()
            if self._raw_log_cb:
                try:
                    self._raw_log_cb("RX", line)
                except Exception:
                    pass
            return line

    def _read_exact(self, n: int, timeout: float = 2.0) -> bytes:
        """读取恰好 n 字节，带超时保护。

        注意：不在读取过程中修改 self._serial.timeout，
        Windows 上修改 timeout 属性会重置 COM 端口内部缓冲区。
        使用构造时设定的 timeout 即可。
        """
        with self._lock:
            if self._serial is None or not self._serial.is_open:
                raise ConnectionError("OE1022D 未连接")
            data = b""
            while len(data) < n:
                chunk = self._serial.read(n - len(data))
                if not chunk:
                    break
                data += chunk
            return data

    def _exchange_ascii(self, cmd: str, read_timeout: float = 2.0) -> str:
        """发送 ASCII 命令并读取一行 ASCII 响应。

        注意：不在读取过程中修改 self._serial.timeout，
        Windows 上修改 timeout 属性会重置 COM 端口内部缓冲区。
        使用构造时设定的 timeout（默认 2.0s）即可。
        """
        with self._lock:
            # 手册要求终结符为 \r (0x0D)，但 \n 也可接受
            tx = (cmd + "\r").encode("ascii")
            self._send(tx)
            resp = self._serial.readline()
            if self._raw_log_cb:
                try:
                    self._raw_log_cb("RX", resp)
                except Exception:
                    pass
            # 去除 null 字节填充和空白字符
            text = resp.decode("ascii", errors="replace")
            text = text.replace("\x00", "").strip()
            return text

    # -- identification ------------------------------------------------------

    def identify(self) -> str:
        return self._exchange_ascii("*IDND?")

    # -- SNAPD? 实时监控 -----------------------------------------------------

    def snapd(self, channel: int = 1, *params: int) -> Dict[str, float]:
        """SNAPD? 同步读取多个参数。

        示例: snapd(1, 0, 1, 2, 3) -> CH-A 的 X, Y, R, theta
        手册 Section 5.2.9: 同一时间点记录，避免 OUTPD? 的延时偏斜。
        """
        if not params:
            params = (0, 1, 2, 3)
        args = ",".join(str(p) for p in (channel, *params))
        resp = self._exchange_ascii(f"SNAPD? {args}")
        parts = [p.strip() for p in resp.split(",")]
        result: Dict[str, float] = {}
        for i, val_str in enumerate(parts):
            try:
                val = float(val_str)
            except ValueError:
                continue
            if i < len(SNAPD_FIELDS):
                key = SNAPD_FIELDS[i]
                # 原始单位为 V，转为 mV（X/Y/R）；theta 为度，不变
                if key in ("X", "Y", "R"):
                    val *= 1000.0
                result[key] = val
        with self._lock:
            self._cached_data = result.copy()
        return result

    # -- RALL? 高速采集 ------------------------------------------------------

    def start_rall_stream(self) -> None:
        """发送第一个 RALL? 启动数据流。"""
        if not self.rall_supported:
            raise RuntimeError("RALL? is only available on the OE1022D USB2.0 interface, not RS232.")
        self._send(b"RALL?\r")

    def read_rall_batch(self, timeout: float = 2.0) -> bytes:
        """读取一批 RALL? 数据（12288 bytes）。"""
        return self._read_exact(RALL_TOTAL_BYTES, timeout)

    @staticmethod
    def parse_rall(raw: bytes) -> Dict[str, np.ndarray]:
        """解析 RALL? 返回的 12288 bytes。

        返回 dict: col_name -> np.ndarray(shape=(50,), dtype=float64)
        """
        if len(raw) < 8000:
            raise ValueError(f"RALL? 数据不完整: {len(raw)} < 8000 bytes")
        data: Dict[str, np.ndarray] = {}
        for i, (col_name, _raw_unit, _unit, scale, _source, _quantity, _desc, _tags) in enumerate(RALL_PARAMS):
            offset = i * 400  # 50 samples × 8 bytes
            samples = np.frombuffer(raw, dtype=">f8", count=SAMPLES_PER_BATCH, offset=offset).copy()
            data[col_name] = samples * scale
        return data

    @staticmethod
    def parse_rall_config(raw: bytes) -> Dict[str, Dict[str, object]]:
        """Parse the RALL? configuration snapshot region when available.

        The manual maps key CH-A bytes at offsets 8390/8391/8404/8405/8406 and
        status bytes at 8479..8481. CH-B uses the same block stride observed in
        the configuration table. Missing or short packets return empty channel
        dictionaries instead of failing the data path.
        """
        if len(raw) < 8482:
            return {"A": {}, "B": {}}

        def _byte(offset: int) -> int:
            if offset >= len(raw):
                return 0
            return raw[offset]

        def _channel(base_shift: int) -> Dict[str, object]:
            tc_idx = _byte(8404 + base_shift)
            return {
                "sensitivity_index": _byte(8390 + base_shift),
                "sensitivity": SENSITIVITY_LABELS.get(_byte(8390 + base_shift), str(_byte(8390 + base_shift))),
                "reserve_index": _byte(8391 + base_shift),
                "reserve": RESERVE_LABELS.get(_byte(8391 + base_shift), str(_byte(8391 + base_shift))),
                "time_constant_index": tc_idx,
                "time_constant": TIME_CONSTANTS.get(tc_idx, (str(tc_idx), 0.0))[0],
                "time_constant_s": TIME_CONSTANTS.get(tc_idx, ("", 0.0))[1],
                "filter_slope_index": _byte(8405 + base_shift),
                "filter_slope": FILTER_SLOPES.get(_byte(8405 + base_shift), str(_byte(8405 + base_shift))),
                "sync_filter": bool(_byte(8406 + base_shift)),
                "input_overload": bool(_byte(8479 + base_shift)),
                "gain_overload": bool(_byte(8480 + base_shift)),
                "pll_locked": bool(_byte(8481 + base_shift)),
            }

        return {"A": _channel(0), "B": _channel(96)}

    # -- status queries ------------------------------------------------------

    def get_input_overload(self, channel: int = 1) -> bool:
        """输入过载状态: 0=正常, 1=过载"""
        resp = self._exchange_ascii(f"INOVD? {channel}")
        return resp.strip() == "1"

    def get_gain_overload(self, channel: int = 1) -> bool:
        """增益过载状态"""
        resp = self._exchange_ascii(f"GNOVD? {channel}")
        return resp.strip() == "1"

    def get_pll_locked(self, channel: int = 1) -> bool:
        """PLL 锁定状态: 0=未锁定, 1=锁定"""
        resp = self._exchange_ascii(f"*PLLD? {channel}")
        return resp.strip() == "1"

    # -- auto setup ----------------------------------------------------------

    def auto_gain(self, channel: int = 1) -> None:
        """自动增益 AGAND"""
        self._exchange_ascii(f"AGAND {channel}")

    def auto_reserve(self, channel: int = 1) -> None:
        """自动动态储备 ARSVD"""
        self._exchange_ascii(f"ARSVD {channel}")

    def auto_phase(self, channel: int = 1) -> None:
        """自动相位 APHSD"""
        self._exchange_ascii(f"APHSD {channel}")

    # -- configuration helpers -----------------------------------------------

    def set_time_constant(self, channel: int = 1, index: int = 6) -> None:
        """设置时间常数，index: 4=1ms, 5=3ms, 6=10ms, ..., 10=1s"""
        self._exchange_ascii(f"OFLTD {channel},{index}")

    def set_filter_slope(self, channel: int = 1, index: int = 2) -> None:
        """滤波器滚降: 0=6dB, 1=12dB, 2=18dB, 3=24dB/oct"""
        self._exchange_ascii(f"OFSLD {channel},{index}")

    def set_sync_filter(self, channel: int = 1, on: bool = True) -> None:
        self._exchange_ascii(f"SYNCD {channel},{1 if on else 0}")

    def set_line_notch(self, channel: int = 1, mode: int = 1) -> None:
        """mode: 0=Off, 1=50Hz, 2=50+100Hz, 3=100Hz"""
        self._exchange_ascii(f"ILIND {channel},{mode}")

    def get_index(self, mnemonic: str, channel: int = 1, default: int = 0) -> int:
        resp = self._exchange_ascii(f"{mnemonic}? {channel}")
        try:
            return int(float(resp.split(",")[-1].strip()))
        except Exception:
            return default

    # -- INPUT / FILTERS -----------------------------------------------------

    def set_input_source(self, channel: int = 1, source: int = 0) -> None:
        """输入源: 0=A, 1=AB, 2=I(10^6), 3=I(10^8)"""
        self._exchange_ascii(f"ISRCD {channel},{source}")

    def set_current_gain(self, channel: int = 1, gain: int = 0) -> None:
        """Compatibility no-op.

        OE1022D folds current gain into ISRCD source modes:
        2 = 1 MOhm / 10E6 V/A, 3 = 100 MOhm / 10E8 V/A.
        """
        return None

    def set_grounding(self, channel: int = 1, ground: int = 0) -> None:
        """接地: 0=Float, 1=Ground"""
        self._exchange_ascii(f"IGNDD {channel},{ground}")

    def set_coupling(self, channel: int = 1, coupling: int = 0) -> None:
        """耦合: 0=AC, 1=DC"""
        self._exchange_ascii(f"ICPLD {channel},{coupling}")

    # -- REF / PHASE ---------------------------------------------------------

    def set_ref_phase(self, channel: int = 1, phase_deg: float = 0.0) -> None:
        """参考相位，单位度。"""
        self._exchange_ascii(f"PHASD {channel},{phase_deg:.2f}")

    def set_ref_source(self, channel: int = 1, source: int = 0) -> None:
        """参考源: 0=External, 1=Internal, 2=Internal Sweep"""
        self._exchange_ascii(f"FMODD {channel},{source}")

    def set_ref_slope(self, channel: int = 1, slope: int = 0) -> None:
        """外部参考触发方式（仅 External 模式有效）: 0=TTL Rising, 1=TTL Falling, 2=Sine Zero Cross"""
        self._exchange_ascii(f"RSLPD {channel},{slope}")

    def set_ref_frequency(self, channel: int = 1, freq_hz: float = 1000.0) -> None:
        """参考频率，单位 Hz（仅内部源有效）。"""
        self._exchange_ascii(f"FREQD {channel},{freq_hz:.6g}")

    def set_harmonic(self, channel: int = 1, harmonic: int = 1, slot: int = 1) -> None:
        """谐波次数: slot=1/2, harmonic=1~32767."""
        self._exchange_ascii(f"HARMD {channel},{slot},{harmonic}")

    # -- GAIN / TC -----------------------------------------------------------

    def set_sensitivity(self, channel: int = 1, index: int = 10) -> None:
        """灵敏度索引，详见手册。"""
        self._exchange_ascii(f"SENSD {channel},{index}")

    def set_reserve(self, channel: int = 1, reserve: int = 1) -> None:
        """动态储备: 0=Min, 1=Auto, 2=Max"""
        self._exchange_ascii(f"RMODD {channel},{reserve}")

    # -- CHANNEL OUTPUT ------------------------------------------------------

    def set_output_source(self, channel: int = 1, output_ch: int = 1, source: int = 0) -> None:
        """后面板 CH1/CH2 输出源选择，source 为手册 FPOPD 参数 k。"""
        self._exchange_ascii(f"FPOPD {output_ch},{source}")

    def set_output_offset(self, channel: int = 1, output_ch: int = 1, offset: float = 0.0, source: int = 0, expand: int = 1) -> None:
        """输出偏移百分比，必须和源/放大倍数一起通过 OEXPD 下发。"""
        self._exchange_ascii(f"OEXPD {output_ch},{source},{offset:.3f},{expand}")

    def set_output_expand(self, channel: int = 1, output_ch: int = 1, expand: int = 1, source: int = 0, offset: float = 0.0) -> None:
        """输出放大倍数 1..256，必须和源/偏移一起通过 OEXPD 下发。"""
        self._exchange_ascii(f"OEXPD {output_ch},{source},{offset:.3f},{expand}")

    def set_output_speed(self, channel: int = 1, output_ch: int = 1, speed: int = 0) -> None:
        """输出速率: 0=Slow, 1=Fast."""
        self._exchange_ascii(f"SPEDD {output_ch},{speed}")

    def set_aux_output_voltage(self, channel: int = 1, output_ch: int = 1, voltage_v: float = 0.0) -> None:
        self._exchange_ascii(f"CAUXD {output_ch},{voltage_v:.3f}")

    def configure_channel_output(
        self,
        output_ch: int = 1,
        source: int = 0,
        offset_pct: float = 0.0,
        expand: int = 1,
        speed: int = 0,
        aux_voltage_v: float = 0.0,
    ) -> None:
        """Configure CHOUT using the OE1022D manual command set."""
        self.set_output_source(output_ch=output_ch, source=source)
        if source == 34:
            self.set_aux_output_voltage(output_ch=output_ch, voltage_v=aux_voltage_v)
        else:
            self.set_output_offset(output_ch=output_ch, source=source, offset=offset_pct, expand=expand)
        self.set_output_speed(output_ch=output_ch, speed=speed)

    def set_sample_config(
        self,
        channel: int = 1,
        step_time_ms: float = 100.0,
        length: int = 1024,
        buffers: Tuple[int, int, int, int] = (0, 1, 2, 3),
        trigger_mode: int = 0,
        sample_mode: int = 0,
    ) -> None:
        """Configure OE1022D sample buffers per manual SRATD/SLEND/SSLED/STRGD/SPRMD."""
        if not 1 <= length <= 16384:
            raise ValueError("OE1022D sample length must be 1..16384")
        self._exchange_ascii(f"SRATD {channel},{step_time_ms:.3f}")
        self._exchange_ascii(f"SLEND {channel},{length}")
        for idx, param in enumerate(buffers, start=1):
            self._exchange_ascii(f"SSLED {channel},{idx},{param}")
        self._exchange_ascii(f"STRGD {channel},{trigger_mode}")
        self._exchange_ascii(f"SPRMD {channel},{sample_mode}")

    def query_config(self, channel: int = 1) -> Dict[str, object]:
        """Best-effort query of the main front-panel configuration."""
        config: Dict[str, object] = {"channel": channel}
        query_map = {
            "input_source": ("ISRCD", 0),
            "ground": ("IGNDD", 0),
            "coupling": ("ICPLD", 0),
            "line_notch": ("ILIND", 1),
            "ref_source": ("FMODD", 0),
            "ref_slope": ("RSLPD", 0),
            "sensitivity_index": ("SENSD", 10),
            "reserve_index": ("RMODD", 1),
            "time_constant_index": ("OFLTD", 6),
            "filter_slope_index": ("OFSLD", 2),
            "sync_filter": ("SYNCD", 1),
        }
        for key, (mnemonic, default) in query_map.items():
            config[key] = self.get_index(mnemonic, channel, default)
        tc_idx = int(config.get("time_constant_index", 6))
        sens_idx = int(config.get("sensitivity_index", 10))
        slope_idx = int(config.get("filter_slope_index", 2))
        reserve_idx = int(config.get("reserve_index", 1))
        config["time_constant"] = TIME_CONSTANTS.get(tc_idx, ("unknown", 0.0))[0]
        config["time_constant_s"] = TIME_CONSTANTS.get(tc_idx, ("", 0.0))[1]
        config["sensitivity"] = SENSITIVITY_LABELS.get(sens_idx, str(sens_idx))
        config["filter_slope"] = FILTER_SLOPES.get(slope_idx, str(slope_idx))
        config["reserve"] = RESERVE_LABELS.get(reserve_idx, str(reserve_idx))
        try:
            config["input_overload"] = self.get_input_overload(channel)
            config["gain_overload"] = self.get_gain_overload(channel)
            config["pll_locked"] = self.get_pll_locked(channel)
        except Exception:
            pass
        return config

    @staticmethod
    def display_interval_for_time_constant(index: int, min_ms: int = 50, max_ms: int = 300) -> int:
        tc_s = TIME_CONSTANTS.get(index, ("", 0.010))[1]
        if tc_s <= 0.030:
            return max(min_ms, 50)
        return int(min(max(tc_s * 1000.0 / 3.0, 100.0), float(max_ms)))

    # -- convenience ---------------------------------------------------------

    def get_rall_column_info(self) -> List[Dict]:
        """返回 RALL? 列的完整元数据，供 columns.json 生成使用。"""
        info = []
        for col_name, raw_unit, unit, scale, source, quantity, desc, tags in RALL_PARAMS:
            info.append({
                "name": col_name,
                "description": desc,
                "raw_unit": raw_unit,
                "unit": unit,
                "scale_from_raw": scale,
                "source": source,
                "quantity": quantity,
                "tags": tags,
            })
        return info
