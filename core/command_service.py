"""命令执行服务层

所有对设备的操作（GUI / AI Agent）均通过此类统一下发。
- 单线程串行执行，避免 VISA/Serial 并发冲突
- 安全校验在命令路由层执行（如功率限制）
- 订阅底层 InstrumentController 信号并统一广播
"""

from __future__ import annotations

import queue
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from PyQt5.QtCore import QObject, QThread, pyqtSignal

from core.commands import Command, CommandType
from core.instrument_controller import InstrumentController
from core.timestamp_sync import TimestampSyncHub, TimestampedSample
from data.recorder import ODMRRecorder


class SafetyError(Exception):
    """安全策略拒绝的命令。"""
    pass


class CommandService(QObject):
    """统一的设备命令执行入口。GUI 和 AI Agent 的唯一操作通道。"""

    # 命令执行结果信号（所有调用方均可订阅）
    command_completed = pyqtSignal(str, bool, str, dict)
    # request_id, success: bool, message: str, result: dict

    command_error = pyqtSignal(str, str)
    # request_id, error_message

    # 设备状态广播（统一出口）
    smb_state_broadcast = pyqtSignal(dict)
    lockin_data_broadcast = pyqtSignal(dict)
    lockin_status_broadcast = pyqtSignal(dict)
    lockin_batch_broadcast = pyqtSignal(dict)
    laser_state_broadcast = pyqtSignal(dict)

    # 日志和错误转发
    log_requested = pyqtSignal(str)
    error_occurred = pyqtSignal(str)

    def __init__(
        self,
        controller: InstrumentController,
        config: Optional[Dict[str, Any]] = None,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self._ctrl = controller
        self._config = config or {}
        self._cmd_queue: queue.Queue = queue.Queue()
        self._worker_thread: Optional[QThread] = None
        self._running = False
        self._acq_recorder: Optional[ODMRRecorder] = None

        # 时间戳同步中心
        self._ts_hub = TimestampSyncHub()
        self._ts_hub.register_source("smb", poll_interval_ms=100)
        self._ts_hub.register_source("lockin", poll_interval_ms=50)

        # 同步等待机制：submit_sync 用
        self._sync_lock = threading.Lock()
        self._sync_results: Dict[str, Tuple[bool, str, dict]] = {}
        self._sync_events: Dict[str, threading.Event] = {}

        # 订阅底层信号并重新广播
        self._ctrl.smb_state_changed.connect(self._on_smb_state_changed)
        self._ctrl.smb_state_dict_changed.connect(self.smb_state_broadcast.emit)
        self._ctrl.lockin_data_ready.connect(self._on_lockin_data_ready)
        self._ctrl.lockin_status_changed.connect(self._on_lockin_status_changed)
        self._ctrl.lockin_batch_ready.connect(self._on_lockin_batch_ready)
        self._ctrl.laser_state_changed.connect(self.laser_state_broadcast.emit)
        self._ctrl.error_occurred.connect(self.error_occurred.emit)
        self._ctrl.log_requested.connect(self.log_requested.emit)

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> None:
        """启动命令处理工作线程。

        CommandService 自身 moveToThread，确保 _process_loop 在工作线程中执行，
        而非阻塞 GUI 主线程。构造时 parent 必须为 None。
        """
        if self._worker_thread is not None and self._worker_thread.isRunning():
            return
        self._running = True
        self._worker_thread = QThread()
        self.moveToThread(self._worker_thread)
        self._worker_thread.started.connect(self._process_loop)
        self._worker_thread.finished.connect(self._worker_thread.deleteLater)
        self._worker_thread.start()

    def stop(self) -> None:
        """停止命令处理工作线程。"""
        self._running = False
        # 放入一个空命令唤醒线程
        self._cmd_queue.put(None)
        if self._worker_thread is not None:
            if not self._worker_thread.wait(2000):
                self._worker_thread.terminate()
                self._worker_thread.wait(1000)
            self._worker_thread = None

    # -- public API ----------------------------------------------------------

    def submit(self, cmd: Command) -> str:
        """异步提交命令，返回 request_id。结果通过 signal 回调。"""
        self._cmd_queue.put(cmd)
        return cmd.request_id

    def submit_sync(
        self, cmd: Command, timeout_ms: int = 5000
    ) -> Tuple[bool, str, dict]:
        """同步执行命令（阻塞），用于 AI Agent 或需要立即返回的场景。

        Returns:
            (success: bool, message: str, result: dict)
        """
        event = threading.Event()
        with self._sync_lock:
            self._sync_events[cmd.request_id] = event
            self._sync_results[cmd.request_id] = (False, "timeout", {})

        self._cmd_queue.put(cmd)
        ok = event.wait(timeout_ms / 1000.0)

        with self._sync_lock:
            result = self._sync_results.pop(cmd.request_id, (False, "lost", {}))
            self._sync_events.pop(cmd.request_id, None)

        if not ok:
            return False, f"命令超时 ({timeout_ms}ms)", {}
        return result

    # -- worker loop ---------------------------------------------------------

    def _process_loop(self) -> None:
        """单线程处理循环：串行执行所有命令。"""
        while self._running:
            try:
                cmd = self._cmd_queue.get(timeout=0.1)
            except queue.Empty:
                continue
            if cmd is None:
                break
            self._handle_command(cmd)

    def _handle_command(self, cmd: Command) -> None:
        """处理单条命令：执行 + 结果分发。"""
        try:
            result = self._execute(cmd)
            self._deliver_result(cmd.request_id, True, "OK", result)
        except SafetyError as exc:
            self._deliver_result(cmd.request_id, False, f"[安全限制] {exc}", {})
        except Exception as exc:
            self._deliver_result(cmd.request_id, False, str(exc), {})

    def _deliver_result(self, request_id: str, success: bool, message: str, result: dict) -> None:
        """分发命令结果：同步等待者 + 信号广播。"""
        # 同步等待者
        with self._sync_lock:
            if request_id in self._sync_events:
                self._sync_results[request_id] = (success, message, result)
                self._sync_events[request_id].set()

        # 信号广播（Qt 信号，跨线程安全）
        self.command_completed.emit(request_id, success, message, result)
        if not success:
            self.command_error.emit(request_id, message)

    # -- command routing -----------------------------------------------------

    def _execute(self, cmd: Command) -> dict:
        """命令路由：根据 cmd_type 调用 InstrumentController 或 Driver 方法。"""
        ct = cmd.cmd_type
        p = cmd.params

        # ===== SMB100A =====
        if ct == CommandType.SMB_CONNECT:
            idn = self._ctrl.connect_smb(p["address"], p.get("timeout_ms", 10000))
            return {"idn": idn}

        if ct == CommandType.SMB_DISCONNECT:
            self._ctrl.disconnect_smb()
            return {}

        if ct == CommandType.SMB_SET_FREQUENCY:
            self._ctrl.smb.set_freq_cw(float(p["freq_hz"]))
            return {"freq_hz": p["freq_hz"]}

        if ct == CommandType.SMB_SET_POWER:
            # 安全校验：功率限制
            self._check_power_limit(float(p["power_dbm"]))
            self._ctrl.smb.set_power(float(p["power_dbm"]))
            return {"power_dbm": p["power_dbm"]}

        if ct == CommandType.SMB_SET_OUTPUT:
            self._ctrl.smb.set_output(bool(p["enabled"]))
            return {"enabled": p["enabled"]}

        if ct == CommandType.SMB_SET_LF_OUTPUT:
            self._ctrl.smb.set_lf_output(bool(p["enabled"]))
            if "freq_hz" in p:
                self._ctrl.smb.set_lf_freq(float(p["freq_hz"]))
            if "amplitude_v" in p:
                self._ctrl.smb.set_lf_voltage(float(p["amplitude_v"]) * 1000)
            return {}

        if ct == CommandType.SMB_SET_LF_FREQ:
            self._ctrl.smb.set_lf_freq(float(p["freq_hz"]))
            return {"freq_hz": p["freq_hz"]}

        if ct == CommandType.SMB_SET_LF_VOLTAGE:
            self._ctrl.smb.set_lf_voltage(float(p["mv"]))
            return {"mv": p["mv"]}

        if ct == CommandType.SMB_SET_LF_SHAPE:
            self._ctrl.smb.set_lf_shape(p["shape"])
            return {"shape": p["shape"]}

        if ct == CommandType.SMB_SET_FM_DEVIATION:
            self._ctrl.smb.set_fm_deviation(float(p["hz"]))
            return {"hz": p["hz"]}

        if ct == CommandType.SMB_SET_MODULATION:
            # 简化版：仅开关 FM 调制
            enabled = bool(p.get("enabled", False))
            self._ctrl.smb.set_fm_state(enabled)
            return {"enabled": enabled}

        if ct == CommandType.SMB_SET_SWEEP:
            sweep_power = float(p.get("power_dbm", self._ctrl.smb.cached_power_dbm))
            self._check_power_limit(sweep_power)
            self._ctrl.smb.configure_sweep(
                float(p["start_hz"]),
                float(p["stop_hz"]),
                float(p["step_hz"]),
                float(p["dwell_ms"]),
                sweep_power,
            )
            return {}

        if ct == CommandType.SMB_START_SWEEP:
            self._ctrl.smb.start_sweep()
            return {}

        if ct == CommandType.SMB_STOP_SWEEP:
            self._ctrl.smb.stop_sweep()
            return {}

        if ct == CommandType.SMB_QUERY_STATE:
            return self._query_smb_state()

        if ct == CommandType.SMB_EMERGENCY_STOP:
            self._ctrl.smb.emergency_stop()
            return {}

        # ===== OE1022D =====
        if ct == CommandType.LOCKIN_CONNECT:
            idn = self._ctrl.connect_lockin(
                p["port"],
                p.get("baudrate", 921600),
                p.get("bytesize", 8),
                p.get("parity", "N"),
                p.get("stopbits", 1),
                p.get("timeout", 1.0),
            )
            return {"idn": idn}

        if ct == CommandType.LOCKIN_DISCONNECT:
            self._ctrl.disconnect_lockin()
            return {}

        if ct == CommandType.LOCKIN_QUERY_SNAPD:
            ch = p.get("channel", 1)
            data = self._ctrl.lockin.snapd(ch, 0, 1, 2, 3)
            return {"channel": ch, **data}

        if ct == CommandType.LOCKIN_QUERY_STATUS:
            ch = p.get("channel", 1)
            return {
                "channel": ch,
                "input_overload": self._ctrl.lockin.get_input_overload(ch),
                "gain_overload": self._ctrl.lockin.get_gain_overload(ch),
                "pll_locked": self._ctrl.lockin.get_pll_locked(ch),
            }

        if ct == CommandType.LOCKIN_START_ACQUIRE:
            recorder = self._create_recorder(p) if p.get("record", False) else None
            self._ctrl.start_lockin_acquire(recorder)
            if recorder is not None:
                self._acq_recorder = recorder
            return self._acq_result(recorder)

        if ct == CommandType.LOCKIN_STOP_ACQUIRE:
            self._ctrl.stop_lockin_acquire()
            self._acq_recorder = None
            return {}

        if ct == CommandType.LOCKIN_SET_TIME_CONSTANT:
            self._ctrl.lockin.set_time_constant(p.get("channel", 1), p["index"])
            return {}

        if ct == CommandType.LOCKIN_SET_FILTER_SLOPE:
            self._ctrl.lockin.set_filter_slope(p.get("channel", 1), p["index"])
            return {}

        if ct == CommandType.LOCKIN_SET_SYNC_FILTER:
            self._ctrl.lockin.set_sync_filter(p.get("channel", 1), bool(p["on"]))
            return {}

        if ct == CommandType.LOCKIN_SET_LINE_NOTCH:
            self._ctrl.lockin.set_line_notch(p.get("channel", 1), p["mode"])
            return {}

        if ct == CommandType.LOCKIN_AUTO_GAIN:
            self._ctrl.lockin.auto_gain(p.get("channel", 1))
            return {}

        if ct == CommandType.LOCKIN_AUTO_RESERVE:
            self._ctrl.lockin.auto_reserve(p.get("channel", 1))
            return {}

        if ct == CommandType.LOCKIN_AUTO_PHASE:
            self._ctrl.lockin.auto_phase(p.get("channel", 1))
            return {}

        if ct == CommandType.LOCKIN_SET_INPUT:
            ch = p.get("channel", 1)
            self._ctrl.lockin.set_input_source(ch, p.get("source", 0))
            self._ctrl.lockin.set_current_gain(ch, p.get("gain", 0))
            self._ctrl.lockin.set_grounding(ch, p.get("ground", 0))
            self._ctrl.lockin.set_coupling(ch, p.get("coupling", 0))
            self._ctrl.lockin.set_line_notch(ch, p.get("notch", 1))
            return {}

        if ct == CommandType.LOCKIN_SET_REF_PHASE:
            ch = p.get("channel", 1)
            self._ctrl.lockin.set_ref_phase(ch, p.get("phase_deg", 0.0))
            self._ctrl.lockin.set_ref_source(ch, p.get("source", 0))
            self._ctrl.lockin.set_ref_slope(ch, p.get("slope", 0))
            self._ctrl.lockin.set_ref_frequency(ch, p.get("freq_hz", 1000.0))
            self._ctrl.lockin.set_harmonic(ch, p.get("harmonic", 1))
            return {}

        if ct == CommandType.LOCKIN_SET_GAIN_TC:
            ch = p.get("channel", 1)
            self._ctrl.lockin.set_sensitivity(ch, p.get("sensitivity", 10))
            self._ctrl.lockin.set_reserve(ch, p.get("reserve", 1))
            self._ctrl.lockin.set_time_constant(ch, p.get("time_const", 6))
            self._ctrl.lockin.set_filter_slope(ch, p.get("filter_db", 2))
            self._ctrl.lockin.set_sync_filter(ch, bool(p.get("sync", True)))
            return {}

        if ct == CommandType.LOCKIN_SET_OUTPUT:
            ch = p.get("channel", 1)
            self._ctrl.lockin.set_output_source(ch, p.get("output_ch", 1), p.get("source", 0))
            self._ctrl.lockin.set_output_offset(ch, p.get("output_ch", 1), p.get("offset", 0))
            self._ctrl.lockin.set_output_expand(ch, p.get("output_ch", 1), p.get("expand", 0))
            return {}

        if ct == CommandType.LOCKIN_SET_SAMPLE:
            # OE1022D 采样参数配置（当前为占位，驱动层命令待完善）
            self.log_requested.emit(
                f"[Lockin] Sample config: step_time={p.get('step_time_ms')}ms, "
                f"length={p.get('length')}, trigger={'Internal' if p.get('trigger_mode') == 0 else 'External'}"
            )
            return {"status": "configured"}

        # ===== 采集 =====
        if ct == CommandType.ACQ_SET_SAMPLING:
            interval_ms = int(p.get("interval_ms", 50))
            if interval_ms < 10:
                raise ValueError("采样间隔不能小于 10 ms")
            return {}

        if ct == CommandType.ACQ_START_RECORDING:
            recorder = self._create_recorder(p)
            self._acq_recorder = recorder
            self._ctrl.start_lockin_acquire(recorder)
            return self._acq_result(recorder)

        if ct == CommandType.ACQ_STOP_RECORDING:
            stop_acquire = bool(p.get("stop_acquire", True))
            recorder = self._acq_recorder
            if stop_acquire:
                self._ctrl.stop_lockin_acquire()
            else:
                self._ctrl.detach_lockin_recorder()
            self._acq_recorder = None
            return self._acq_result(recorder)

        if ct == CommandType.ACQ_QUERY_STATE:
            recorder = self._acq_recorder
            result = {
                "acquiring": self._ctrl.is_acquiring,
                "recording": bool(recorder is not None and recorder.is_recording),
            }
            if recorder is not None and recorder.output_dir is not None:
                result["output_dir"] = str(recorder.output_dir)
            return result

        # ===== 激光器 =====
        if ct == CommandType.LASER_CONNECT:
            idn = self._ctrl.connect_laser(
                p["port"],
                p.get("baudrate", 9600),
                p.get("bytesize", 8),
                p.get("parity", "N"),
                p.get("stopbits", 1),
                p.get("timeout", 1.0),
            )
            return {"idn": idn}

        if ct == CommandType.LASER_DISCONNECT:
            self._ctrl.disconnect_laser()
            return {}

        if ct == CommandType.LASER_SET_POWER:
            self._check_laser_power_limit(int(p["power_mw"]))
            self._ctrl.laser.set_power(int(p["power_mw"]))
            return {"power_mw": p["power_mw"]}

        if ct == CommandType.LASER_SET_OUTPUT:
            self._ctrl.laser.set_output(bool(p["enabled"]))
            return {"enabled": p["enabled"]}

        if ct == CommandType.LASER_QUERY_STATE:
            return {
                "power_mw": self._ctrl.laser.get_power(),
                "output_on": self._ctrl.laser.get_output(),
                "wavelength_nm": self._ctrl.laser.wavelength_nm,
                "max_power_mw": self._ctrl.laser.max_power_mw,
            }

        if ct == CommandType.LASER_EMERGENCY_STOP:
            self._ctrl.laser.emergency_stop()
            return {}

        # ===== 系统 =====
        if ct == CommandType.SYS_EMERGENCY_STOP:
            self._ctrl.emergency_stop()
            return {}

        if ct == CommandType.SYS_QUERY_ALL_STATUS:
            result: Dict[str, Any] = {}
            if self._ctrl.is_smb_connected:
                result["smb"] = self._query_smb_state()
            if self._ctrl.is_lockin_connected:
                ch1 = self._ctrl.lockin.snapd(1, 0, 1, 2, 3)
                result["lockin_ch1"] = {"channel": 1, **ch1}
            if self._ctrl.is_laser_connected:
                result["laser"] = {
                    "power_mw": self._ctrl.laser.get_power(),
                    "output_on": self._ctrl.laser.get_output(),
                }
            return result

        raise NotImplementedError(f"未实现的命令类型: {ct.name}")

    # -- safety checks -------------------------------------------------------

    def _check_power_limit(self, power_dbm: float) -> None:
        """微波功率安全限制校验。"""
        smb_cfg = self._config.get("smb", {})
        amplifier_installed = smb_cfg.get("amplifier_installed", True)
        max_dbm = 10.0 if amplifier_installed else 25.0
        if power_dbm > max_dbm:
            raise SafetyError(
                f"功率 {power_dbm} dBm 超过安全限制 {max_dbm} dBm "
                f"({'已接' if amplifier_installed else '未接'}放大器)"
            )

    def _check_laser_power_limit(self, power_mw: int) -> None:
        """激光功率安全限制校验。"""
        laser_cfg = self._config.get("laser", {})
        max_mw = laser_cfg.get("max_power_mw", 150)
        if power_mw < 0 or power_mw > max_mw:
            raise SafetyError(
                f"激光功率 {power_mw} mW 超出安全范围 [0, {max_mw}] mW"
            )

    # -- state helpers -------------------------------------------------------

    def _query_smb_state(self) -> dict:
        """查询 SMB100A 完整状态。"""
        smb = self._ctrl.smb
        return {
            "freq_hz": smb.cached_freq_hz,
            "power_dbm": smb.cached_power_dbm,
            "output_on": smb.cached_output_on,
            "mode": smb.cached_mode,
        }

    def _create_recorder(self, params: Dict[str, Any]) -> ODMRRecorder:
        """创建并启动一次 CSV 记录会话。"""
        output_dir = params.get("output_dir") or params.get("save_dir")
        recorder = ODMRRecorder(Path(output_dir) if output_dir else None)
        recorder.start_recording()
        return recorder

    @staticmethod
    def _acq_result(recorder: Optional[ODMRRecorder]) -> dict:
        if recorder is None:
            return {"recording": False}
        return {
            "recording": recorder.is_recording,
            "output_dir": str(recorder.output_dir) if recorder.output_dir is not None else "",
        }

    # -- signal forwarding ---------------------------------------------------

    def _on_smb_state_changed(self, freq_hz: float, output_on: bool, mode: str) -> None:
        """接收 InstrumentController 的 SMB 状态更新并广播。"""
        state = {
            "freq_hz": freq_hz,
            "output_on": output_on,
            "mode": mode,
            "power_dbm": self._ctrl.smb.cached_power_dbm,
        }
        self.smb_state_broadcast.emit(state)
        # 时间戳同步中心摄入
        self._ts_hub.ingest(TimestampedSample(
            host_timestamp_s=time.monotonic(),
            device_timestamp_s=0.0,
            source="smb",
            data=state,
        ))

    def _on_lockin_data_ready(self, data: dict) -> None:
        """接收 InstrumentController 的 Lockin SNAPD 数据并广播。"""
        self.lockin_data_broadcast.emit(data)
        # 时间戳同步中心摄入
        ch = data.get("channel", 1)
        self._ts_hub.ingest(TimestampedSample(
            host_timestamp_s=time.monotonic(),
            device_timestamp_s=0.0,
            source=f"lockin_{'A' if ch == 1 else 'B'}",
            data=data,
        ))

    def _on_lockin_status_changed(self, status: dict) -> None:
        """接收 InstrumentController 的 Lockin 状态更新并广播。"""
        self.lockin_status_broadcast.emit(status)

    def _on_lockin_batch_ready(self, batch: dict) -> None:
        """接收 InstrumentController 的 Lockin RALL 批次并广播。"""
        self.lockin_batch_broadcast.emit(batch)
