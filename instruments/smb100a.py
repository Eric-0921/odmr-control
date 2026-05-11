from __future__ import annotations

from dataclasses import dataclass
from typing import Any

try:
    import pyvisa
except ImportError:  # Allows non-hardware unit checks before dependencies are installed.
    pyvisa = None


POWER_MIN_DBM = -20.0
POWER_MAX_DBM = 18.0
POWER_NEAR_LIMIT_DB = 2.0
RF_MIN_HZ = 100_000.0
SWEEP_DWELL_MIN_MS = 2.0
SWEEP_DWELL_MAX_MS = 100_000.0


def parse_frequency_to_hz(value: str | float | int | None, *, require_unit: bool = False) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)

    text = str(value).strip().upper().replace(" ", "")
    if not text:
        return None

    units = (
        ("GHZ", 1e9),
        ("MHZ", 1e6),
        ("KHZ", 1e3),
        ("HZ", 1.0),
    )
    for suffix, multiplier in units:
        if text.endswith(suffix):
            return float(text[: -len(suffix)]) * multiplier
    if require_unit:
        raise ValueError("Frequency value requires an explicit unit")
    return float(text)


def parse_time_to_ms(value: str | float | int | None, *, require_unit: bool = False) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        # SMB100A dwell queries usually return seconds.
        numeric = float(value)
        return numeric * 1000 if numeric <= 100 else numeric

    text = str(value).strip().upper().replace(" ", "")
    if not text:
        return None
    if text.endswith("MS"):
        return float(text[:-2])
    if text.endswith("S"):
        return float(text[:-1]) * 1000
    if require_unit:
        raise ValueError("Time value requires an explicit unit")
    numeric = float(text)
    return numeric * 1000 if numeric <= 100 else numeric


def parse_power_dbm(value: str | float | int | None) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().upper().replace("DBM", "").replace(" ", "")
    if not text:
        return None
    return float(text)


def parse_voltage_to_mv(value: str | float | int | None, *, require_unit: bool = False) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        # SMB100A voltage queries usually return volts.
        numeric = float(value)
        return numeric * 1000 if abs(numeric) <= 20 else numeric

    text = str(value).strip().upper().replace(" ", "")
    if not text:
        return None
    if text.endswith("MV"):
        return float(text[:-2])
    if text.endswith("V"):
        return float(text[:-1]) * 1000
    if require_unit:
        raise ValueError("Voltage value requires an explicit unit")
    numeric = float(text)
    return numeric * 1000 if abs(numeric) <= 20 else numeric


def format_frequency(hz: float | None) -> str:
    if hz is None:
        return ""
    if abs(hz) >= 1e9:
        return f"{hz / 1e9:.9g} GHz"
    if abs(hz) >= 1e6:
        return f"{hz / 1e6:.9g} MHz"
    if abs(hz) >= 1e3:
        return f"{hz / 1e3:.9g} kHz"
    return f"{hz:.9g} Hz"


def format_float(value: float | None, suffix: str = "") -> str:
    if value is None:
        return ""
    return f"{value:.9g}{suffix}"


def normalize_state(value: Any) -> bool | None:
    if value is None:
        return None
    text = str(value).strip().upper()
    if text in {"1", "ON", "TRUE"}:
        return True
    if text in {"0", "OFF", "FALSE"}:
        return False
    return None


@dataclass
class SMBParameters:
    power_dbm: float | None = None
    cw_hz: float | None = None
    start_hz: float | None = None
    stop_hz: float | None = None
    step_hz: float | None = None
    dwell_ms: float | None = None
    lf_freq_hz: float | None = None
    lf_amp_mv: float | None = None
    lf_shape: str | None = None
    fm_dev_hz: float | None = None
    rf_output: bool | None = None
    lf_output: bool | None = None
    fm_state: bool | None = None
    power_override: bool = False

    def as_comparable(self) -> dict[str, Any]:
        return {
            "power_dbm": self.power_dbm,
            "cw_hz": self.cw_hz,
            "start_hz": self.start_hz,
            "stop_hz": self.stop_hz,
            "step_hz": self.step_hz,
            "dwell_ms": self.dwell_ms,
            "lf_freq_hz": self.lf_freq_hz,
            "lf_amp_mv": self.lf_amp_mv,
            "lf_shape": self.lf_shape.upper() if self.lf_shape else None,
            "fm_dev_hz": self.fm_dev_hz,
            "rf_output": self.rf_output,
            "lf_output": self.lf_output,
            "fm_state": self.fm_state,
        }


@dataclass
class SMBError:
    code: int
    message: str

    @property
    def is_error(self) -> bool:
        return self.code != 0


