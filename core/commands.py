"""命令定义层

所有对设备的操作均封装为可序列化的 Command 对象，
支持 GUI 和 AI Agent 统一调用。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Dict


class CommandType(Enum):
    """设备操作命令类型枚举。"""

    # ===== SMB100A 连接与基础控制 =====
    SMB_CONNECT = auto()              # {protocol, address, timeout_ms}
    SMB_DISCONNECT = auto()
    SMB_SET_FREQUENCY = auto()        # {freq_hz}
    SMB_SET_POWER = auto()            # {power_dbm}
    SMB_SET_OUTPUT = auto()           # {enabled: bool}
    SMB_SET_LF_OUTPUT = auto()        # {enabled: bool, freq_hz, amplitude_v}
    SMB_SET_LF_FREQ = auto()          # {freq_hz}
    SMB_SET_LF_VOLTAGE = auto()       # {mv}
    SMB_SET_LF_SHAPE = auto()         # {shape}
    SMB_SET_MODULATION = auto()       # {mod_type, enabled, depth/freq/...}
    SMB_SET_FM_DEVIATION = auto()     # {hz}
    SMB_SET_SWEEP = auto()            # {start_hz, stop_hz, step_hz, dwell_ms, power_dbm}
    SMB_START_SWEEP = auto()
    SMB_STOP_SWEEP = auto()
    SMB_QUERY_STATE = auto()          # {} -> 返回完整状态字典
    SMB_EMERGENCY_STOP = auto()

    # ===== OE1022D 连接与基础控制 =====
    LOCKIN_CONNECT = auto()           # {port, baudrate, bytesize, parity, stopbits, timeout}
    LOCKIN_DISCONNECT = auto()
    LOCKIN_SET_INPUT = auto()         # {channel, source, gain, ground, coupling, notch}
    LOCKIN_SET_REF_PHASE = auto()     # {channel, phase_deg, source, slope, freq_hz, harmonic}
    LOCKIN_SET_GAIN_TC = auto()       # {channel, sensitivity, reserve, time_const, filter_db, sync}
    LOCKIN_SET_OUTPUT = auto()        # {channel, source, offset, expand, voltage}
    LOCKIN_SET_SAMPLE = auto()        # {step_time_ms, length, trigger_mode, sample_mode}
    LOCKIN_AUTO_GAIN = auto()         # {channel}
    LOCKIN_AUTO_RESERVE = auto()      # {channel}
    LOCKIN_AUTO_PHASE = auto()        # {channel}
    LOCKIN_QUERY_SNAPD = auto()       # {channel} -> X,Y,R,theta
    LOCKIN_QUERY_STATUS = auto()      # {channel} -> overload, pll, etc.
    LOCKIN_START_ACQUIRE = auto()     # {recorder_id}
    LOCKIN_STOP_ACQUIRE = auto()
    LOCKIN_SET_TIME_CONSTANT = auto() # {channel, index}
    LOCKIN_SET_FILTER_SLOPE = auto()  # {channel, index}
    LOCKIN_SET_SYNC_FILTER = auto()   # {channel, on}
    LOCKIN_SET_LINE_NOTCH = auto()    # {channel, mode}

    # ===== 激光器 =====
    LASER_CONNECT = auto()           # {port, baudrate, bytesize, parity, stopbits, timeout}
    LASER_DISCONNECT = auto()
    LASER_SET_POWER = auto()         # {power_mw: int}
    LASER_SET_OUTPUT = auto()        # {enabled: bool}
    LASER_QUERY_STATE = auto()       # {} -> 返回缓存状态
    LASER_EMERGENCY_STOP = auto()

    # ===== 采集 =====
    ACQ_SET_SAMPLING = auto()         # {interval_ms}
    ACQ_START_RECORDING = auto()      # {output_dir, filename_prefix}
    ACQ_STOP_RECORDING = auto()
    ACQ_QUERY_STATE = auto()

    # ===== 系统 =====
    SYS_EMERGENCY_STOP = auto()
    SYS_QUERY_ALL_STATUS = auto()     # 查询所有已连接设备状态


@dataclass(frozen=True)
class Command:
    """标准化命令对象，可序列化为 JSON。"""

    cmd_type: CommandType
    params: Dict[str, Any] = field(default_factory=dict)
    request_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    source: str = "gui"               # "gui" | "agent" | "system"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "cmd_type": self.cmd_type.name,
            "params": self.params,
            "request_id": self.request_id,
            "source": self.source,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Command":
        return cls(
            cmd_type=CommandType[d["cmd_type"]],
            params=d.get("params", {}),
            request_id=d.get("request_id", ""),
            source=d.get("source", "gui"),
        )

    def __repr__(self) -> str:
        p = ", ".join(f"{k}={v!r}" for k, v in self.params.items())
        return f"Command({self.cmd_type.name}, {p}, source={self.source!r})"
