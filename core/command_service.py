"""命令执行服务层

所有对设备的操作（GUI / AI Agent）均通过此类统一下发。
- 单线程串行执行，避免 VISA/Serial 并发冲突
- 安全校验在命令路由层执行（如功率限制）
- 订阅底层 InstrumentController 信号并统一广播
"""

from __future__ import annotations

import copy
import datetime
import hashlib
import json
import queue
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from PyQt5.QtCore import QObject, QThread, pyqtSignal

try:
    import jsonschema
except ImportError:
    jsonschema = None  # type: ignore[assignment]

from core.commands import Command, CommandType
from core.instrument_controller import InstrumentController
from core.mag_field_controller import FieldController
from core.mag_sequence_engine import FieldSequence, FieldStep, SequenceEngine
from core.preflight import ExperimentPreflight
from core.resource_manager import ResourceManager
from core.run_manifest import RunManifest
from core.vector_field import SphericalField, spherical_to_cartesian
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
    mag_state_broadcast = pyqtSignal(dict)

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
        self._mag_sequence = SequenceEngine(controller.mag)
        self._experiment_plan: Optional[Dict[str, Any]] = None
        self._experiment_state: Dict[str, Any] = {"running": False, "paused": False, "step_index": -1}
        self._experiment_thread: Optional[threading.Thread] = None
        self._experiment_stop = threading.Event()
        self._experiment_pause = threading.Event()
        self._experiment_manual_advance = threading.Event()
        self._resource_manager = ResourceManager()
        self._experiment_run_id: str = ""
        self._experiment_run_dir: Optional[Path] = None

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
        self._ctrl.mag_state_changed.connect(self.mag_state_broadcast.emit)
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

    _LEASED_COMMAND_RESOURCES = {
        CommandType.SMB_SET_FREQUENCY: "microwave",
        CommandType.SMB_SET_POWER: "microwave",
        CommandType.SMB_SET_OUTPUT: "microwave",
        CommandType.SMB_SET_LF_OUTPUT: "microwave",
        CommandType.SMB_SET_LF_FREQ: "microwave",
        CommandType.SMB_SET_LF_VOLTAGE: "microwave",
        CommandType.SMB_SET_LF_SHAPE: "microwave",
        CommandType.SMB_SET_MODULATION: "microwave",
        CommandType.SMB_SET_FM_DEVIATION: "microwave",
        CommandType.SMB_SET_SWEEP: "microwave",
        CommandType.SMB_START_SWEEP: "microwave",
        CommandType.LOCKIN_SET_INPUT: "lockin",
        CommandType.LOCKIN_SET_REF_PHASE: "lockin",
        CommandType.LOCKIN_SET_GAIN_TC: "lockin",
        CommandType.LOCKIN_SET_OUTPUT: "lockin",
        CommandType.LOCKIN_SET_SAMPLE: "lockin",
        CommandType.LOCKIN_AUTO_GAIN: "lockin",
        CommandType.LOCKIN_AUTO_RESERVE: "lockin",
        CommandType.LOCKIN_AUTO_PHASE: "lockin",
        CommandType.LOCKIN_START_ACQUIRE: "lockin",
        CommandType.LOCKIN_STOP_ACQUIRE: "lockin",
        CommandType.LOCKIN_SET_TIME_CONSTANT: "lockin",
        CommandType.LOCKIN_SET_FILTER_SLOPE: "lockin",
        CommandType.LOCKIN_SET_SYNC_FILTER: "lockin",
        CommandType.LOCKIN_SET_LINE_NOTCH: "lockin",
        CommandType.ACQ_START_RECORDING: "lockin",
        CommandType.ACQ_STOP_RECORDING: "lockin",
        CommandType.LASER_SET_POWER: "laser",
        CommandType.LASER_SET_OUTPUT: "laser",
        CommandType.MAG_SET_FIELD: "magnetic_field",
        CommandType.MAG_SET_FIELD_3D: "magnetic_field",
        CommandType.MAG_SET_CURRENT: "magnetic_field",
        CommandType.MAG_SET_ZERO_OFFSET: "magnetic_field",
        CommandType.MAG_PREPARE_ZERO_LOCK: "magnetic_field",
        CommandType.MAG_SET_OUTPUT: "magnetic_field",
        CommandType.MAG_LOCK_ZERO: "magnetic_field",
        CommandType.MAG_LOAD_SEQUENCE: "magnetic_field",
        CommandType.MAG_START_SEQUENCE: "magnetic_field",
    }

    def _enforce_resource_lease(self, cmd: Command) -> None:
        if cmd.source == "experiment":
            return
        if cmd.cmd_type in {
            CommandType.SYS_EMERGENCY_STOP,
            CommandType.SMB_EMERGENCY_STOP,
            CommandType.LASER_EMERGENCY_STOP,
            CommandType.MAG_EMERGENCY_STOP,
            CommandType.EXPERIMENT_STOP,
            CommandType.EXPERIMENT_PAUSE,
            CommandType.EXPERIMENT_RESUME,
            CommandType.EXPERIMENT_QUERY_STATE,
        }:
            return
        resource = self._LEASED_COMMAND_RESOURCES.get(cmd.cmd_type)
        if not resource or not self._resource_manager.is_leased(resource):
            return
        owner = self._resource_manager.owner(resource)
        raise SafetyError(f"{resource} is leased by active run {owner}; manual command {cmd.cmd_type.name} is blocked")

    def _execute(self, cmd: Command) -> dict:
        """命令路由：根据 cmd_type 调用 InstrumentController 或 Driver 方法。"""
        ct = cmd.cmd_type
        p = cmd.params
        self._enforce_resource_lease(cmd)

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
            if "fm_source" in p:
                self._ctrl.smb.set_fm_source(str(p["fm_source"]))
            if "fm_mode" in p:
                self._ctrl.smb.set_fm_mode(str(p["fm_mode"]))
            self._ctrl.smb.set_fm_state(enabled)
            if "am_state" in p:
                self._ctrl.smb.set_am_state(bool(p["am_state"]))
            if "am_depth_pct" in p:
                self._ctrl.smb.set_am_depth(float(p["am_depth_pct"]))
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
                spacing=str(p.get("spacing", "LIN")),
                shape=str(p.get("shape", "SAWTOOTH")),
                retrace=bool(p.get("retrace", False)),
                trigger=str(p.get("trigger", "IMM")),
                lf_connector=bool(p.get("lf_connector", False)),
                ovolt_start_v=float(p.get("ovolt_start_v", 0.0)),
                ovolt_stop_v=float(p.get("ovolt_stop_v", 3.0)),
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

        if ct == CommandType.SMB_QUERY_CONFIG:
            return self._ctrl.smb.query_config()

        if ct == CommandType.SMB_APPLY_CONFIG:
            return self._apply_smb_config(p)

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
                p.get("transport", "rs232"),
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
            self._ctrl.lockin.configure_channel_output(
                output_ch=int(p.get("output_ch", 1)),
                source=int(p.get("source", 0)),
                offset_pct=float(p.get("offset", 0.0)),
                expand=int(p.get("expand", 1)),
                speed=int(p.get("speed", 0)),
                aux_voltage_v=float(p.get("aux_voltage_v", 0.0)),
            )
            return {}

        if ct == CommandType.LOCKIN_SET_SAMPLE:
            self._ctrl.lockin.set_sample_config(
                channel=int(p.get("channel", 1)),
                step_time_ms=float(p.get("step_time_ms", 100.0)),
                length=int(p.get("length", 1024)),
                buffers=tuple(p.get("buffers", (0, 1, 2, 3))),
                trigger_mode=int(p.get("trigger_mode", 0)),
                sample_mode=int(p.get("sample_mode", 0)),
            )
            return {"status": "configured"}

        if ct == CommandType.LOCKIN_QUERY_CONFIG:
            ch = int(p.get("channel", 1))
            return self._ctrl.lockin.query_config(ch)

        if ct == CommandType.LOCKIN_APPLY_CONFIG:
            return self._apply_lockin_config(p)

        if ct == CommandType.LOCKIN_SET_DISPLAY_REFRESH_POLICY:
            return self._set_lockin_display_refresh_policy(p)

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

        # ===== 磁场控制 =====
        if ct == CommandType.MAG_CONNECT_AXIS:
            axis = self._normalize_axis(p.get("axis", "X"))
            idn = self._ctrl.mag.connect_axis(axis, p["port"], int(p.get("baudrate", 9600)))
            self._apply_mag_axis_config(axis, p)
            return {"axis": axis, "idn": idn, "state": self._ctrl.mag.get_status(axis)}

        if ct == CommandType.MAG_DISCONNECT_AXIS:
            axis = self._normalize_axis(p.get("axis", "X"))
            self._ctrl.mag.disconnect_axis(axis)
            return {"axis": axis}

        if ct == CommandType.MAG_CONNECT_ALL:
            ports = dict(p.get("ports", {}))
            selected = [port for port in ports.values() if port]
            duplicates = sorted({port for port in selected if selected.count(port) > 1})
            if duplicates:
                raise SafetyError(f"重复串口已阻止: {', '.join(duplicates)}")
            results = self._ctrl.mag.connect_all(ports, int(p.get("baudrate", 9600)))
            for axis in ("X", "Y", "Z"):
                axis_cfg = p.get("axes", {}).get(axis, {})
                if self._ctrl.mag.is_connected(axis):
                    self._apply_mag_axis_config(axis, axis_cfg)
            return {"results": results, "state": self._ctrl.mag.get_field_snapshot()}

        if ct == CommandType.MAG_DISCONNECT_ALL:
            self._ctrl.mag.disconnect_all()
            return {}

        if ct == CommandType.MAG_SCAN_PORTS:
            baudrate = int(p.get("baudrate", self._config.get("magnetic_field", {}).get("baudrate", 9600)))
            return {"devices": self._ctrl.mag.scan_device_ports(baudrate)}

        if ct == CommandType.MAG_AUTO_DETECT:
            baudrate = int(p.get("baudrate", self._config.get("magnetic_field", {}).get("baudrate", 9600)))
            devices = self._ctrl.mag.scan_device_ports(baudrate)
            bindings = p.get("bindings") or self._mag_axis_bindings()
            matched_ports = self._ctrl.mag.match_devices_to_axes(devices, bindings)
            port_to_idn = {item.get("port"): item.get("idn", "") for item in devices}
            matched = {
                axis: {"port": port or "", "idn": port_to_idn.get(port, "")}
                for axis, port in matched_ports.items()
            }
            return {"devices": devices, "matched": matched}

        if ct == CommandType.MAG_BIND_AXIS_IDN:
            axis = self._normalize_axis(p.get("axis", "X"))
            binding = {"idn": str(p.get("idn", "")), "port": str(p.get("port", ""))}
            self._config.setdefault("magnetic_field", {}).setdefault("bindings", {})[axis] = binding
            return {"axis": axis, "binding": binding}

        if ct == CommandType.MAG_SET_POLL_INTERVAL:
            interval_ms = int(p["interval_ms"])
            self._ctrl.mag.set_poll_interval_ms(interval_ms)
            self._config.setdefault("magnetic_field", {})["poll_interval_ms"] = interval_ms
            return {"interval_ms": interval_ms}

        if ct == CommandType.MAG_SET_FIELD:
            axis = self._normalize_axis(p.get("axis", "X"))
            ok = self._ctrl.mag.set_field(axis, float(p["field_nT"]))
            if not ok:
                raise SafetyError(f"{axis} 轴磁场设置被拒绝")
            return self._ctrl.mag.get_status(axis)

        if ct == CommandType.MAG_SET_FIELD_3D:
            for axis, key in (("X", "x_nT"), ("Y", "y_nT"), ("Z", "z_nT")):
                if self._ctrl.mag.is_connected(axis):
                    ok = self._ctrl.mag.set_field(axis, float(p.get(key, 0.0)))
                    if not ok:
                        raise SafetyError(f"{axis} 轴磁场设置被拒绝")
            return self._ctrl.mag.get_field_snapshot()

        if ct == CommandType.MAG_SET_CURRENT:
            axis = self._normalize_axis(p.get("axis", "X"))
            ok = self._ctrl.mag.set_current(axis, float(p["current_mA"]))
            if not ok:
                raise SafetyError(f"{axis} 轴电流设置被拒绝")
            return self._ctrl.mag.get_status(axis)

        if ct == CommandType.MAG_SET_ZERO_OFFSET:
            axis = self._normalize_axis(p.get("axis", "X"))
            ok = self._ctrl.mag.set_zero_offset(axis, float(p["zero_offset_mA"]))
            if not ok:
                raise SafetyError(f"{axis} 轴零偏设置被拒绝")
            return self._ctrl.mag.get_status(axis)

        if ct == CommandType.MAG_CAPTURE_BACKGROUND:
            axis = self._normalize_axis(p.get("axis", "X"))
            zero = self._ctrl.mag.capture_background_as_zero(axis)
            state = self._ctrl.mag.get_status(axis)
            return {"axis": axis, "zero_offset_mA": zero, "state": state}

        if ct == CommandType.MAG_PREPARE_ZERO_LOCK:
            axis = self._normalize_axis(p.get("axis", "X"))
            ok = self._ctrl.mag.prepare_zero_lock(axis, capture_readback=bool(p.get("capture_readback", False)))
            if not ok:
                raise SafetyError(f"{axis} 轴零场锁定准备失败")
            return self._ctrl.mag.get_status(axis)

        if ct == CommandType.MAG_SET_COIL_CONSTANT:
            axis = self._normalize_axis(p.get("axis", "X"))
            value = float(p["coil_constant"])
            if value <= 0:
                raise SafetyError("线圈常数必须为正数")
            self._ctrl.mag.set_coil_constant(axis, value)
            return self._ctrl.mag.get_status(axis)

        if ct == CommandType.MAG_SET_OUTPUT:
            axis = self._normalize_axis(p.get("axis", "X"))
            if not self._ctrl.mag.is_connected(axis):
                raise SafetyError(f"{axis} 轴未连接，不能开启或关闭输出")
            ok = self._ctrl.mag.set_output(axis, bool(p["enabled"]))
            if not ok:
                raise SafetyError(f"{axis} 轴输出设置被拒绝")
            return self._ctrl.mag.get_status(axis)

        if ct == CommandType.MAG_LOCK_ZERO:
            axis = self._normalize_axis(p.get("axis", "X"))
            ok = self._ctrl.mag.lock_zero(axis, bool(p["locked"]))
            if not ok:
                raise SafetyError(f"{axis} 轴锁零设置被拒绝")
            return self._ctrl.mag.get_status(axis)

        if ct == CommandType.MAG_QUERY_STATE:
            if "axis" in p:
                axis = self._normalize_axis(p["axis"])
                return self._ctrl.mag.get_status(axis)
            return self._ctrl.mag.get_field_snapshot()

        if ct == CommandType.MAG_EMERGENCY_STOP:
            self._ctrl.mag.all_output_off()
            return self._ctrl.mag.verify_emergency_stop()

        if ct == CommandType.MAG_LOAD_SEQUENCE:
            seq = self._load_mag_sequence(p)
            self._mag_sequence.load_sequence(seq)
            return {"name": seq.name, "steps": len(seq.steps)}

        if ct == CommandType.MAG_START_SEQUENCE:
            self._mag_sequence.start(p.get("record_path"))
            return {"running": True}

        if ct == CommandType.MAG_PAUSE_SEQUENCE:
            self._mag_sequence.pause()
            return {"paused": True}

        if ct == CommandType.MAG_RESUME_SEQUENCE:
            self._mag_sequence.resume()
            return {"paused": False}

        if ct == CommandType.MAG_STOP_SEQUENCE:
            self._mag_sequence.stop()
            return {"running": False}

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
            if self._ctrl.is_mag_connected:
                result["magnetic_field"] = self._ctrl.mag.get_field_snapshot()
            return result

        # ===== 统一实验自动化 =====
        if ct == CommandType.EXPERIMENT_LOAD_JSON:
            plan = self._load_experiment_plan(p)
            self._experiment_plan = plan
            return self._validate_experiment_plan(plan)

        if ct == CommandType.EXPERIMENT_VALIDATE:
            plan = self._load_experiment_plan(p) if p else self._experiment_plan
            if plan is None:
                raise ValueError("未加载实验 JSON")
            return self._validate_experiment_plan(plan)

        if ct == CommandType.EXPERIMENT_PREFLIGHT:
            plan = self._load_experiment_plan(p) if p else self._experiment_plan
            if plan is None:
                raise ValueError("未加载实验 JSON")
            validation = self._validate_experiment_plan(plan)
            if not validation["valid"]:
                return {"ok": False, "validation": validation, "checks": []}
            return ExperimentPreflight(self._ctrl, self._config).run(plan).to_dict()

        if ct == CommandType.EXPERIMENT_START:
            plan = self._load_experiment_plan(p) if ("path" in p or "plan" in p) else self._experiment_plan
            if plan is None:
                raise ValueError("未加载实验 JSON")
            return self._start_experiment(plan)

        if ct == CommandType.EXPERIMENT_PAUSE:
            self._experiment_pause.set()
            self._experiment_state["paused"] = True
            return self._experiment_state.copy()

        if ct == CommandType.EXPERIMENT_RESUME:
            self._experiment_pause.clear()
            self._experiment_manual_advance.set()
            self._experiment_state["paused"] = False
            return self._experiment_state.copy()

        if ct == CommandType.EXPERIMENT_STOP:
            self._experiment_stop.set()
            self._experiment_manual_advance.set()
            return self._experiment_state.copy()

        if ct == CommandType.EXPERIMENT_QUERY_STATE:
            return self._experiment_state.copy()

        if ct == CommandType.EXPERIMENT_WAIT:
            duration = float(p.get("duration_s", 0.0))
            condition = p.get("condition")
            if condition:
                timeout = duration if duration > 0 else 300.0
                met = self._wait_for_condition(condition, timeout)
                return {"condition_met": met, "timeout_s": timeout}
            elif duration > 0:
                self._interruptible_experiment_sleep(duration)
                return {"waited_s": duration}
            return {"waited_s": 0}

        if ct == CommandType.EXPERIMENT_CONDITION_EVAL:
            condition = p.get("condition", p)
            met = self._evaluate_condition(condition)
            return {"condition_met": met, "condition": condition}

        if ct == CommandType.EXPERIMENT_MODIFY_STEP:
            step_index = int(p.get("step_index", -1))
            overrides = p.get("overrides", {})
            if self._experiment_plan is not None:
                sequence = self._experiment_plan.get("sequence", {})
                steps = sequence.get("steps", sequence if isinstance(sequence, list) else [])
                name = p.get("step_name", "")
                if name:
                    step_index = self._find_step_index(steps, name)
                if 0 <= step_index < len(steps):
                    step = steps[step_index]
                    for key, value in overrides.items():
                        if key in step and isinstance(step[key], dict) and isinstance(value, dict):
                            step[key].update(value)
                        else:
                            step[key] = value
                    return {"modified_step": step_index, "overrides_applied": list(overrides.keys())}
            return {"error": "无法修改步骤：索引无效或实验计划未加载"}

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

    def _apply_smb_config(self, cfg: Dict[str, Any]) -> dict:
        if "frequency_hz" in cfg:
            self._ctrl.smb.set_freq_cw(float(cfg["frequency_hz"]))
        if "power_dbm" in cfg:
            self._check_power_limit(float(cfg["power_dbm"]))
            self._ctrl.smb.set_power(float(cfg["power_dbm"]))
        if "phase_deg" in cfg:
            self._ctrl.smb.set_phase(float(cfg["phase_deg"]))
        if "level_offset_db" in cfg:
            self._ctrl.smb.set_level_offset(float(cfg["level_offset_db"]))
        if "lf" in cfg:
            lf = cfg["lf"]
            if "frequency_hz" in lf:
                self._ctrl.smb.set_lf_freq(float(lf["frequency_hz"]))
            if "voltage_mv" in lf:
                self._ctrl.smb.set_lf_voltage(float(lf["voltage_mv"]))
            if "shape" in lf:
                self._ctrl.smb.set_lf_shape(str(lf["shape"]))
            if "impedance" in lf:
                self._ctrl.smb.set_lf_impedance(str(lf["impedance"]))
            if "output" in lf:
                self._ctrl.smb.set_lf_output(bool(lf["output"]))
        if "modulation" in cfg:
            mod = cfg["modulation"]
            if "fm_deviation_hz" in mod:
                self._ctrl.smb.set_fm_deviation(float(mod["fm_deviation_hz"]))
            if "fm_source" in mod:
                self._ctrl.smb.set_fm_source(str(mod["fm_source"]))
            if "fm_mode" in mod:
                self._ctrl.smb.set_fm_mode(str(mod["fm_mode"]))
            if "fm_state" in mod:
                self._ctrl.smb.set_fm_state(bool(mod["fm_state"]))
            if "am_depth_pct" in mod:
                self._ctrl.smb.set_am_depth(float(mod["am_depth_pct"]))
            if "am_state" in mod:
                self._ctrl.smb.set_am_state(bool(mod["am_state"]))
        if "sweep" in cfg:
            sweep = cfg["sweep"]
            if all(k in sweep for k in ("start_hz", "stop_hz", "step_hz", "dwell_ms")):
                sweep_power = float(sweep.get("power_dbm", cfg.get("power_dbm", self._ctrl.smb.cached_power_dbm)))
                self._check_power_limit(sweep_power)
                self._ctrl.smb.configure_sweep(
                    float(sweep["start_hz"]),
                    float(sweep["stop_hz"]),
                    float(sweep["step_hz"]),
                    float(sweep["dwell_ms"]),
                    sweep_power,
                    spacing=str(sweep.get("spacing", "LIN")),
                    shape=str(sweep.get("shape", "SAWTOOTH")),
                    retrace=bool(sweep.get("retrace", False)),
                    trigger=str(sweep.get("trigger", "IMM")),
                    lf_connector=bool(sweep.get("lf_connector", False)),
                    ovolt_start_v=float(sweep.get("ovolt_start_v", 0.0)),
                    ovolt_stop_v=float(sweep.get("ovolt_stop_v", 3.0)),
                )
            if sweep.get("execute"):
                self._ctrl.smb.set_output(True)
                self._ctrl.smb.start_sweep()
        if "rf_output" in cfg:
            self._ctrl.smb.set_output(bool(cfg["rf_output"]))
        return self._ctrl.smb.query_config()

    def _apply_lockin_config(self, cfg: Dict[str, Any]) -> dict:
        channel = int(cfg.get("channel", 1))
        applied = False
        if "input" in cfg:
            data = dict(cfg["input"], channel=channel)
            self._execute(Command(CommandType.LOCKIN_SET_INPUT, data, source="system"))
            applied = True
        if "ref" in cfg:
            data = dict(cfg["ref"], channel=channel)
            self._execute(Command(CommandType.LOCKIN_SET_REF_PHASE, data, source="system"))
            applied = True
        if "gain_tc" in cfg:
            data = dict(cfg["gain_tc"], channel=channel)
            self._execute(Command(CommandType.LOCKIN_SET_GAIN_TC, data, source="system"))
            applied = True
        if "output" in cfg:
            data = dict(cfg["output"], channel=channel)
            self._execute(Command(CommandType.LOCKIN_SET_OUTPUT, data, source="system"))
            applied = True
        if "auto" in cfg:
            auto = cfg["auto"]
            if auto.get("gain"):
                self._ctrl.lockin.auto_gain(channel)
                applied = True
            if auto.get("reserve"):
                self._ctrl.lockin.auto_reserve(channel)
                applied = True
            if auto.get("phase"):
                self._ctrl.lockin.auto_phase(channel)
                applied = True
        if not applied:
            return {"channel": channel}
        return self._ctrl.lockin.query_config(channel)

    def _set_lockin_display_refresh_policy(self, params: Dict[str, Any]) -> dict:
        policy = {
            **self._config.get("lockin", {}).get("display_refresh_policy", {}),
            **params,
        }
        self._config.setdefault("lockin", {})["display_refresh_policy"] = policy
        return policy

    @staticmethod
    def _normalize_axis(axis: object) -> str:
        value = str(axis).upper()
        if value not in ("X", "Y", "Z"):
            raise ValueError(f"无效磁场轴: {axis}")
        return value

    def _mag_axis_bindings(self) -> Dict[str, Dict[str, str]]:
        """返回统一格式的轴绑定: {"X": {"idn": "...", "port": "..."}, ...}

        兼容旧格式（纯字符串 IDN）和新格式（含 idn + port 的 dict）。
        """
        raw = self._config.get("magnetic_field", {}).get("bindings", {})
        return {
            axis: FieldController._normalize_binding(raw.get(axis, ""))
            for axis in ("X", "Y", "Z")
        }

    def _apply_mag_axis_config(self, axis: str, params: Dict[str, Any]) -> None:
        cfg = self._config.get("magnetic_field", {}).get("axes", {}).get(axis, {})
        merged = {**cfg, **params}
        if "coil_constant" in merged:
            value = float(merged["coil_constant"])
            if value <= 0:
                raise SafetyError(f"{axis} 轴线圈常数必须为正数")
            self._ctrl.mag.set_coil_constant(axis, value)
        if "zero_offset_mA" in merged:
            ok = self._ctrl.mag.set_zero_offset(axis, float(merged["zero_offset_mA"]))
            if not ok:
                raise SafetyError(f"{axis} 轴零偏设置被拒绝")

    def _load_mag_sequence(self, params: Dict[str, Any]) -> FieldSequence:
        if "path" in params:
            return SequenceEngine.load_sequence_file(str(params["path"]))
        data = params.get("sequence", params)
        steps = []
        for step in data.get("steps", []):
            steps.append(FieldStep(
                name=step.get("name", ""),
                field_x_nT=float(step.get("field_x_nT", step.get("x_nT", 0.0))),
                field_y_nT=float(step.get("field_y_nT", step.get("y_nT", 0.0))),
                field_z_nT=float(step.get("field_z_nT", step.get("z_nT", 0.0))),
                hold_seconds=float(step.get("hold_seconds", step.get("hold_s", 0.0))),
                settle_seconds=float(step.get("settle_seconds", step.get("settle_s", 0.5))),
                trigger=step.get("trigger", "immediate"),
                delay_seconds=float(step.get("delay_seconds", step.get("delay_s", 0.0))),
            ))
        return FieldSequence(
            name=data.get("name", "Untitled"),
            steps=steps,
            loop_count=int(data.get("loop_count", 1)),
            loop_delay=float(data.get("loop_delay", 0.0)),
            return_to_zero=bool(data.get("return_to_zero", True)),
        )

    def _load_experiment_plan(self, params: Dict[str, Any]) -> Dict[str, Any]:
        if "plan" in params:
            plan = params["plan"]
            if not isinstance(plan, dict):
                raise ValueError("plan 必须是 dict")
            return plan
        if "path" in params:
            return json.loads(Path(params["path"]).read_text(encoding="utf-8-sig"))
        return params

    @staticmethod
    def _load_experiment_plan_schema() -> Optional[Dict[str, Any]]:
        """加载实验计划 JSON Schema（如果文件存在且 jsonschema 可用）。"""
        schema_path = Path(__file__).resolve().parent.parent / "schemas" / "experiment_plan.json"
        if not schema_path.exists():
            return None
        try:
            return json.loads(schema_path.read_text(encoding="utf-8"))
        except Exception:
            return None

    def _validate_experiment_plan(self, plan: Dict[str, Any]) -> dict:
        """验证实验计划的完整性和正确性。

        验证层级：
        1. 结构完整性验证（主验证，错误始终报告）
        2. 字段值域验证（主验证）
        3. 业务逻辑验证（主验证）
        4. JSON Schema 验证（补充验证，仅报告主验证未覆盖的结构性问题）
        """
        errors: list[str] = []

        # -- 层级 1: 结构完整性验证 ------------------------------------
        if not isinstance(plan, dict):
            errors.append("实验计划必须是对象（dict）")
            return {"valid": False, "errors": errors, "steps": 0}

        metadata = plan.get("metadata")
        if metadata is not None and not isinstance(metadata, dict):
            errors.append("metadata 必须是对象")

        sequence = plan.get("sequence")
        if sequence is None:
            errors.append("sequence.steps 不能为空")
            return {"valid": False, "errors": errors, "steps": 0}

        if isinstance(sequence, dict):
            steps = sequence.get("steps", [])
            loop_count = sequence.get("loop_count")
            if loop_count is not None:
                if not isinstance(loop_count, int) or loop_count < 0:
                    errors.append(f"sequence.loop_count 必须为非负整数，当前值: {loop_count}")
        elif isinstance(sequence, list):
            # 兼容旧格式：sequence 直接是步骤列表
            steps = sequence
        else:
            errors.append("sequence 必须是对象或数组")
            return {"valid": False, "errors": errors, "steps": 0}

        if not isinstance(steps, list):
            errors.append("sequence.steps 必须是数组")
            return {"valid": False, "errors": errors, "steps": 0}

        if not steps:
            errors.append("sequence.steps 不能为空")
            return {"valid": False, "errors": errors, "steps": 0}

        # -- 层级 2 & 3: 逐步验证 -------------------------------------
        for idx, step in enumerate(steps):
            step_label = f"step {idx}"

            if not isinstance(step, dict):
                errors.append(f"{step_label} 必须是对象")
                continue

            # timing 验证
            timing = step.get("timing", {})
            if timing and not isinstance(timing, dict):
                errors.append(f"{step_label} timing 必须是对象")
            elif isinstance(timing, dict):
                for key in ("settle_s", "hold_s", "delay_s"):
                    val = timing.get(key)
                    if val is not None:
                        try:
                            fval = float(val)
                            if fval < 0:
                                errors.append(f"{step_label} timing.{key} 不能为负数，当前值: {val}")
                        except (TypeError, ValueError):
                            errors.append(f"{step_label} timing.{key} 必须是数值，当前值: {val!r}")
                trigger = timing.get("trigger")
                if trigger is not None and trigger not in ("immediate", "manual", "delay"):
                    errors.append(f"{step_label} timing.trigger 无效: {trigger}")
                if trigger == "delay" and "delay_s" not in timing:
                    errors.append(f"{step_label} timing.trigger 为 delay 时必须指定 delay_s")

            # magnetic_field 验证
            if "magnetic_field" in step:
                field = step["magnetic_field"]
                if not isinstance(field, dict):
                    errors.append(f"{step_label} magnetic_field 必须是对象")
                else:
                    has_cartesian = all(k in field for k in ("x_nT", "y_nT", "z_nT"))
                    has_spherical = all(k in field for k in ("magnitude_nT", "theta_deg", "phi_deg"))
                    has_alias = any(k in field for k in ("field_x_nT", "field_y_nT", "field_z_nT"))
                    if not (has_cartesian or has_spherical or has_alias):
                        errors.append(
                            f"{step_label} magnetic_field 缺少坐标定义"
                            "（需要 x_nT/y_nT/z_nT 或 magnitude_nT/theta_deg/phi_deg）"
                        )
                    for key in ("x_nT", "y_nT", "z_nT", "field_x_nT", "field_y_nT", "field_z_nT",
                                "magnitude_nT", "theta_deg", "phi_deg"):
                        val = field.get(key)
                        if val is not None:
                            try:
                                float(val)
                            except (TypeError, ValueError):
                                errors.append(f"{step_label} magnetic_field.{key} 必须是数值，当前值: {val!r}")
                    mag = field.get("magnitude_nT")
                    if mag is not None:
                        try:
                            if float(mag) < 0:
                                errors.append(f"{step_label} magnetic_field.magnitude_nT 不能为负数")
                        except (TypeError, ValueError):
                            pass

            # microwave 验证
            if "microwave" in step:
                mw = step["microwave"]
                if not isinstance(mw, dict):
                    errors.append(f"{step_label} microwave 必须是对象")
                else:
                    freq = mw.get("frequency_hz")
                    if freq is not None:
                        try:
                            fval = float(freq)
                            if fval < 100_000 or fval > 12_750_000_000:
                                errors.append(
                                    f"{step_label} microwave.frequency_hz 超出范围 [100kHz, 12.75GHz]，"
                                    f"当前值: {freq}"
                                )
                        except (TypeError, ValueError):
                            errors.append(f"{step_label} microwave.frequency_hz 必须是数值")
                    power = mw.get("power_dbm")
                    if power is not None:
                        try:
                            float(power)
                        except (TypeError, ValueError):
                            errors.append(f"{step_label} microwave.power_dbm 必须是数值")
                    sweep = mw.get("sweep")
                    if sweep is not None:
                        if not isinstance(sweep, dict):
                            errors.append(f"{step_label} microwave.sweep 必须是对象")
                        else:
                            for k in ("start_hz", "stop_hz", "step_hz"):
                                v = sweep.get(k)
                                if v is not None:
                                    try:
                                        fv = float(v)
                                        if k == "step_hz" and fv <= 0:
                                            errors.append(f"{step_label} microwave.sweep.{k} 必须为正数")
                                    except (TypeError, ValueError):
                                        errors.append(f"{step_label} microwave.sweep.{k} 必须是数值")
                            if sweep.get("shape") is not None:
                                if sweep["shape"] not in ("SAWTOOTH", "TRIANGLE"):
                                    errors.append(f"{step_label} microwave.sweep.shape 无效: {sweep['shape']}")

            # lockin 验证
            if "lockin" in step:
                lockin = step["lockin"]
                if not isinstance(lockin, dict):
                    errors.append(f"{step_label} lockin 必须是对象")
                else:
                    ch = lockin.get("channel")
                    if ch is not None and ch not in (1, 2):
                        errors.append(f"{step_label} lockin.channel 必须是 1 或 2，当前值: {ch}")

            # laser 验证
            if "laser" in step:
                laser = step["laser"]
                if not isinstance(laser, dict):
                    errors.append(f"{step_label} laser 必须是对象")
                else:
                    pwr = laser.get("power_mw")
                    if pwr is not None:
                        try:
                            if float(pwr) < 0:
                                errors.append(f"{step_label} laser.power_mw 不能为负数")
                        except (TypeError, ValueError):
                            errors.append(f"{step_label} laser.power_mw 必须是数值")

            # acquisition 验证
            acq = step.get("acquisition", {})
            if isinstance(acq, dict):
                start_trigger = acq.get("start_trigger", "step_start")
                stop_trigger = acq.get("stop_trigger", "hold_elapsed")
                valid_start = {
                    "step_start", "setpoints_applied", "magnetic_settled",
                    "microwave_output_on", "microwave_sweep_start",
                    "manual", "external_trigger", "condition_met",
                    "inline", "disabled",
                }
                valid_stop = {
                    "hold_elapsed", "timed", "microwave_sweep_complete",
                    "manual", "step_end", "condition_met",
                    "external_trigger", "disabled",
                }
                if start_trigger not in valid_start:
                    errors.append(f"{step_label} acquisition.start_trigger 无效: {start_trigger}")
                if stop_trigger not in valid_stop:
                    errors.append(f"{step_label} acquisition.stop_trigger 无效: {stop_trigger}")

            # 条件步骤验证
            if step.get("type") == "conditional":
                cond = step.get("condition")
                if not isinstance(cond, dict):
                    errors.append(f"{step_label} 条件步骤缺少 condition 字段")
                elif "type" not in cond:
                    errors.append(f"{step_label} condition 缺少 type 字段")

            # 等待步骤验证
            if step.get("type") == "wait":
                has_duration = "duration_s" in step
                has_condition = isinstance(step.get("condition"), dict)
                if not has_duration and not has_condition:
                    errors.append(f"{step_label} 等待步骤至少需要 duration_s 或 condition")

            # 动态参数步骤验证
            if "parameter_overrides" in step:
                overrides = step["parameter_overrides"]
                if not isinstance(overrides, dict):
                    errors.append(f"{step_label} parameter_overrides 必须是对象")

        # -- 层级 4: JSON Schema 补充验证 -----------------------------
        # 仅在主验证通过时执行，用于发现主验证未覆盖的结构性问题
        # 跳过 sequence 为列表的旧格式（schema 仅支持 dict 格式）
        if not errors and isinstance(plan.get("sequence"), dict):
            schema = self._load_experiment_plan_schema()
            if schema is not None and jsonschema is not None:
                try:
                    jsonschema.validate(plan, schema)
                except jsonschema.ValidationError as exc:
                    path = ".".join(str(p) for p in exc.absolute_path) if exc.absolute_path else "(root)"
                    errors.append(f"Schema 验证失败 [{path}]: {exc.message}")
                except jsonschema.SchemaError as exc:
                    errors.append(f"Schema 定义错误: {exc.message}")

        return {
            "valid": not errors,
            "errors": errors,
            "steps": len(steps),
        }

    @staticmethod
    def _stable_hash(data: Dict[str, Any]) -> str:
        encoded = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _required_resources(plan: Dict[str, Any]) -> list[str]:
        sequence = plan.get("sequence", {})
        steps = sequence.get("steps", []) if isinstance(sequence, dict) else sequence
        resources: set[str] = set()
        if isinstance(steps, list):
            for step in steps:
                if not isinstance(step, dict):
                    continue
                if "microwave" in step:
                    resources.add("microwave")
                if "lockin" in step or "acquisition" in step:
                    resources.add("lockin")
                if "laser" in step:
                    resources.add("laser")
                if "magnetic_field" in step:
                    resources.add("magnetic_field")
        return sorted(resources)

    def _output_root_for_plan(self, plan: Dict[str, Any]) -> Path:
        metadata = plan.get("metadata", {}) if isinstance(plan.get("metadata"), dict) else {}
        recording_defaults = metadata.get("recording_defaults", {}) if isinstance(metadata.get("recording_defaults"), dict) else {}
        output_root = recording_defaults.get("output_dir") or self._config.get("acquisition", {}).get("save_dir", "./experiments")
        return Path(str(output_root))

    def _create_run_artifacts(
        self,
        plan: Dict[str, Any],
        validation: Dict[str, Any],
        preflight_report: Dict[str, Any],
    ) -> tuple[str, Path]:
        metadata = plan.get("metadata", {}) if isinstance(plan.get("metadata"), dict) else {}
        plan_hash = self._stable_hash(plan)
        manifest = RunManifest.create(
            plan_hash,
            name=str(metadata.get("name") or "run"),
        )
        manifest.operator = str(metadata.get("operator", ""))
        manifest.safety_profile = str(metadata.get("safety_profile", "default"))
        manifest.status = "running"

        run_dir = self._output_root_for_plan(plan) / manifest.run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "compiled_plan.json").write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
        (run_dir / "draft_snapshot.json").write_text(
            json.dumps(metadata.get("draft_snapshot", {}), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        (run_dir / "compile_report.json").write_text(
            json.dumps({"valid": validation.get("valid"), "errors": validation.get("errors", []), "plan_hash": plan_hash}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        (run_dir / "preflight_report.json").write_text(json.dumps(preflight_report, ensure_ascii=False, indent=2), encoding="utf-8")
        (run_dir / "events.jsonl").write_text("", encoding="utf-8")
        manifest.write(run_dir)
        self._write_run_event("run_started", {"run_id": manifest.run_id, "plan_hash": plan_hash}, run_dir=run_dir)
        return manifest.run_id, run_dir

    def _write_run_event(self, event_type: str, payload: Dict[str, Any], *, run_dir: Optional[Path] = None) -> None:
        target_dir = run_dir or self._experiment_run_dir
        if target_dir is None:
            return
        event = {
            "time": datetime.datetime.now(datetime.UTC).isoformat(),
            "type": event_type,
            **payload,
        }
        try:
            with (target_dir / "events.jsonl").open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(event, ensure_ascii=False) + "\n")
        except Exception:
            pass

    def _update_run_manifest_status(self, status: str) -> None:
        if self._experiment_run_dir is None:
            return
        path = self._experiment_run_dir / "run_manifest.json"
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
            manifest["status"] = status
            manifest["end_time"] = datetime.datetime.now(datetime.UTC).isoformat()
            path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass

    def _start_experiment(self, plan: Dict[str, Any]) -> dict:
        if self._experiment_thread is not None and self._experiment_thread.is_alive():
            raise RuntimeError("实验正在运行")
        validation = self._validate_experiment_plan(plan)
        if not validation["valid"]:
            raise ValueError("; ".join(validation["errors"]))
        preflight = ExperimentPreflight(self._ctrl, self._config).run(plan)
        if not preflight.ok:
            raise ValueError("Preflight failed")
        run_id, run_dir = self._create_run_artifacts(plan, validation, preflight.to_dict())
        resources = self._required_resources(plan)
        self._experiment_run_id = run_id
        self._experiment_run_dir = run_dir
        try:
            self._resource_manager.acquire_for_run(run_id, resources)
        except Exception as exc:
            self._write_run_event("run_failed", {"run_id": run_id, "error": str(exc)}, run_dir=run_dir)
            self._update_run_manifest_status("failed")
            self._resource_manager.release(run_id)
            self._experiment_run_id = ""
            self._experiment_run_dir = None
            raise
        self._experiment_plan = plan
        self._experiment_stop.clear()
        self._experiment_pause.clear()
        self._experiment_manual_advance.clear()
        self._experiment_state = {
            "running": True,
            "paused": False,
            "step_index": -1,
            "error": "",
            "run_id": run_id,
            "run_dir": str(run_dir),
            "leased_resources": resources,
        }
        self._experiment_thread = threading.Thread(
            target=self._run_experiment_plan,
            args=(plan, run_id),
            name="ODMRExperimentEngine",
            daemon=True,
        )
        self._experiment_thread.start()
        return self._experiment_state.copy()

    def _run_experiment_plan(self, plan: Dict[str, Any], run_id: str = "") -> None:
        final_status = "completed"
        try:
            sequence = plan.get("sequence", {})
            steps = sequence.get("steps", sequence if isinstance(sequence, list) else [])
            loop_count = int(sequence.get("loop_count", 1)) if isinstance(sequence, dict) else 1
            loop_delay = float(sequence.get("loop_delay", 0.0)) if isinstance(sequence, dict) else 0.0
            return_to_zero = bool(sequence.get("return_to_zero", False)) if isinstance(sequence, dict) else False
            defaults = plan.get("defaults", {})
            default_settle = float(defaults.get("settle_s", 0.0))
            default_hold = float(defaults.get("hold_s", 0.0))
            loop = 0
            # 支持参数动态调整：每个步骤的 overrides 缓存
            step_overrides: Dict[int, Dict[str, Any]] = {}
            idx = 0
            while not self._experiment_stop.is_set() and (loop_count == 0 or loop < loop_count):
                idx = 0
                while idx < len(steps):
                    if self._experiment_stop.is_set():
                        break
                    step = steps[idx]
                    self._wait_experiment_unpaused()
                    self._experiment_state.update({"running": True, "paused": False, "step_index": idx, "loop_index": loop, "step_name": step.get("name", "")})
                    self._write_run_event("step_started", {
                        "run_id": run_id,
                        "loop_index": loop,
                        "step_index": idx,
                        "step_name": step.get("name", ""),
                    })

                    # ---- 条件分支步骤 ----
                    if step.get("type") == "conditional":
                        cond = step.get("condition", {})
                        met = self._evaluate_condition(cond)
                        self._experiment_state["condition_result"] = met
                        if met:
                            target = step.get("then_step")
                        else:
                            target = step.get("else_step")
                        if target is not None:
                            target_idx = self._find_step_index(steps, target)
                            if target_idx >= 0:
                                idx = target_idx
                                continue
                        # 目标未找到或未指定，跳过此步骤
                        idx += 1
                        continue

                    # ---- 等待步骤 ----
                    if step.get("type") == "wait":
                        self._experiment_state["phase"] = "waiting"
                        duration = float(step.get("duration_s", 0.0))
                        condition = step.get("condition")
                        if condition:
                            # 等待条件满足（带超时）
                            timeout = duration if duration > 0 else 300.0  # 默认 5 分钟超时
                            self._wait_for_condition(condition, timeout)
                        elif duration > 0:
                            self._interruptible_experiment_sleep(duration)
                        idx += 1
                        continue

                    # ---- 应用动态参数覆盖 ----
                    effective_step = self._apply_parameter_overrides(step, step_overrides.get(idx, {}))

                    timing = effective_step.get("timing", {})
                    settle = float(timing.get("settle_s", effective_step.get("settle_s", default_settle)))
                    hold = float(timing.get("hold_s", effective_step.get("hold_s", default_hold)))
                    trigger = timing.get("trigger", effective_step.get("trigger", "immediate"))
                    acq = effective_step.get("acquisition", {}) if isinstance(effective_step.get("acquisition", {}), dict) else {}
                    start_trigger = acq.get("start_trigger", "step_start")
                    stop_trigger = acq.get("stop_trigger", "hold_elapsed")

                    # ---- 条件触发：start_trigger == "condition_met" ----
                    if start_trigger == "condition_met":
                        cond = acq.get("start_condition", {})
                        self._wait_for_condition(cond, float(acq.get("condition_timeout_s", 300.0)))

                    if start_trigger == "step_start":
                        self._apply_acquisition_action(effective_step, start=True)
                    self._apply_experiment_step(effective_step)
                    if start_trigger in {"setpoints_applied", "microwave_output_on", "microwave_sweep_start"}:
                        self._apply_acquisition_action(effective_step, start=True)
                    if trigger == "manual":
                        self._experiment_manual_advance.clear()
                        while not self._experiment_stop.is_set() and not self._experiment_manual_advance.wait(0.1):
                            self._wait_experiment_unpaused()
                    elif trigger == "delay":
                        self._interruptible_experiment_sleep(float(timing.get("delay_s", 0.0)))
                    self._interruptible_experiment_sleep(settle)
                    if start_trigger == "magnetic_settled":
                        self._apply_acquisition_action(effective_step, start=True)
                    self._interruptible_experiment_sleep(hold)

                    # ---- 条件触发：stop_trigger == "condition_met" ----
                    if stop_trigger == "condition_met":
                        cond = acq.get("stop_condition", {})
                        self._wait_for_condition(cond, float(acq.get("condition_timeout_s", 300.0)))

                    if stop_trigger == "manual":
                        self._experiment_manual_advance.clear()
                        while not self._experiment_stop.is_set() and not self._experiment_manual_advance.wait(0.1):
                            self._wait_experiment_unpaused()
                    if stop_trigger in {"hold_elapsed", "timed", "microwave_sweep_complete", "manual", "step_end", "condition_met"}:
                        self._apply_acquisition_action(effective_step, start=False)
                    self._write_run_event("step_finished", {
                        "run_id": run_id,
                        "loop_index": loop,
                        "step_index": idx,
                        "step_name": step.get("name", ""),
                    })

                    # ---- 记录 parameter_overrides 供后续步骤使用 ----
                    if "parameter_overrides" in step:
                        for target_name, override in step["parameter_overrides"].items():
                            target_idx = self._find_step_index(steps, target_name)
                            if target_idx >= 0:
                                step_overrides.setdefault(target_idx, {}).update(override)

                    idx += 1
                loop += 1
                has_next_loop = loop_count == 0 or loop < loop_count
                if has_next_loop and loop_delay > 0 and not self._experiment_stop.is_set():
                    self._interruptible_experiment_sleep(loop_delay)
            if return_to_zero and not self._experiment_stop.is_set():
                self._execute(Command(
                    CommandType.MAG_SET_FIELD_3D,
                    {"x_nT": 0.0, "y_nT": 0.0, "z_nT": 0.0},
                    source="experiment",
                ))
        except Exception as exc:
            final_status = "failed"
            self._experiment_state["error"] = str(exc)
            self._write_run_event("run_failed", {"run_id": run_id, "error": str(exc)})
            self._apply_experiment_safety()
        finally:
            if self._experiment_stop.is_set() and final_status == "completed":
                final_status = "aborted"
            self._write_run_event("run_finished", {"run_id": run_id, "status": final_status})
            self._update_run_manifest_status(final_status)
            if run_id:
                self._resource_manager.release(run_id)
            self._experiment_state["running"] = False

    def _wait_experiment_unpaused(self) -> None:
        while self._experiment_pause.is_set() and not self._experiment_stop.is_set():
            self._experiment_state["paused"] = True
            time.sleep(0.1)
        self._experiment_state["paused"] = False

    def _interruptible_experiment_sleep(self, seconds: float) -> None:
        end = time.monotonic() + max(0.0, seconds)
        while time.monotonic() < end and not self._experiment_stop.is_set():
            self._wait_experiment_unpaused()
            time.sleep(min(0.1, end - time.monotonic()))

    # -- 条件评估与等待 --------------------------------------------------------

    _COMPARISON_OPS = {
        ">": lambda a, b: a > b,
        ">=": lambda a, b: a >= b,
        "<": lambda a, b: a < b,
        "<=": lambda a, b: a <= b,
        "==": lambda a, b: a == b,
        "!=": lambda a, b: a != b,
    }

    def _evaluate_condition(self, condition: Dict[str, Any]) -> bool:
        """评估单个条件表达式，返回是否满足。

        支持的条件类型:
        - field_magnitude: 磁场幅值比较 {axis, operator, value}
        - field_stable: 磁场稳定检测 {axis, tolerance_nT, window_s?}
        - lockin_value: 锁相放大器值比较 {channel, param, operator, value}
        - microwave_freq: 微波频率比较 {operator, value_hz}
        - always: 始终为真 {}
        - never: 始终为假 {}
        - composite: 组合条件 {logic: "and"|"or", conditions: [...]}
        """
        if not isinstance(condition, dict):
            return False
        cond_type = condition.get("type", "")

        if cond_type == "always":
            return True
        if cond_type == "never":
            return False

        if cond_type == "field_magnitude":
            return self._evaluate_field_magnitude(condition)
        if cond_type == "field_stable":
            return self._evaluate_field_stable(condition)
        if cond_type == "lockin_value":
            return self._evaluate_lockin_value(condition)
        if cond_type == "microwave_freq":
            return self._evaluate_microwave_freq(condition)
        if cond_type == "composite":
            return self._evaluate_composite(condition)

        return False

    def _evaluate_field_magnitude(self, condition: Dict[str, Any]) -> bool:
        """比较指定轴磁场幅值。"""
        axis = str(condition.get("axis", "X")).upper()
        op = condition.get("operator", ">")
        value = float(condition.get("value", 0.0))
        compare_fn = self._COMPARISON_OPS.get(op)
        if compare_fn is None:
            return False
        try:
            state = self._ctrl.mag.query_state(axis)
            current = float(state.get("field_nT", 0.0))
            return compare_fn(current, value)
        except Exception:
            return False

    def _evaluate_field_stable(self, condition: Dict[str, Any]) -> bool:
        """检测磁场是否在容差范围内稳定。"""
        axis = str(condition.get("axis", "X")).upper()
        tolerance = float(condition.get("tolerance_nT", 10.0))
        window = float(condition.get("window_s", 1.0))
        try:
            state = self._ctrl.mag.query_state(axis)
            current = float(state.get("field_nT", 0.0))
            samples = [current]
            sample_count = max(3, int(window / 0.2))
            for _ in range(sample_count):
                time.sleep(0.2)
                if self._experiment_stop.is_set():
                    return False
                state = self._ctrl.mag.query_state(axis)
                samples.append(float(state.get("field_nT", 0.0)))
            avg = sum(samples) / len(samples)
            return all(abs(s - avg) <= tolerance for s in samples)
        except Exception:
            return False

    def _evaluate_lockin_value(self, condition: Dict[str, Any]) -> bool:
        """比较锁相放大器参数值。"""
        channel = int(condition.get("channel", 1))
        param = condition.get("param", "X")
        op = condition.get("operator", ">")
        value = float(condition.get("value", 0.0))
        compare_fn = self._COMPARISON_OPS.get(op)
        if compare_fn is None:
            return False
        try:
            data = self._ctrl.lockin.query_snapd(channel)
            param_map = {"X": 0, "Y": 1, "R": 2, "theta": 3}
            idx = param_map.get(param.upper(), 0)
            current = float(data[idx]) if len(data) > idx else 0.0
            return compare_fn(current, value)
        except Exception:
            return False

    def _evaluate_microwave_freq(self, condition: Dict[str, Any]) -> bool:
        """比较微波源当前频率。"""
        op = condition.get("operator", ">")
        value = float(condition.get("value_hz", 0.0))
        compare_fn = self._COMPARISON_OPS.get(op)
        if compare_fn is None:
            return False
        try:
            current = self._ctrl.smb.cached_freq_hz
            return compare_fn(current, value)
        except Exception:
            return False

    def _evaluate_composite(self, condition: Dict[str, Any]) -> bool:
        """组合条件评估（and/or 逻辑）。"""
        logic = condition.get("logic", "and")
        conditions = condition.get("conditions", [])
        if not conditions:
            return False
        results = [self._evaluate_condition(c) for c in conditions]
        if logic == "or":
            return any(results)
        return all(results)

    def _wait_for_condition(self, condition: Dict[str, Any], timeout_s: float) -> bool:
        """阻塞等待条件满足，支持中断和暂停。

        Returns:
            True 表示条件满足，False 表示超时或被中断。
        """
        deadline = time.monotonic() + max(0.0, timeout_s)
        poll_interval = 0.5
        while not self._experiment_stop.is_set():
            self._wait_experiment_unpaused()
            if self._evaluate_condition(condition):
                self._experiment_state["phase"] = "condition_met"
                return True
            if time.monotonic() >= deadline:
                self._experiment_state["phase"] = "condition_timeout"
                return False
            remaining = deadline - time.monotonic()
            time.sleep(min(poll_interval, max(0.05, remaining)))
        return False

    @staticmethod
    def _find_step_index(steps: list, name: Any) -> int:
        """按名称或索引查找步骤位置，未找到返回 -1。"""
        if isinstance(name, int):
            return name if 0 <= name < len(steps) else -1
        for i, s in enumerate(steps):
            if isinstance(s, dict) and s.get("name") == name:
                return i
        return -1

    @staticmethod
    def _apply_parameter_overrides(step: Dict[str, Any], overrides: Dict[str, Any]) -> Dict[str, Any]:
        """将参数覆盖合并到步骤中（深合并）。

        overrides 格式: {"magnetic_field": {"x_nT": 500}, "microwave": {"power_dbm": -10}}
        """
        if not overrides:
            return step
        merged = copy.deepcopy(step)
        for key, value in overrides.items():
            if key in merged and isinstance(merged[key], dict) and isinstance(value, dict):
                merged[key].update(value)
            else:
                merged[key] = value
        return merged

    def _apply_experiment_step(self, step: Dict[str, Any]) -> None:
        if "magnetic_field" in step:
            field = self._resolve_field_step(step["magnetic_field"])
            self._execute(Command(CommandType.MAG_SET_FIELD_3D, field, source="experiment"))
        if "microwave" in step:
            self._apply_smb_config(step["microwave"])
        if "lockin" in step:
            self._apply_lockin_config(step["lockin"])
        if "laser" in step:
            laser = step["laser"]
            if "power_mw" in laser:
                self._execute(Command(CommandType.LASER_SET_POWER, {"power_mw": laser["power_mw"]}, source="experiment"))
            if "output" in laser:
                self._execute(Command(CommandType.LASER_SET_OUTPUT, {"enabled": laser["output"]}, source="experiment"))
        if "acquisition" in step:
            acq = step["acquisition"]
            if acq.get("start_recording") and acq.get("start_trigger") == "inline":
                self._execute(Command(CommandType.ACQ_START_RECORDING, {"output_dir": acq.get("output_dir")}, source="experiment"))
            if acq.get("stop_recording") and acq.get("stop_trigger") == "inline":
                self._execute(Command(CommandType.ACQ_STOP_RECORDING, {"stop_acquire": bool(acq.get("stop_acquire", False))}, source="experiment"))

    def _apply_acquisition_action(self, step: Dict[str, Any], *, start: bool) -> None:
        acq = step.get("acquisition", {})
        if not isinstance(acq, dict):
            return
        if start:
            if acq.get("start_recording", False) and self._acq_recorder is None:
                self._experiment_state["phase"] = "acquisition_start"
                self._execute(Command(
                    CommandType.ACQ_START_RECORDING,
                    {"output_dir": acq.get("output_dir"), "column_groups": acq.get("column_groups")},
                    source="experiment",
                ))
            elif acq.get("start_stream", False) and not self._ctrl.is_acquiring:
                self._ctrl.start_lockin_acquire(None)
            return
        if acq.get("stop_recording", True) and self._acq_recorder is not None:
            self._experiment_state["phase"] = "acquisition_stop"
            self._execute(Command(
                CommandType.ACQ_STOP_RECORDING,
                {"stop_acquire": bool(acq.get("stop_acquire", False))},
                source="experiment",
            ))

    @staticmethod
    def _resolve_field_step(field: Dict[str, Any]) -> Dict[str, float]:
        if all(k in field for k in ("magnitude_nT", "theta_deg", "phi_deg")):
            cart = spherical_to_cartesian(SphericalField(
                float(field["magnitude_nT"]),
                float(field["theta_deg"]),
                float(field["phi_deg"]),
            ))
            return {"x_nT": cart.x_nT, "y_nT": cart.y_nT, "z_nT": cart.z_nT}
        return {
            "x_nT": float(field.get("x_nT", field.get("field_x_nT", 0.0))),
            "y_nT": float(field.get("y_nT", field.get("field_y_nT", 0.0))),
            "z_nT": float(field.get("z_nT", field.get("field_z_nT", 0.0))),
        }

    def _apply_experiment_safety(self) -> None:
        try:
            self._ctrl.smb.emergency_stop()
        except Exception:
            pass
        try:
            self._ctrl.mag.all_output_off()
        except Exception:
            pass
        try:
            if self._acq_recorder is not None:
                self._execute(Command(CommandType.ACQ_STOP_RECORDING, {"stop_acquire": False}, source="experiment"))
        except Exception:
            pass

    # -- state helpers -------------------------------------------------------

    def _query_smb_state(self) -> dict:
        """查询 SMB100A 完整状态。"""
        smb = self._ctrl.smb
        state = {
            "freq_hz": smb.cached_freq_hz,
            "power_dbm": smb.cached_power_dbm,
            "output_on": smb.cached_output_on,
            "mode": smb.cached_mode,
        }
        try:
            state.update(smb.query_config())
        except Exception:
            pass
        return state

    def _create_recorder(self, params: Dict[str, Any]) -> ODMRRecorder:
        """创建并启动一次 CSV 记录会话。"""
        output_dir = params.get("output_dir") or params.get("save_dir")
        column_groups = params.get("column_groups")
        recorder = ODMRRecorder(Path(output_dir) if output_dir else None, column_groups=column_groups)
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