class SMBCommandError(RuntimeError):
    def __init__(self, command: str, errors: list[SMBError]) -> None:
        self.command = command
        self.errors = errors
        details = "; ".join(f"{error.code}: {error.message}" for error in errors)
        super().__init__(f"SMB100A command failed after `{command}`: {details}")


@dataclass
class PowerSafety:
    power_dbm: float | None
    within_limits: bool
    near_limit: bool
    override_enabled: bool
    blocked_reason: str | None = None


class SMB100AController:
    def __init__(self) -> None:
        self.rm = None
        self.instrument = None

    @property
    def is_connected(self) -> bool:
        return self.instrument is not None

    def connect(self, address: str) -> str:
        if pyvisa is None:
            raise RuntimeError("pyvisa is not installed; run `pip install -r requirements.txt` first")
        self.rm = pyvisa.ResourceManager()
        self.instrument = self.rm.open_resource(address)
        self.instrument.timeout = 10000
        self.instrument.write_termination = "\n"
        self.instrument.read_termination = "\n"
        self.instrument.clear()
        self.write("*CLS")
        return self.query("*IDN?")

    def close(self, rf_off: bool = True) -> None:
        if self.instrument is not None:
            try:
                if rf_off:
                    self.write("OUTP OFF")
                self.instrument.close()
            finally:
                self.instrument = None
        if self.rm is not None:
            try:
                self.rm.close()
            except Exception:
                pass
            self.rm = None

    def write(self, command: str) -> None:
        if self.instrument is None:
            raise RuntimeError("SMB100A is not connected")
        self.instrument.write(command)

    def write_checked(self, command: str) -> list[SMBError]:
        self.write(command)
        errors = self.query_errors()
        failures = [error for error in errors if error.is_error]
        if failures:
            raise SMBCommandError(command, failures)
        return errors

    def query(self, command: str) -> str:
        if self.instrument is None:
            raise RuntimeError("SMB100A is not connected")
        return self.instrument.query(command).strip()

    def query_optional(self, command: str) -> str | None:
        try:
            return self.query(command)
        except Exception:
            return None

    def query_errors(self) -> list[SMBError]:
        if self.instrument is None:
            return []
        errors: list[SMBError] = []
        count_text = self.query_optional("SYSTEM:ERROR:COUNT?")
        try:
            count = int(float(count_text)) if count_text is not None else None
        except ValueError:
            count = None

        if count is None:
            parsed = parse_smb_error(self.query_optional("SYST:ERR?"))
            return [parsed] if parsed is not None else []

        if count == 0:
            return []

        for _ in range(count):
            parsed = parse_smb_error(self.query_optional("SYST:ERR?"))
            if parsed is None:
                break
            errors.append(parsed)
            if parsed.code == 0:
                break
        return errors

    def read_parameters(self) -> SMBParameters:
        return SMBParameters(
            power_dbm=parse_power_dbm(self.query_optional("POW:LEV?")),
            cw_hz=parse_frequency_to_hz(self.query_optional("FREQ:CW?")),
            start_hz=parse_frequency_to_hz(self.query_optional("FREQ:START?")),
            stop_hz=parse_frequency_to_hz(self.query_optional("FREQ:STOP?")),
            step_hz=parse_frequency_to_hz(self.query_optional("SWE:STEP?")),
            dwell_ms=parse_time_to_ms(self.query_optional("SWE:DWELL?")),
            lf_freq_hz=parse_frequency_to_hz(self.query_optional("SOUR:LFO:FREQ?")),
            lf_amp_mv=parse_voltage_to_mv(self.query_optional("SOUR:LFO:VOLT?")),
            lf_shape=self.query_optional("SOUR:LFO:SHAP?"),
            fm_dev_hz=parse_frequency_to_hz(self.query_optional("SOUR:FM:DEV?")),
            rf_output=normalize_state(self.query_optional("OUTP?")),
            lf_output=normalize_state(self.query_optional("SOUR:LFO:STAT?")),
            fm_state=normalize_state(self.query_optional("SOUR:FM:STAT?")),
        )

    def current_frequency_hz(self) -> float:
        return float(self.query("FREQ?"))

    def apply_common_parameters(self, params: SMBParameters) -> None:
        validate_common_parameters(params)
        if params.power_dbm is not None:
            self.write_checked(f"POW:LEV {params.power_dbm:.12g} DBM")
        if params.lf_amp_mv is not None:
            self.write_checked(f"SOUR:LFO:VOLT {params.lf_amp_mv:.12g} mV")
        if params.lf_freq_hz is not None:
            self.write_checked(f"SOUR:LFO:FREQ {params.lf_freq_hz:.12g} Hz")
        if params.lf_shape:
            self.write_checked(f"SOUR:LFO:SHAP {params.lf_shape}")
        self.write_checked("SOUR:LFO:SIMP LOW")
        if params.fm_dev_hz is not None:
            self.write_checked(f"SOUR:FM:DEV {params.fm_dev_hz:.12g} Hz")
        if params.rf_output is not None:
            self.write_checked(f"OUTP {'ON' if params.rf_output else 'OFF'}")
        if params.lf_output is not None:
            self.write_checked(f"SOUR:LFO:STAT {'ON' if params.lf_output else 'OFF'}")
        if params.fm_state is not None:
            self.write_checked(f"SOUR:FM:STAT {'ON' if params.fm_state else 'OFF'}")

    def apply_sweep_parameters(self, params: SMBParameters) -> None:
        validate_sweep_parameters(params)
        self.apply_common_parameters(params)
        if params.start_hz is not None:
            self.write_checked(f"FREQ:START {params.start_hz:.12g} Hz")
        if params.stop_hz is not None:
            self.write_checked(f"FREQ:STOP {params.stop_hz:.12g} Hz")
        self.write_checked("SWE:SPAC LIN")
        if params.step_hz is not None:
            self.write_checked(f"SWE:STEP {params.step_hz:.12g} Hz")
        if params.dwell_ms is not None:
            self.write_checked(f"SWE:DWELL {params.dwell_ms:.12g} MS")
        self.write_checked("SWE:MODE AUTO")
        self.write_checked("FREQ:MODE SWE")

    def apply_cw_parameters(self, params: SMBParameters) -> None:
        validate_cw_parameters(params)
        self.apply_common_parameters(params)
        if params.cw_hz is not None:
            self.write_checked(f"FREQ:CW {params.cw_hz:.12g} Hz")

    def set_frequency_mode_cw(self) -> None:
        self.write_checked("FREQ:MODE CW")


