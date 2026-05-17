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

from pathlib import Path
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

    def query_smb_config(self, timeout_ms: int = 5000) -> Tuple[bool, str, dict]:
        """读取 SMB100A 面板分组配置快照。"""
        cmd = Command(CommandType.SMB_QUERY_CONFIG, source="agent")
        return self._svc.submit_sync(cmd, timeout_ms)

    def apply_smb_config(self, config: Dict[str, Any], timeout_ms: int = 5000) -> Tuple[bool, str, dict]:
        """按统一配置对象应用 SMB100A CW/LF/sweep/modulation 设置。"""
        cmd = Command(CommandType.SMB_APPLY_CONFIG, config, source="agent")
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

    def query_lockin_config(
        self, channel: int = 1, timeout_ms: int = 5000
    ) -> Tuple[bool, str, dict]:
        """读取 OE1022D 指定通道的结构化配置快照。"""
        cmd = Command(CommandType.LOCKIN_QUERY_CONFIG, {"channel": channel}, source="agent")
        return self._svc.submit_sync(cmd, timeout_ms)

    def apply_lockin_config(
        self, config: Dict[str, Any], timeout_ms: int = 5000
    ) -> Tuple[bool, str, dict]:
        """应用 OE1022D input/ref/gain_tc/output/auto 配置对象。"""
        cmd = Command(CommandType.LOCKIN_APPLY_CONFIG, config, source="agent")
        return self._svc.submit_sync(cmd, timeout_ms)

    def start_lockin_acquisition(
        self, timeout_ms: int = 5000
    ) -> Tuple[bool, str, dict]:
        """启动 OE1022D RALL? 采集流，不写文件。"""
        cmd = Command(CommandType.LOCKIN_START_ACQUIRE, source="agent")
        return self._svc.submit_sync(cmd, timeout_ms)

    def stop_lockin_acquisition(
        self, timeout_ms: int = 5000
    ) -> Tuple[bool, str, dict]:
        """停止 OE1022D RALL? 采集流。"""
        cmd = Command(CommandType.LOCKIN_STOP_ACQUIRE, source="agent")
        return self._svc.submit_sync(cmd, timeout_ms)

    def start_recording(
        self, output_dir: str = "./experiments", timeout_ms: int = 5000
    ) -> Tuple[bool, str, dict]:
        """启动 RALL? 采集并写入 CSV。"""
        cmd = Command(
            CommandType.ACQ_START_RECORDING,
            {"output_dir": output_dir},
            source="agent",
        )
        return self._svc.submit_sync(cmd, timeout_ms)

    def stop_recording(
        self, stop_acquire: bool = True, timeout_ms: int = 5000
    ) -> Tuple[bool, str, dict]:
        """停止 CSV 记录，可选择是否同时停止 RALL? 采集流。"""
        cmd = Command(
            CommandType.ACQ_STOP_RECORDING,
            {"stop_acquire": stop_acquire},
            source="agent",
        )
        return self._svc.submit_sync(cmd, timeout_ms)

    def get_acquisition_state(
        self, timeout_ms: int = 5000
    ) -> Tuple[bool, str, dict]:
        """查询 RALL? 采集和记录状态。"""
        cmd = Command(CommandType.ACQ_QUERY_STATE, source="agent")
        return self._svc.submit_sync(cmd, timeout_ms)

    # -- 磁场控制快捷方法 ----------------------------------------------------

    def connect_magnetic_axis(
        self,
        axis: str,
        port: str,
        baudrate: int = 9600,
        coil_constant: Optional[float] = None,
        zero_offset_mA: Optional[float] = None,
        timeout_ms: int = 5000,
    ) -> Tuple[bool, str, dict]:
        """连接单个磁场轴并可选应用线圈常数/零偏。"""
        params: Dict[str, Any] = {"axis": axis, "port": port, "baudrate": baudrate}
        if coil_constant is not None:
            params["coil_constant"] = coil_constant
        if zero_offset_mA is not None:
            params["zero_offset_mA"] = zero_offset_mA
        cmd = Command(CommandType.MAG_CONNECT_AXIS, params, source="agent")
        return self._svc.submit_sync(cmd, timeout_ms)

    def set_magnetic_field(
        self, axis: str, field_nT: float, timeout_ms: int = 5000
    ) -> Tuple[bool, str, dict]:
        """设置单轴目标磁场 (nT)。"""
        cmd = Command(CommandType.MAG_SET_FIELD, {"axis": axis, "field_nT": field_nT}, source="agent")
        return self._svc.submit_sync(cmd, timeout_ms)

    def set_magnetic_field_3d(
        self,
        x_nT: float = 0.0,
        y_nT: float = 0.0,
        z_nT: float = 0.0,
        timeout_ms: int = 5000,
    ) -> Tuple[bool, str, dict]:
        """设置三轴目标磁场 (nT)。"""
        cmd = Command(
            CommandType.MAG_SET_FIELD_3D,
            {"x_nT": x_nT, "y_nT": y_nT, "z_nT": z_nT},
            source="agent",
        )
        return self._svc.submit_sync(cmd, timeout_ms)

    def set_magnetic_output(
        self, axis: str, enabled: bool, timeout_ms: int = 5000
    ) -> Tuple[bool, str, dict]:
        """开关单轴磁场电源输出。"""
        cmd = Command(CommandType.MAG_SET_OUTPUT, {"axis": axis, "enabled": enabled}, source="agent")
        return self._svc.submit_sync(cmd, timeout_ms)

    def lock_magnetic_zero(
        self, axis: str, locked: bool, timeout_ms: int = 5000
    ) -> Tuple[bool, str, dict]:
        """切换单轴零偏叠加锁定。"""
        cmd = Command(CommandType.MAG_LOCK_ZERO, {"axis": axis, "locked": locked}, source="agent")
        return self._svc.submit_sync(cmd, timeout_ms)

    def capture_magnetic_background(
        self, axis: str, timeout_ms: int = 5000
    ) -> Tuple[bool, str, dict]:
        """回读单轴当前电源电流并保存为背景零偏。"""
        cmd = Command(CommandType.MAG_CAPTURE_BACKGROUND, {"axis": axis}, source="agent")
        return self._svc.submit_sync(cmd, timeout_ms)

    def prepare_magnetic_zero_lock(
        self, axis: str, capture_readback: bool = False, timeout_ms: int = 5000
    ) -> Tuple[bool, str, dict]:
        """按连接-输出零偏-可选回读-锁零工作流准备单轴磁场。"""
        cmd = Command(
            CommandType.MAG_PREPARE_ZERO_LOCK,
            {"axis": axis, "capture_readback": capture_readback},
            source="agent",
        )
        return self._svc.submit_sync(cmd, timeout_ms)

    def get_magnetic_state(
        self, axis: Optional[str] = None, timeout_ms: int = 5000
    ) -> Tuple[bool, str, dict]:
        """查询磁场状态。axis 为空时返回三轴快照。"""
        params = {"axis": axis} if axis else {}
        cmd = Command(CommandType.MAG_QUERY_STATE, params, source="agent")
        return self._svc.submit_sync(cmd, timeout_ms)

    def scan_magnetic_ports(self, timeout_ms: int = 10000) -> Tuple[bool, str, dict]:
        """扫描串口并读取磁场电源 IDN。"""
        cmd = Command(CommandType.MAG_SCAN_PORTS, source="agent")
        return self._svc.submit_sync(cmd, timeout_ms)

    def auto_detect_magnetic_axes(
        self, bindings: Optional[Dict[str, Any]] = None, timeout_ms: int = 10000
    ) -> Tuple[bool, str, dict]:
        """按 IDN 绑定自动匹配 X/Y/Z 磁场电源。"""
        params = {"bindings": bindings} if bindings is not None else {}
        cmd = Command(CommandType.MAG_AUTO_DETECT, params, source="agent")
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

    # -- 统一实验 JSON -------------------------------------------------------

    def load_experiment_json(
        self, path_or_dict: str | Path | Dict[str, Any], timeout_ms: int = 5000
    ) -> Tuple[bool, str, dict]:
        """加载统一实验 JSON 文件或 dict。"""
        params = {"plan": path_or_dict} if isinstance(path_or_dict, dict) else {"path": str(path_or_dict)}
        cmd = Command(CommandType.EXPERIMENT_LOAD_JSON, params, source="agent")
        return self._svc.submit_sync(cmd, timeout_ms)

    def validate_experiment_plan(
        self, plan: Optional[Dict[str, Any]] = None, timeout_ms: int = 5000
    ) -> Tuple[bool, str, dict]:
        """校验统一实验 JSON。plan 为空时校验已加载计划。"""
        params = {"plan": plan} if plan is not None else {}
        cmd = Command(CommandType.EXPERIMENT_VALIDATE, params, source="agent")
        return self._svc.submit_sync(cmd, timeout_ms)

    def start_experiment(
        self, plan: Optional[Dict[str, Any]] = None, timeout_ms: int = 5000
    ) -> Tuple[bool, str, dict]:
        """启动统一实验序列。plan 为空时使用已加载计划。"""
        params = {"plan": plan} if plan is not None else {}
        cmd = Command(CommandType.EXPERIMENT_START, params, source="agent")
        return self._svc.submit_sync(cmd, timeout_ms)

    def pause_experiment(self, timeout_ms: int = 5000) -> Tuple[bool, str, dict]:
        """暂停当前实验序列。"""
        cmd = Command(CommandType.EXPERIMENT_PAUSE, source="agent")
        return self._svc.submit_sync(cmd, timeout_ms)

    def resume_experiment(self, timeout_ms: int = 5000) -> Tuple[bool, str, dict]:
        """继续当前实验序列。"""
        cmd = Command(CommandType.EXPERIMENT_RESUME, source="agent")
        return self._svc.submit_sync(cmd, timeout_ms)

    def stop_experiment(self, timeout_ms: int = 5000) -> Tuple[bool, str, dict]:
        """停止当前实验序列并触发默认安全策略。"""
        cmd = Command(CommandType.EXPERIMENT_STOP, source="agent")
        return self._svc.submit_sync(cmd, timeout_ms)

    def get_experiment_state(self, timeout_ms: int = 5000) -> Tuple[bool, str, dict]:
        """查询统一实验序列状态。"""
        cmd = Command(CommandType.EXPERIMENT_QUERY_STATE, source="agent")
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
