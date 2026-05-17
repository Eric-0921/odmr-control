from __future__ import annotations

import queue
from typing import Dict, List, Optional, Union

from PyQt5.QtCore import QObject, QThread, pyqtSignal

from instruments.magnet_coil import MagnetCoilController
from workers.mag_poll_worker import AxisPollWorker

AXES = ("X", "Y", "Z")

# unit conversion (all relative to nT)
UNIT_TO_NT = {"nT": 1.0, "μT": 1000.0, "uT": 1000.0, "mT": 1e6, "T": 1e9, "G": 100000.0, "Oe": 100000.0}


class FieldController(QObject):
    """Unified API for 3-axis magnetic field control.

    GUI and SequenceEngine both operate hardware through this facade.
    Internally manages three MagnetCoilController instances, three QThreads,
    three AxisPollWorkers, and three command queues.
    """

    # signals
    current_changed = pyqtSignal(str, float)   # (axis, mA)
    field_changed = pyqtSignal(str, float)     # (axis, nT)
    error_occurred = pyqtSignal(str, str)      # (axis, msg)
    connection_changed = pyqtSignal(str, bool) # (axis, connected)
    log_requested = pyqtSignal(str)

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._controllers: Dict[str, MagnetCoilController] = {
            a: MagnetCoilController(a) for a in AXES
        }
        self._queues: Dict[str, queue.Queue] = {a: queue.Queue() for a in AXES}
        self._workers: Dict[str, Optional[AxisPollWorker]] = {a: None for a in AXES}
        self._threads: Dict[str, Optional[QThread]] = {a: None for a in AXES}
        self._latest_current: Dict[str, float] = {a: 0.0 for a in AXES}  # measured total current, mA
        self._recur_current: Dict[str, float] = {a: 0.0 for a in AXES}   # requested reproduction current, mA
        self._poll_interval_ms = 500

    # -- connection ----------------------------------------------------------

    def connect_axis(self, axis: str, port: str, baudrate: int = 9600) -> str:
        ctrl = self._controllers[axis]
        idn = ctrl.connect(port, baudrate)
        ctrl.set_remote()
        ctrl.set_voltage(75.0)
        ctrl.set_current(0.0)
        ctrl.set_output(False)
        self._start_poll(axis)
        self.connection_changed.emit(axis, True)
        return idn

    def disconnect_axis(self, axis: str) -> None:
        self._stop_poll(axis)
        self._controllers[axis].close()
        self._latest_current[axis] = 0.0
        self._recur_current[axis] = 0.0
        self.connection_changed.emit(axis, False)

    def is_connected(self, axis: str) -> bool:
        return self._controllers[axis].is_connected

    def connected_axes(self) -> List[str]:
        return [a for a in AXES if self._controllers[a].is_connected]

    def connect_all(self, ports: Dict[str, str], baudrate: int = 9600) -> Dict[str, str]:
        """Connect all axes. Returns {axis: idn_or_error}."""
        results = {}
        for axis in AXES:
            if axis in ports and ports[axis]:
                try:
                    results[axis] = self.connect_axis(axis, ports[axis], baudrate)
                except Exception as exc:
                    results[axis] = f"ERROR: {exc}"
        return results

    def disconnect_all(self) -> None:
        for axis in AXES:
            if self.is_connected(axis):
                self.disconnect_axis(axis)

    def check_all_axes(self) -> Dict[str, str]:
        """Hardware self-test: send *IDN? to all connected axes.

        Returns {axis: idn_string_or_error}.
        """
        results: Dict[str, str] = {}
        for axis in AXES:
            if self.is_connected(axis):
                try:
                    results[axis] = self._controllers[axis].identify()
                except Exception as exc:
                    results[axis] = f"ERROR: {exc}"
            else:
                results[axis] = "未连接 / Not connected"
        return results

    @staticmethod
    def scan_device_ports(baudrate: int = 9600) -> List[Dict[str, str]]:
        """Scan all COM ports and return those that respond to *IDN?.

        Returns list of {"port": "COM3", "idn": "manufacturer,model,serial"}.
        """
        try:
            import serial as _serial
            import serial.tools.list_ports as _list_ports
            import time as _time
        except ImportError:
            return []
        results: List[Dict[str, str]] = []
        for info in _list_ports.comports():
            port = info.device
            try:
                sp = _serial.Serial(
                    port=port, baudrate=baudrate, bytesize=8, parity="N",
                    stopbits=1, timeout=0.1, dsrdtr=False,
                )
                sp.dtr = True
                sp.reset_input_buffer()
                sp.reset_output_buffer()
                _time.sleep(0.05)
                sp.write(b"*IDN?\n")
                sp.flush()
                _time.sleep(0.1)
                resp = sp.readline().decode("ascii", errors="replace").strip()
                sp.close()
                if resp and "," in resp:
                    results.append({"port": port, "idn": resp})
            except Exception:
                pass
        return results

    @staticmethod
    def _normalize_binding(value: Union[str, Dict[str, str], None]) -> Dict[str, str]:
        """将绑定值统一转换为 {"idn": "...", "port": "..."} 格式。

        兼容旧格式（纯 IDN 字符串）、新格式（含 idn + port 的 dict）及 None。
        """
        if value is None:
            return {"idn": "", "port": ""}
        if isinstance(value, dict):
            return {
                "idn": str(value.get("idn", "")),
                "port": str(value.get("port", "")),
            }
        # 旧格式：纯 IDN 字符串
        return {"idn": str(value), "port": ""}

    @staticmethod
    def match_devices_to_axes(
        detected: List[Dict[str, str]],
        bindings: Dict[str, Union[str, Dict[str, str]]],
    ) -> Dict[str, Optional[str]]:
        """Match detected devices to axes using stored IDN bindings.

        Returns {axis: port} for matched axes, {axis: None} for unmatched.
        If all bindings are empty, falls back to index-based assignment.

        bindings 兼容两种格式:
          - 旧格式: {"X": "IDN字符串", ...}
          - 新格式: {"X": {"idn": "...", "port": "..."}, ...}
        """
        result: Dict[str, Optional[str]] = {a: None for a in AXES}
        # 统一绑定格式并提取 IDN
        normalized: Dict[str, Dict[str, str]] = {}
        for axis in AXES:
            raw = bindings.get(axis, "")
            normalized[axis] = FieldController._normalize_binding(raw)
        # Check if any bindings exist
        has_bindings = any(normalized[a]["idn"] for a in AXES)
        if not has_bindings:
            # Fallback: index-based assignment
            for i, axis in enumerate(AXES):
                if i < len(detected):
                    result[axis] = detected[i]["port"]
            return result
        # Match by IDN
        idn_to_port = {d["idn"]: d["port"] for d in detected}
        for axis in AXES:
            bound_idn = normalized[axis]["idn"]
            if bound_idn and bound_idn in idn_to_port:
                result[axis] = idn_to_port[bound_idn]
        return result

    # -- field operations ----------------------------------------------------

    def set_field(self, axis: str, nT: float) -> bool:
        ctrl = self._controllers[axis]
        if nT < 0:
            self._reject(axis, f"负磁场请求被阻止: {nT:.2f} nT")
            return False
        if ctrl.coil_constant <= 0:
            self._reject(axis, f"线圈常数必须为正数: {ctrl.coil_constant}")
            return False
        ma = nT / ctrl.coil_constant
        return self.set_current(axis, ma)

    def get_field(self, axis: str) -> float:
        return self._recur_current[axis] * self._controllers[axis].coil_constant

    def set_current(self, axis: str, mA: float) -> bool:
        if mA < 0:
            self._reject(axis, f"负电流请求被阻止: {mA:.5f} mA")
            return False
        ctrl = self._controllers[axis]
        if mA - ctrl.MAX_CURRENT_MA > 0.001:
            self._reject(axis, f"复现电流超出上限: {mA:.5f} mA > {ctrl.MAX_CURRENT_MA:.1f} mA")
            return False
        if ctrl.output_on and ctrl.lock_zero and not self._validate_total_current(axis, recur_current=mA):
            return False
        self._recur_current[axis] = mA
        if ctrl.output_on and ctrl.lock_zero:
            self._queue_total_current(axis)
        return True

    def get_current(self, axis: str) -> float:
        return self._latest_current[axis]

    def set_output(self, axis: str, on: bool) -> bool:
        if on:
            if not self._validate_total_current(axis):
                return False
            self._queue_total_current(axis)
        else:
            self._queues[axis].put(("set_current", (0.0,)))
        self._queues[axis].put(("set_output", (on,)))
        return True

    def set_zero_offset(self, axis: str, mA: float) -> bool:
        if mA < 0:
            self._reject(axis, f"负零偏电流请求被阻止: {mA:.5f} mA")
            return False
        ctrl = self._controllers[axis]
        if mA - ctrl.MAX_CURRENT_MA > 0.001:
            self._reject(axis, f"零偏电流超出上限: {mA:.5f} mA > {ctrl.MAX_CURRENT_MA:.1f} mA")
            return False
        if ctrl.output_on and not self._validate_total_current(axis, zero_offset=mA):
            return False
        self._controllers[axis].zero_offset = mA
        if ctrl.output_on:
            self._queue_total_current(axis)
        return True

    def lock_zero(self, axis: str, locked: bool) -> bool:
        ctrl = self._controllers[axis]
        if ctrl.output_on and not self._validate_total_current(axis, locked=locked):
            return False
        ctrl.lock_zero = locked
        if ctrl.output_on:
            self._queue_total_current(axis)
        return True

    def capture_background_as_zero(self, axis: str) -> float:
        """Read back the present PSU current and use it as the zero/background offset.

        The coil PSU cannot measure the ambient magnetic field directly. In this
        control model the "background" is the compensation current that is
        already present on the axis. Capturing it makes the old workflow explicit:
        output compensation current, read it back, store as zero offset, then lock
        zero before applying reproduction field current.
        """
        ctrl = self._controllers[axis]
        if not ctrl.is_connected:
            raise ConnectionError(f"{axis} 轴未连接")
        ma = max(0.0, ctrl.get_current())
        ctrl.update_cached_current(ma)
        self._latest_current[axis] = ma
        if not self.set_zero_offset(axis, ma):
            raise ValueError(f"{axis} 轴背景零偏捕获失败")
        self._recur_current[axis] = 0.0
        return ma

    def prepare_zero_lock(self, axis: str, *, capture_readback: bool = False) -> bool:
        """Start output at the zero offset and lock the zero/background state."""
        ctrl = self._controllers[axis]
        if not ctrl.is_connected:
            raise ConnectionError(f"{axis} 轴未连接")
        self._recur_current[axis] = 0.0
        if not self.set_output(axis, True):
            return False
        if capture_readback:
            try:
                self.capture_background_as_zero(axis)
            except Exception as exc:
                self._reject(axis, f"背景零偏回读失败: {exc}")
                return False
        return self.lock_zero(axis, True)

    def set_voltage(self, axis: str, v: float) -> None:
        self._queues[axis].put(("set_voltage", (v,)))

    def get_output_on(self, axis: str) -> bool:
        return self._controllers[axis].output_on

    def get_lock_zero(self, axis: str) -> bool:
        return self._controllers[axis].lock_zero

    def get_zero_offset(self, axis: str) -> float:
        return self._controllers[axis].zero_offset

    def get_coil_constant(self, axis: str) -> float:
        return self._controllers[axis].coil_constant

    def set_coil_constant(self, axis: str, value: float) -> None:
        self._controllers[axis].coil_constant = value

    def get_max_current(self, axis: str) -> float:
        return self._controllers[axis].MAX_CURRENT_MA

    def get_max_field(self, axis: str) -> float:
        return self._controllers[axis].MAX_CURRENT_MA * self._controllers[axis].coil_constant

    def get_poll_interval_ms(self) -> int:
        return self._poll_interval_ms

    def set_poll_interval_ms(self, interval_ms: int) -> None:
        interval = int(interval_ms)
        if interval < 100 or interval > 5000:
            raise ValueError("轮询间隔必须在 100–5000 ms 范围内")
        if interval == self._poll_interval_ms:
            return
        self._poll_interval_ms = interval
        for axis in AXES:
            if self.is_connected(axis):
                self._restart_poll(axis)

    # -- convenience ---------------------------------------------------------

    def set_field_3d(self, x_nT: float = 0.0, y_nT: float = 0.0, z_nT: float = 0.0) -> None:
        for axis, nT in zip(AXES, (x_nT, y_nT, z_nT)):
            if self.is_connected(axis):
                self.set_field(axis, nT)

    def all_output_off(self) -> None:
        for axis in AXES:
            if self.is_connected(axis):
                self.set_output(axis, False)

    def set_serial_log_callback(self, callback) -> None:
        for ctrl in self._controllers.values():
            ctrl.set_raw_log_callback(callback)

    def is_any_worker_running(self) -> bool:
        return any(thread is not None and thread.isRunning() for thread in self._threads.values())

    def verify_emergency_stop(self) -> Dict[str, Dict[str, object]]:
        """Read back output state and current from all connected axes."""
        results: Dict[str, Dict[str, object]] = {}
        for axis in AXES:
            if not self.is_connected(axis):
                continue
            ctrl = self._controllers[axis]
            try:
                output_on = ctrl.get_output_state()
                current_ma = ctrl.get_current()
                ok = (not output_on) and current_ma < 1.0
                results[axis] = {"output_on": output_on, "current_mA": current_ma, "ok": ok}
            except Exception as exc:
                results[axis] = {"output_on": True, "current_mA": -1.0, "ok": False, "error": str(exc)}
        return results

    def get_status(self, axis: str) -> dict:
        ctrl = self._controllers[axis]
        ma = self._latest_current[axis]
        desired_total = self._desired_total_current(axis)
        estimated_recur = None
        estimated_field = None
        if ctrl.output_on and ctrl.lock_zero:
            estimated_recur = ma - ctrl.zero_offset
            estimated_field = estimated_recur * ctrl.coil_constant
        target_field = self._recur_current[axis] * ctrl.coil_constant
        return {
            "axis": axis,
            "connected": ctrl.is_connected,
            "output_on": ctrl.output_on,
            "lock_zero": ctrl.lock_zero,
            "current_mA": ma,
            "total_current_mA": ma,
            "desired_total_current_mA": desired_total,
            "recur_current_mA": self._recur_current[axis],
            "target_field_nT": target_field,
            "field_nT": target_field,
            "estimated_recur_current_mA": estimated_recur,
            "estimated_field_nT": estimated_field,
            "zero_offset_mA": ctrl.zero_offset,
            "coil_constant": ctrl.coil_constant,
            "max_current_mA": ctrl.MAX_CURRENT_MA,
            "current_margin_mA": ctrl.MAX_CURRENT_MA - desired_total,
        }

    def get_field_snapshot(self) -> dict:
        """Return a read-only 3-axis snapshot for UI visualization."""
        axes = {axis: self.get_status(axis) for axis in AXES}
        target = {axis: axes[axis]["target_field_nT"] for axis in AXES}
        estimated = {
            axis: axes[axis]["estimated_field_nT"]
            for axis in AXES
        }
        estimated_available = all(value is not None for value in estimated.values())
        return {
            "axes": axes,
            "target_field_nT": target,
            "estimated_field_nT": estimated if estimated_available else None,
            "estimated_available": estimated_available,
        }

    def _desired_total_current(
        self,
        axis: str,
        *,
        zero_offset: Optional[float] = None,
        recur_current: Optional[float] = None,
        locked: Optional[bool] = None,
    ) -> float:
        ctrl = self._controllers[axis]
        zero = ctrl.zero_offset if zero_offset is None else zero_offset
        recur = self._recur_current[axis] if recur_current is None else recur_current
        is_locked = ctrl.lock_zero if locked is None else locked
        if is_locked:
            return zero + recur
        return zero

    def _validate_total_current(
        self,
        axis: str,
        *,
        zero_offset: Optional[float] = None,
        recur_current: Optional[float] = None,
        locked: Optional[bool] = None,
    ) -> bool:
        ctrl = self._controllers[axis]
        total = self._desired_total_current(
            axis,
            zero_offset=zero_offset,
            recur_current=recur_current,
            locked=locked,
        )
        if total < 0:
            self._reject(axis, f"总输出电流为负，已阻止: {total:.5f} mA")
            return False
        if total - ctrl.MAX_CURRENT_MA > 0.001:
            self._reject(axis, f"总输出电流超出上限: {total:.5f} mA > {ctrl.MAX_CURRENT_MA:.1f} mA")
            return False
        return True

    def _queue_total_current(self, axis: str) -> None:
        self._queues[axis].put(("set_current", (self._desired_total_current(axis),)))

    def _reject(self, axis: str, msg: str) -> None:
        self.error_occurred.emit(axis, msg)
        self.log_requested.emit(f"[{axis}] {msg}")

    # -- internal poll management --------------------------------------------

    def _start_poll(self, axis: str) -> None:
        if self._workers[axis] is not None:
            return
        ctrl = self._controllers[axis]
        q = self._queues[axis]
        worker = AxisPollWorker(ctrl, q, interval_ms=self._poll_interval_ms)
        thread = QThread(self)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.current_read.connect(self._on_current_read)
        worker.log_requested.connect(self.log_requested)
        worker.error_occurred.connect(lambda msg: self._on_error(axis, msg))
        worker.finished.connect(lambda _: self._on_poll_finished(axis))
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        self._workers[axis] = worker
        self._threads[axis] = thread
        thread.start()

    def _restart_poll(self, axis: str) -> None:
        self._stop_poll(axis)
        self._start_poll(axis)
        self.log_requested.emit(f"[{axis}] 轮询间隔已更新为 {self._poll_interval_ms} ms")

    def _stop_poll(self, axis: str) -> None:
        worker = self._workers[axis]
        if worker is not None:
            worker.stop()
        thread = self._threads[axis]
        if thread is not None:
            thread.wait(3000)
        self._workers[axis] = None
        self._threads[axis] = None

    def _on_poll_finished(self, axis: str) -> None:
        self._workers[axis] = None
        self._threads[axis] = None

    def _on_current_read(self, axis: str, ma: float) -> None:
        self._latest_current[axis] = ma
        self.current_changed.emit(axis, ma)
        nT = self._recur_current[axis] * self._controllers[axis].coil_constant
        self.field_changed.emit(axis, nT)

    def _on_error(self, axis: str, msg: str) -> None:
        self.error_occurred.emit(axis, msg)