def evaluate_power_safety(power_dbm: float | None, override_enabled: bool = False) -> PowerSafety:
    if power_dbm is None:
        return PowerSafety(power_dbm=None, within_limits=True, near_limit=False, override_enabled=override_enabled)
    within = POWER_MIN_DBM <= power_dbm <= POWER_MAX_DBM
    near = (
        within
        and (
            power_dbm <= POWER_MIN_DBM + POWER_NEAR_LIMIT_DB
            or power_dbm >= POWER_MAX_DBM - POWER_NEAR_LIMIT_DB
        )
    )
    reason = None
    if not within and not override_enabled:
        reason = f"Power {power_dbm:.3g} dBm is outside the allowed -20..+18 dBm range"
    return PowerSafety(
        power_dbm=power_dbm,
        within_limits=within,
        near_limit=near,
        override_enabled=override_enabled,
        blocked_reason=reason,
    )


def validate_power(power_dbm: float | None, override_enabled: bool = False) -> PowerSafety:
    safety = evaluate_power_safety(power_dbm, override_enabled)
    if safety.blocked_reason:
        raise ValueError(safety.blocked_reason)
    return safety


def validate_common_parameters(params: SMBParameters) -> None:
    validate_power(params.power_dbm, params.power_override)
    if params.lf_freq_hz is not None and params.lf_freq_hz <= 0:
        raise ValueError("LF frequency must be positive")
    if params.lf_amp_mv is not None and params.lf_amp_mv < 0:
        raise ValueError("LF amplitude must be non-negative")
    if params.fm_dev_hz is not None and params.fm_dev_hz < 0:
        raise ValueError("FM deviation must be non-negative")


def validate_cw_parameters(params: SMBParameters) -> None:
    validate_common_parameters(params)
    if params.cw_hz is not None and params.cw_hz < RF_MIN_HZ:
        raise ValueError("SMB100A RF frequency must be at least 100 kHz")


def validate_sweep_parameters(params: SMBParameters) -> None:
    validate_common_parameters(params)
    for label, hz in [("start frequency", params.start_hz), ("stop frequency", params.stop_hz), ("step frequency", params.step_hz)]:
        if hz is not None and hz < RF_MIN_HZ:
            raise ValueError(f"SMB100A {label} must be at least 100 kHz")
    if params.step_hz is not None and params.step_hz <= 0:
        raise ValueError("SMB100A sweep step must be positive")
    if params.dwell_ms is not None and not (SWEEP_DWELL_MIN_MS <= params.dwell_ms <= SWEEP_DWELL_MAX_MS):
        raise ValueError("SMB100A sweep dwell must be between 2 ms and 100 s")


def parse_smb_error(response: str | None) -> SMBError | None:
    if response is None:
        return None
    text = response.strip()
    if not text:
        return None
    if "," in text:
        code_text, message = text.split(",", 1)
    else:
        parts = text.split(maxsplit=1)
        code_text = parts[0]
        message = parts[1] if len(parts) > 1 else ""
    try:
        code = int(float(code_text.strip()))
    except ValueError:
        code = -1
        message = text
    return SMBError(code=code, message=message.strip().strip('"'))
