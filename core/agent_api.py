"""AI Agent 可编程接口

面向 AI Agent 的高级 Python API，隐藏命令细节。
所有方法最终转换为 Command 提交到 CommandService。

使用示例:
    api = AgentAPI(cmd_service)
    ok, msg, result = api.set_frequency(2.87e9)
    ok, msg, result = api.set_power(-20.0)
    ok, msg, result = api.start_acquisition("./experiments/run_001")
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

from core.command_service import CommandService
from core.commands import Command, CommandType


class AgentAPI:
    """AI Agent 可编程接口。

    所有方法返回 (success: bool, message: str, result: dict)。
    默认超时 5 秒，可通过 timeout_ms 参数调整。
    """

    def __init__(self, service: CommandService) -> None:
        self._svc = service

    # -- SMB100A 快捷方法 ----------------------------------------------------

    def set_frequency(self, freq_hz: float, timeout_ms: int = 5000) -> Tuple[bool, str, dict]:
        """设置 SMB100A CW 频率 (Hz)。"""
        cmd = Command(CommandType.SMB_SET_FREQUENCY, {"freq_hz": freq_hz}, source="agent")
        return self._svc.submit_sync(cmd, timeout_ms)

    def set_power(self, power_dbm: float, timeout_ms: int = 5000) -> Tuple[bool, str, dict]:
        """设置 SMB100A 输出功率 (dBm)。受安全限制约束。"""
        cmd = Command(CommandType.SMB_SET_POWER, {"power_dbm": power_dbm}, source="agent")
        return self._svc.submit_sync(cmd, timeout_ms)

    def set_output(self, enabled: bool, timeout_ms: int = 5000) -> Tuple[bool, str, dict]:
        """开关 SMB100A RF 输出。"""
        cmd = Command(CommandType.SMB_SET_OUTPUT, {"enabled": enabled}, source="agent")
        return self._svc.submit_sync(cmd, timeout_ms)

    def set_lf_output(
        self, enabled: bool, freq_hz: Optional[float] = None,
        amplitude_v: Optional[float] = None, timeout_ms: int = 5000
    ) -> Tuple[bool, str, dict]:
        """设置 SMB100A LF 输出。"""
        params: Dict[str, Any] = {"enabled": enabled}
        if freq_hz is not None:
            params["freq_hz"] = freq_hz
        if amplitude_v is not None:
            params["amplitude_v"] = amplitude_v
        cmd = Command(CommandType.SMB_SET_LF_OUTPUT, params, source="agent")
        return self._svc.submit_sync(cmd, timeout_ms)

    def set_sweep(
        self,
        start_hz: float,
        stop_hz: float,
        step_hz: float,
        dwell_ms: float,
        power_dbm: Optional[float] = None,
        timeout_ms: int = 5000,
    ) -> Tuple[bool, str, dict]:
        """配置 SMB100A 扫频参数。"""
        params: Dict[str, Any] = {
            "start_hz": start_hz,
            "stop_hz": stop_hz,
            "step_hz": step_hz,
            "dwell_ms": dwell_ms,
        }
        if power_dbm is not None:
            params["power_dbm"] = power_dbm
        cmd = Command(CommandType.SMB_SET_SWEEP, params, source="agent")
        return self._svc.submit_sync(cmd, timeout_ms)

    def start_sweep(self, timeout_ms: int = 5000) -> Tuple[bool, str, dict]:
        """启动 SMB100A 扫频。"""
        cmd = Command(CommandType.SMB_START_SWEEP, source="agent")
        return self._svc.submit_sync(cmd, timeout_ms)

    def stop_sweep(self, timeout_ms: int = 5000) -> Tuple[bool, str, dict]:
        """停止 SMB100A 扫频。"""
        cmd = Command(CommandType.SMB_STOP_SWEEP, source="agent")
        return self._svc.submit_sync(cmd, timeout_ms)

    def get_smb_status(self, timeout_ms: int = 5000) -> Tuple[bool, str, dict]:
        """获取 SMB100A 当前状态。"""
        cmd = Command(CommandType.SMB_QUERY_STATE, source="agent")
        return self._svc.submit_sync(cmd, timeout_ms)

    # -- OE1022D 快捷方法 ----------------------------------------------------

    def get_lockin_snapd(
        self, channel: int = 1, timeout_ms: int = 5000
    ) -> Tuple[bool, str, dict]:
        """获取 OE1022D 通道的 X/Y/R/theta。"""
        cmd = Command(CommandType.LOCKIN_QUERY_SNAPD, {"channel": channel}, source="agent")
        return self._svc.submit_sync(cmd, timeout_ms)

    def get_lockin_status(
        self, channel: int = 1, timeout_ms: int = 5000
    ) -> Tuple[bool, str, dict]:
        """获取 OE1022D 通道的状态（过载、PLL）。"""
        cmd = Command(CommandType.LOCKIN_QUERY_STATUS, {"channel": channel}, source="agent")
        return self._svc.submit_sync(cmd, timeout_ms)

    def set_lockin_time_constant(
        self, channel: int, index: int, timeout_ms: int = 5000
    ) -> Tuple[bool, str, dict]:
        """设置 OE1022D 时间常数。"""
        cmd = Command(
            CommandType.LOCKIN_SET_TIME_CONSTANT,
            {"channel": channel, "index": index},
            source="agent",
        )
        return self._svc.submit_sync(cmd, timeout_ms)

    def auto_gain(self, channel: int = 1, timeout_ms: int = 5000) -> Tuple[bool, str, dict]:
        """执行 OE1022D 自动增益。"""
        cmd = Command(CommandType.LOCKIN_AUTO_GAIN, {"channel": channel}, source="agent")
        return self._svc.submit_sync(cmd, timeout_ms)

    def auto_reserve(self, channel: int = 1, timeout_ms: int = 5000) -> Tuple[bool, str, dict]:
        """执行 OE1022D 自动动态储备。"""
        cmd = Command(CommandType.LOCKIN_AUTO_RESERVE, {"channel": channel}, source="agent")
        return self._svc.submit_sync(cmd, timeout_ms)

    def auto_phase(self, channel: int = 1, timeout_ms: int = 5000) -> Tuple[bool, str, dict]:
        """执行 OE1022D 自动相位。"""
        cmd = Command(CommandType.LOCKIN_AUTO_PHASE, {"channel": channel}, source="agent")
        return self._svc.submit_sync(cmd, timeout_ms)

    # -- 系统级快捷方法 ------------------------------------------------------

    def emergency_stop(self, timeout_ms: int = 5000) -> Tuple[bool, str, dict]:
        """执行系统级急停。"""
        cmd = Command(CommandType.SYS_EMERGENCY_STOP, source="agent")
        return self._svc.submit_sync(cmd, timeout_ms)

    def get_all_status(self, timeout_ms: int = 5000) -> Tuple[bool, str, dict]:
        """获取所有已连接设备的状态。"""
        cmd = Command(CommandType.SYS_QUERY_ALL_STATUS, source="agent")
        return self._svc.submit_sync(cmd, timeout_ms)

    # -- 高级组合操作 --------------------------------------------------------

    def run_odmr_sweep(
        self,
        start_freq_hz: float,
        stop_freq_hz: float,
        step_hz: float,
        dwell_ms: float,
        power_dbm: float,
        cycles: int = 1,
        timeout_ms: int = 30000,
    ) -> Tuple[bool, str, dict]:
        """执行一次完整的 ODMR 扫频实验（简化版）。

        实际扫频由 SweepEngine 管理，此处仅做参数配置演示。
        完整扫频需通过 GUI 或 SweepEngine 直接调用。
        """
        # 1. 配置扫频参数
        ok, msg, _ = self.set_sweep(start_freq_hz, stop_freq_hz, step_hz, dwell_ms, power_dbm)
        if not ok:
            return ok, msg, {}
        # 2. 启动扫频
        return self.start_sweep(timeout_ms)
