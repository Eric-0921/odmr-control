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
import json
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from PyQt5.QtCore import QObject, QThread, pyqtSignal

from core.commands import Command, CommandType
from core.instrument_controller import InstrumentController
from core.mag_sequence_engine import FieldSequence, FieldStep, SequenceEngine
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
                )
            if "shape" in sweep:
                self._ctrl.smb.set_sweep_shape(str(sweep["shape"]))
            if "retrace" in sweep:
                self._ctrl.smb.set_sweep_retrace(bool(sweep["retrace"]))
            if "trigger" in sweep:
                self._ctrl.smb.set_sweep_trigger_source(str(sweep["trigger"]))
        if "rf_output" in cfg:
            self._ctrl.smb.set_output(bool(cfg["rf_output"]))
        return self._ctrl.smb.query_config()

    def _apply_lockin_config(self, cfg: Dict[str, Any]) -> dict:
        channel = int(cfg.get("channel", 1))
        if "input" in cfg:
            data = dict(cfg["input"], channel=channel)
            self._execute(Command(CommandType.LOCKIN_SET_INPUT, data, source="system"))
        if "ref" in cfg:
            data = dict(cfg["ref"], channel=channel)
            self._execute(Command(CommandType.LOCKIN_SET_REF_PHASE, data, source="system"))
        if "gain_tc" in cfg:
            data = dict(cfg["gain_tc"], channel=channel)
            self._execute(Command(CommandType.LOCKIN_SET_GAIN_TC, data, source="system"))
        if "output" in cfg:
            data = dict(cfg["output"], channel=channel)
            self._execute(Command(CommandType.LOCKIN_SET_OUTPUT, data, source="system"))
        if "auto" in cfg:
            auto = cfg["auto"]
            if auto.get("gain"):
                self._ctrl.lockin.auto_gain(channel)
            if auto.get("reserve"):
                self._ctrl.lockin.auto_reserve(channel)
            if auto.get("phase"):
                self._ctrl.lockin.auto_phase(channel)
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

    def _mag_axis_bindings(self) -> Dict[str, str]:
        raw = self._config.get("magnetic_field", {}).get("bindings", {})
        result: Dict[str, str] = {}
        for axis in ("X", "Y", "Z"):
            value = raw.get(axis, "")
            result[axis] = value.get("idn", "") if isinstance(value, dict) else str(value)
        return result

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
            return json.loads(Path(params["path"]).read_text(encoding="utf-8"))
        return params

    def _validate_experiment_plan(self, plan: Dict[str, Any]) -> dict:
        errors = []
        sequence = plan.get("sequence", [])
        if isinstance(sequence, dict):
            steps = sequence.get("steps", [])
        else:
            steps = sequence
        if not isinstance(steps, list) or not steps:
            errors.append("sequence.steps 不能为空")
        for idx, step in enumerate(steps if isinstance(steps, list) else []):
            if not isinstance(step, dict):
                errors.append(f"step {idx} 必须是对象")
                continue
            if "magnetic_field" in step:
                self._resolve_field_step(step["magnetic_field"])
        return {"valid": not errors, "errors": errors, "steps": len(steps) if isinstance(steps, list) else 0}

    def _start_experiment(self, plan: Dict[str, Any]) -> dict:
        if self._experiment_thread is not None and self._experiment_thread.is_alive():
            raise RuntimeError("实验正在运行")
        validation = self._validate_experiment_plan(plan)
        if not validation["valid"]:
            raise ValueError("; ".join(validation["errors"]))
        self._experiment_plan = plan
        self._experiment_stop.clear()
        self._experiment_pause.clear()
        self._experiment_manual_advance.clear()
        self._experiment_state = {"running": True, "paused": False, "step_index": -1, "error": ""}
        self._experiment_thread = threading.Thread(
            target=self._run_experiment_plan,
            args=(plan,),
            name="ODMRExperimentEngine",
            daemon=True,
        )
        self._experiment_thread.start()
        return self._experiment_state.copy()

    def _run_experiment_plan(self, plan: Dict[str, Any]) -> None:
        try:
            sequence = plan.get("sequence", {})
            steps = sequence.get("steps", sequence if isinstance(sequence, list) else [])
            loop_count = int(sequence.get("loop_count", 1)) if isinstance(sequence, dict) else 1
            loop = 0
            while not self._experiment_stop.is_set() and (loop_count == 0 or loop < loop_count):
                for idx, step in enumerate(steps):
                    if self._experiment_stop.is_set():
                        break
                    self._wait_experiment_unpaused()
                    self._experiment_state.update({"running": True, "paused": False, "step_index": idx, "loop_index": loop, "step_name": step.get("name", "")})
                    self._apply_experiment_step(step)
                    timing = step.get("timing", {})
                    settle = float(timing.get("settle_s", step.get("settle_s", 0.0)))
                    hold = float(timing.get("hold_s", step.get("hold_s", 0.0)))
                    trigger = timing.get("trigger", step.get("trigger", "immediate"))
                    if trigger == "manual":
                        self._experiment_manual_advance.clear()
                        while not self._experiment_stop.is_set() and not self._experiment_manual_advance.wait(0.1):
                            self._wait_experiment_unpaused()
                    elif trigger == "delay":
                        self._interruptible_experiment_sleep(float(timing.get("delay_s", 0.0)))
                    self._interruptible_experiment_sleep(settle)
                    self._interruptible_experiment_sleep(hold)
                loop += 1
        except Exception as exc:
            self._experiment_state["error"] = str(exc)
            self._apply_experiment_safety()
        finally:
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
            if acq.get("start_recording"):
                self._execute(Command(CommandType.ACQ_START_RECORDING, {"output_dir": acq.get("output_dir")}, source="experiment"))
            if acq.get("stop_recording"):
                self._execute(Command(CommandType.ACQ_STOP_RECORDING, {"stop_acquire": bool(acq.get("stop_acquire", False))}, source="experiment"))

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
