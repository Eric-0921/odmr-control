"""SMB100A VISA 驱动

封装所有 SCPI 命令，内置多层安全校验：
- 频率范围硬限制（100 kHz ~ 12.75 GHz）
- 功率范围查询与校验
- 扫频参数交叉验证（start < stop, step > 0）
- 线程安全状态缓存
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Optional

try:
    import pyvisa
except ImportError:
    pyvisa = None  # type: ignore[assignment]


class SMB100ADriver:
    """R&S SMB100A RF/Microwave Signal Generator SCPI driver."""

    # 硬件规格限制（依具体型号可调）
    MIN_FREQ_HZ: float = 100e3      # 100 kHz
    MAX_FREQ_HZ: float = 12.75e9    # 12.75 GHz
    MIN_POWER_DBM: float = -120.0
    MAX_POWER_DBM: float = 30.0

    def __init__(self) -> None:
        self._resource_manager: Optional[pyvisa.ResourceManager] = None
        self._instrument: Optional[pyvisa.resources.Resource] = None
        self._lock = threading.Lock()
        self._cached_freq_hz: float = 0.0
        self._cached_power_dbm: float = -30.0
        self._cached_output_on: bool = False
        self._cached_mode: str = "CW"  # "CW" or "SWEEP"
        self._cached_lf_on: bool = False
        self._cached_mod_on: bool = False
        self._raw_log_cb: Optional[Callable[[str, bytes], None]] = None

    # -- properties ----------------------------------------------------------

    @property
    def is_connected(self) -> bool:
        with self._lock:
            return self._instrument is not None

    @property
    def cached_freq_hz(self) -> float:
        with self._lock:
            return self._cached_freq_hz

    @property
    def cached_power_dbm(self) -> float:
        with self._lock:
            return self._cached_power_dbm

    @property
    def cached_output_on(self) -> bool:
        with self._lock:
            return self._cached_output_on

    @property
    def cached_mode(self) -> str:
        with self._lock:
            return self._cached_mode

    def set_raw_log_callback(self, cb: Optional[Callable[[str, bytes], None]]) -> None:
        self._raw_log_cb = cb

    # -- connection ----------------------------------------------------------

    def connect(self, visa_address: str, timeout_ms: int = 10000) -> str:
        if pyvisa is None:
            raise RuntimeError("pyvisa is not installed")
        self._resource_manager = pyvisa.ResourceManager()
        self._instrument = self._resource_manager.open_resource(visa_address)
        self._instrument.timeout = timeout_ms
        self._instrument.write_termination = "\n"
        self._instrument.read_termination = "\n"
        self._instrument.clear()
        self._instrument.write("*CLS")
        idn = self._instrument.query("*IDN?")
        # 初始化安全状态
        self._instrument.write("OUTP OFF")
        self._instrument.write("FREQ:MODE CW")
        self._update_cached_state()
        return idn.strip()

    def close(self) -> None:
        """安全断开：先关输出，再切 CW，最后释放资源。"""
        if self._instrument is not None:
            try:
                self._instrument.write("OUTP OFF")
                self._instrument.write("FREQ:MODE CW")
            except Exception:
                pass
            finally:
                try:
                    self._instrument.close()
                except Exception:
                    pass
                self._instrument = None
        if self._resource_manager is not None:
            try:
                self._resource_manager.close()
            except Exception:
                pass
            self._resource_manager = None
        with self._lock:
            self._cached_output_on = False
            self._cached_mode = "CW"

    # -- class-level scan ----------------------------------------------------

    @staticmethod
    def scan_rs_devices(timeout_ms: int = 500) -> list:
        """自动扫描所有 VISA 资源，返回罗德施瓦茨设备列表。

        Returns:
            [(visa_address, idn_string), ...]
        """
        if pyvisa is None:
            raise RuntimeError("pyvisa is not installed")
        rm = pyvisa.ResourceManager()
        devices = []
        for addr in rm.list_resources():
            try:
                instr = rm.open_resource(addr)
                instr.timeout = timeout_ms
                instr.write_termination = "\n"
                instr.read_termination = "\n"
                idn = instr.query("*IDN?").strip()
                if "Rohde&Schwarz" in idn or "SMB" in idn:
                    devices.append((addr, idn))
                instr.close()
            except Exception:
                try:
                    instr.close()
                except Exception:
                    pass
        rm.close()
        return devices

    # -- low-level I/O -------------------------------------------------------

    def _write(self, cmd: str) -> None:
        with self._lock:
            if self._instrument is None:
                raise ConnectionError("SMB100A 未连接")
            self._instrument.write(cmd)
            if self._raw_log_cb:
                try:
                    self._raw_log_cb("TX", (cmd + "\n").encode("ascii"))
                except Exception:
                    pass

    def _query(self, cmd: str) -> str:
        with self._lock:
            if self._instrument is None:
                raise ConnectionError("SMB100A 未连接")
            result = self._instrument.query(cmd)
            if self._raw_log_cb:
                try:
                    self._raw_log_cb("TX", (cmd + "\n").encode("ascii"))
                    self._raw_log_cb("RX", result.encode("ascii", errors="replace"))
                except Exception:
                    pass
            return result.strip()

    # -- state cache ---------------------------------------------------------

    def _update_cached_state(self) -> None:
        """从设备读取当前状态并缓存。"""
        try:
            freq = float(self._query("FREQ:CW?"))
            power = float(self._query("POW:LEV?"))
            outp = self._query("OUTP?").strip()
            mode = self._query("FREQ:MODE?").strip()
            lf_on = False
            mod_on = False
            try:
                lf_on = self._query("SOUR:LFO:STAT?").strip() in ("1", "ON")
                mod_on = self._query("SOUR:MOD:ALL:STAT?").strip() in ("1", "ON")
            except Exception:
                pass
            with self._lock:
                self._cached_freq_hz = freq
                self._cached_power_dbm = power
                self._cached_output_on = outp in ("1", "ON")
                self._cached_mode = mode
                self._cached_lf_on = lf_on
                self._cached_mod_on = mod_on
        except Exception as exc:
            logging.warning(f"[SMB100A] 状态缓存更新失败: {exc}")

    def refresh_state(self) -> None:
        self._update_cached_state()

    # -- validators ----------------------------------------------------------

    def validate_frequency(self, hz: float) -> bool:
        if not (self.MIN_FREQ_HZ <= hz <= self.MAX_FREQ_HZ):
            raise ValueError(
                f"频率 {hz:.3e} Hz 超出范围 [{self.MIN_FREQ_HZ:.3e}, {self.MAX_FREQ_HZ:.3e}]"
            )
        return True

    def validate_power(self, dbm: float) -> bool:
        if not (self.MIN_POWER_DBM <= dbm <= self.MAX_POWER_DBM):
            raise ValueError(
                f"功率 {dbm:.1f} dBm 超出范围 [{self.MIN_POWER_DBM}, {self.MAX_POWER_DBM}]"
            )
        return True

    def validate_sweep_params(self, start_hz: float, stop_hz: float, step_hz: float) -> bool:
        self.validate_frequency(start_hz)
        self.validate_frequency(stop_hz)
        if start_hz >= stop_hz:
            raise ValueError(f"起始频率必须小于终止频率: {start_hz:.3e} >= {stop_hz:.3e}")
        if step_hz <= 0:
            raise ValueError(f"扫频步进必须为正: {step_hz}")
        return True

    # -- SCPI commands -------------------------------------------------------

    def identify(self) -> str:
        return self._query("*IDN?")

    def reset(self) -> None:
        self._write("*RST")
        time.sleep(0.2)

    def set_freq_cw(self, hz: float) -> None:
        self.validate_frequency(hz)
        with self._lock:
            if self._cached_mode == "SWEEP":
                raise RuntimeError("扫频模式下禁止设置 CW 频率")
        self._write(f"FREQ:CW {hz:.3f}")
        with self._lock:
            self._cached_freq_hz = hz

    def get_freq_cw(self) -> float:
        return float(self._query("FREQ:CW?"))

    def set_power(self, dbm: float) -> None:
        self.validate_power(dbm)
        self._write(f"POW:LEV {dbm:.2f} dBm")
        with self._lock:
            self._cached_power_dbm = dbm

    def get_power(self) -> float:
        return float(self._query("POW:LEV?"))

    def set_output(self, on: bool) -> None:
        self._write(f"OUTP {'ON' if on else 'OFF'}")
        with self._lock:
            self._cached_output_on = on

    def get_output(self) -> bool:
        return self._query("OUTP?").strip() in ("1", "ON")

    # -- sweep ---------------------------------------------------------------

    def set_sweep_start(self, hz: float) -> None:
        self.validate_frequency(hz)
        self._write(f"FREQ:START {hz:.3f}")

    def set_sweep_stop(self, hz: float) -> None:
        self.validate_frequency(hz)
        self._write(f"FREQ:STOP {hz:.3f}")

    def set_sweep_step(self, hz: float) -> None:
        if hz <= 0:
            raise ValueError(f"步进必须为正: {hz}")
        self._write(f"SWE:STEP:LIN {hz:.3f}")

    def set_sweep_dwell(self, ms: float) -> None:
        if ms < 0:
            raise ValueError(f"驻留时间不能为负: {ms}")
        self._write(f"SWE:DWELL {ms:.1f}ms")

    def set_sweep_mode(self, mode: str = "AUTO") -> None:
        """mode: AUTO | SINGLE | STEP"""
        self._write(f"SWE:FREQ:MODE {mode}")

    def set_freq_mode(self, mode: str) -> None:
        """mode: CW | SWEEP"""
        self._write(f"SOUR:FREQ:MODE {mode}")
        with self._lock:
            self._cached_mode = mode

    def get_freq_mode(self) -> str:
        return self._query("FREQ:MODE?").strip()

    def get_lf_output(self) -> bool:
        """查询 LF 输出状态。"""
        return self._query("SOUR:LFO:STAT?").strip() in ("1", "ON")

    def get_lf_freq(self) -> float:
        """查询 LF 频率 (Hz)。"""
        try:
            return float(self._query("SOUR:LFO:FREQ?"))
        except Exception:
            return 0.0

    def get_modulation_state(self) -> bool:
        """查询调制总状态。"""
        return self._query("SOUR:MOD:ALL:STAT?").strip() in ("1", "ON")

    def get_lf_mode(self) -> str:
        """查询 LF 模式（CW / SWEEP）。"""
        return self._query("SOUR:LFO:FREQ:MODE?").strip()

    def execute_single_sweep(self) -> None:
        self._write("SWE:FREQ:EXEC")

    def set_sweep_spacing(self, spacing: str = "LIN") -> None:
        """spacing: LIN | LOG"""
        self._write(f"SWE:SPAC {spacing}")

    def set_sweep_shape(self, shape: str = "SAW") -> None:
        """shape: SAW | TRI."""
        self._write(f"SWE:SHAP {shape}")

    def set_sweep_retrace(self, on: bool = True) -> None:
        self._write(f"SWE:RETR {'ON' if on else 'OFF'}")

    def set_sweep_trigger_source(self, source: str = "IMM") -> None:
        self._write(f"SWE:TRIG:SOUR {source}")

    def get_sweep_running(self) -> bool:
        return self._query("SWE:RUNN?").strip() in ("1", "ON")

    # -- LF output -----------------------------------------------------------

    def set_lf_output(self, on: bool) -> None:
        self._write(f"SOUR:LFO:STAT {'ON' if on else 'OFF'}")
        with self._lock:
            self._cached_lf_on = on

    def set_lf_freq(self, hz: float) -> None:
        self._write(f"SOUR:LFO:FREQ {hz:.3f}Hz")

    def set_lf_voltage(self, mv: float) -> None:
        """注意：手册中单位为 mV，SCPI 中用 V。"""
        volts = mv / 1000.0
        self._write(f"SOUR:LFO:VOLT {volts:.6f}V")

    def set_lf_shape(self, shape: str) -> None:
        """shape: SINE | SQUARE | TRIANGLE | SAWTOOTH | ISAWTOOTH"""
        self._write(f"SOUR:LFO:SHAP {shape}")

    def set_lf_impedance(self, imp: str = "LOW") -> None:
        self._write(f"SOUR:LFO:IMP {imp}")

    def set_lf_sweep(self, start_hz: float, stop_hz: float, step_hz: float,
                     shape: str = "SAW", spacing: str = "LIN", trigger: str = "IMM") -> None:
        self._write(f"SOUR:LFO:FREQ:MODE SWE")
        self._write(f"SOUR:LFO:FREQ:STAR {start_hz:.3f}Hz")
        self._write(f"SOUR:LFO:FREQ:STOP {stop_hz:.3f}Hz")
        self._write(f"SOUR:LFO:SWE:STEP {step_hz:.3f}Hz")
        self._write(f"SOUR:LFO:SWE:SHAP {shape}")
        self._write(f"SOUR:LFO:SWE:SPAC {spacing}")
        self._write(f"SOUR:LFO:SWE:TRIG:SOUR {trigger}")

    # -- FM modulation -------------------------------------------------------

    def set_fm_state(self, on: bool) -> None:
        self._write(f"SOUR:FM:STAT {'ON' if on else 'OFF'}")
        with self._lock:
            self._cached_mod_on = on

    def set_fm_deviation(self, hz: float) -> None:
        self._write(f"SOUR:FM:DEV {hz:.3f}Hz")

    def set_fm_source(self, source: str = "INT") -> None:
        self._write(f"SOUR:FM:SOUR {source}")

    def set_am_state(self, on: bool) -> None:
        self._write(f"SOUR:AM:STAT {'ON' if on else 'OFF'}")
        with self._lock:
            self._cached_mod_on = on

    def set_am_depth(self, pct: float) -> None:
        if not 0 <= pct <= 100:
            raise ValueError("AM depth must be 0..100 %")
        self._write(f"SOUR:AM:DEPT {pct:.3f}PCT")

    def set_am_source(self, source: str = "INT") -> None:
        self._write(f"SOUR:AM:SOUR {source}")

    def set_phase(self, deg: float) -> None:
        self._write(f"SOUR:PHAS {deg:.6f}DEG")

    def set_level_offset(self, db: float) -> None:
        self._write(f"POW:OFFS {db:.3f}DB")

    def query_config(self) -> dict:
        """Best-effort snapshot aligned with the SMB100A front-panel groups."""
        result = {
            "frequency_hz": self.cached_freq_hz,
            "power_dbm": self.cached_power_dbm,
            "rf_output": self.cached_output_on,
            "mode": self.cached_mode,
        }
        queries = {
            "lf_output": "SOUR:LFO:STAT?",
            "lf_frequency_hz": "SOUR:LFO:FREQ?",
            "lf_shape": "SOUR:LFO:SHAP?",
            "lf_impedance": "SOUR:LFO:IMP?",
            "modulation": "SOUR:MOD:ALL:STAT?",
            "fm_state": "SOUR:FM:STAT?",
            "fm_deviation_hz": "SOUR:FM:DEV?",
            "am_state": "SOUR:AM:STAT?",
            "am_depth_pct": "SOUR:AM:DEPT?",
            "sweep_running": "SWE:RUNN?",
        }
        for key, cmd in queries.items():
            try:
                value = self._query(cmd)
                if key.endswith("_hz") or key.endswith("_pct"):
                    result[key] = float(value)
                elif key.endswith("_output") or key.endswith("_state") or key in ("modulation", "sweep_running"):
                    result[key] = value.strip() in ("1", "ON")
                else:
                    result[key] = value
            except Exception:
                pass
        return result

    # -- convenience ---------------------------------------------------------

    def configure_sweep(self, start_hz: float, stop_hz: float, step_hz: float,
                        dwell_ms: float, power_dbm: float) -> None:
        """一次性配置扫频参数（含安全校验）。"""
        self.validate_sweep_params(start_hz, stop_hz, step_hz)
        self.validate_power(power_dbm)
        self.set_power(power_dbm)
        self.set_sweep_start(start_hz)
        self.set_sweep_stop(stop_hz)
        self.set_sweep_step(step_hz)
        self.set_sweep_dwell(dwell_ms)
        self.set_sweep_spacing("LIN")
        self.set_sweep_mode("AUTO")

    def start_sweep(self) -> None:
        """启动扫频：切到 SWEEP 模式并执行。"""
        self.set_freq_mode("SWEEP")
        time.sleep(0.05)
        self.execute_single_sweep()

    def stop_sweep(self) -> None:
        """停止扫频：切回 CW 模式。"""
        self.set_freq_mode("CW")
        with self._lock:
            self._cached_mode = "CW"

    def emergency_stop(self) -> None:
        """急停：关闭 RF、LF、FM，切回 CW。"""
        try:
            self._write("OUTP OFF")
            self._write("SOUR:LFO:STAT OFF")
            self._write("SOUR:FM:STAT OFF")
            self._write("SOUR:AM:STAT OFF")
            self._write("FREQ:MODE CW")
        except Exception:
            pass
        with self._lock:
            self._cached_output_on = False
            self._cached_mode = "CW"
            self._cached_lf_on = False
            self._cached_mod_on = False
