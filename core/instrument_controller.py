"""统一仪器控制器

门面模式：GUI 和 SweepEngine 均通过此类操作硬件，
不直接访问 SMB100ADriver 或 OE1022DDriver。

职责：
- 管理两台设备的连接/断开
- 管理三个 Worker 线程（SMB 轮询、Lockin 监控、Lockin 采集）
- 执行多层安全校验
- 维护命令队列，确保 GUI 线程永不阻塞
- 发射信号同步 GUI 状态
"""

from __future__ import annotations

import queue
from typing import Callable, Dict, Optional

from PyQt5.QtCore import QObject, QThread, pyqtSignal

from data.recorder import ODMRRecorder
from instruments.laser_msl import LaserMSLDriver
from instruments.oe1022d import OE1022DDriver
from instruments.smb100a import SMB100ADriver
from workers.laser_poll_worker import LaserPollWorker
from workers.lockin_acquire_worker import LockinAcquireWorker
from workers.lockin_monitor_worker import LockinMonitorWorker
from workers.smb_poll_worker import SMBPollWorker


class InstrumentController(QObject):
    """Unified facade for SMB100A + OE1022D."""

    # signals
    smb_state_changed = pyqtSignal(float, bool, str)   # freq_hz, output_on, mode (兼容旧版)
    smb_state_dict_changed = pyqtSignal(dict)          # 完整状态字典 (新版)
    lockin_data_ready = pyqtSignal(dict)               # SNAPD? data (含 channel 字段)
    lockin_status_changed = pyqtSignal(dict)           # 过载/PLL 状态
    lockin_batch_ready = pyqtSignal(dict)              # RALL? batch
    laser_state_changed = pyqtSignal(dict)             # 激光器状态
    sweep_progress = pyqtSignal(int, int)              # current_cycle, total_cycles
    sweep_finished = pyqtSignal(bool)                  # completed_ok
    error_occurred = pyqtSignal(str)
    log_requested = pyqtSignal(str)

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._smb = SMB100ADriver()
        self._lockin = OE1022DDriver()
        self._laser = LaserMSLDriver()
        self._smb_queue: queue.Queue = queue.Queue()
        self._lockin_queue: queue.Queue = queue.Queue()
        self._laser_queue: queue.Queue = queue.Queue()

        # workers
        self._smb_worker: Optional[SMBPollWorker] = None
        self._smb_thread: Optional[QThread] = None
        self._lockin_monitor_worker: Optional[LockinMonitorWorker] = None
        self._lockin_monitor_thread: Optional[QThread] = None
        self._lockin_acquire_worker: Optional[LockinAcquireWorker] = None
        self._lockin_acquire_thread: Optional[QThread] = None
        self._laser_worker: Optional[LaserPollWorker] = None
        self._laser_thread: Optional[QThread] = None

        # recorder
        self._recorder: Optional[ODMRRecorder] = None

        # safety state
        self._sweeping = False
        self._acquiring = False

    # -- properties ----------------------------------------------------------

    @property
    def smb(self) -> SMB100ADriver:
        return self._smb

    @property
    def lockin(self) -> OE1022DDriver:
        return self._lockin

    @property
    def laser(self) -> LaserMSLDriver:
        return self._laser

    @property
    def is_smb_connected(self) -> bool:
        return self._smb.is_connected

    @property
    def is_lockin_connected(self) -> bool:
        return self._lockin.is_connected

    @property
    def is_laser_connected(self) -> bool:
        return self._laser.is_connected

    @property
    def is_sweeping(self) -> bool:
        return self._sweeping

    @property
    def is_acquiring(self) -> bool:
        return self._acquiring

    # -- SMB100A connection --------------------------------------------------

    def connect_smb(self, visa_address: str, timeout_ms: int = 10000) -> str:
        idn = self._smb.connect(visa_address, timeout_ms)
        self._start_smb_poll()
        return idn

    def disconnect_smb(self) -> None:
        self._stop_smb_poll()
        self._smb.close()

    # -- OE1022D connection --------------------------------------------------

    def connect_lockin(
        self,
        port: str,
        baudrate: int = 921600,
        bytesize: int = 8,
        parity: str = "N",
        stopbits: int = 1,
        timeout: float = 1.0,
    ) -> str:
        idn = self._lockin.connect(port, baudrate, bytesize, parity, stopbits, timeout)
        self._start_lockin_monitor()
        return idn

    def disconnect_lockin(self) -> None:
        self._stop_lockin_monitor()
        self._stop_lockin_acquire()
        self._lockin.close()

    # -- Laser connection ----------------------------------------------------

    def connect_laser(
        self,
        port: str,
        baudrate: int = 9600,
        bytesize: int = 8,
        parity: str = "N",
        stopbits: int = 1,
        timeout: float = 1.0,
    ) -> str:
        idn = self._laser.connect(port, baudrate, bytesize, parity, stopbits, timeout)
        self._start_laser_poll()
        return idn

    def disconnect_laser(self) -> None:
        self._stop_laser_poll()
        if self._laser.is_connected:
            try:
                self._laser.set_output(False)
            except Exception:
                pass
        self._laser.close()

    # -- worker management ---------------------------------------------------

    def _start_smb_poll(self, interval_ms: int = 100) -> None:
        if self._smb_thread is not None:
            return
        self._smb_worker = SMBPollWorker(self._smb, self._smb_queue, interval_ms)
        self._smb_thread = QThread(self)
        self._smb_worker.moveToThread(self._smb_thread)
        self._smb_worker.state_updated.connect(self._on_smb_state_updated)
        self._smb_worker.state_dict_updated.connect(self.smb_state_dict_changed.emit)
        self._smb_worker.log_requested.connect(self.log_requested.emit)
        self._smb_worker.error_occurred.connect(self.error_occurred.emit)
        self._smb_thread.started.connect(self._smb_worker.run)
        self._smb_thread.finished.connect(self._smb_worker.deleteLater)
        self._smb_thread.start()

    def _stop_smb_poll(self) -> None:
        if self._smb_worker is not None:
            self._smb_worker.stop()
        if self._smb_thread is not None:
            self._smb_thread.quit()
            if not self._smb_thread.wait(2000):
                self._smb_thread.terminate()
                self._smb_thread.wait(1000)
            self._smb_thread = None
        self._smb_worker = None

    def _start_lockin_monitor(self, interval_ms: int = 94) -> None:
        if self._lockin_monitor_thread is not None:
            return
        self._lockin_monitor_worker = LockinMonitorWorker(
            self._lockin, self._lockin_queue, interval_ms
        )
        self._lockin_monitor_thread = QThread(self)
        self._lockin_monitor_worker.moveToThread(self._lockin_monitor_thread)
        self._lockin_monitor_worker.data_ready.connect(self.lockin_data_ready.emit)
        self._lockin_monitor_worker.status_ready.connect(self.lockin_status_changed.emit)
        self._lockin_monitor_worker.log_requested.connect(self.log_requested.emit)
        self._lockin_monitor_worker.error_occurred.connect(self.error_occurred.emit)
        self._lockin_monitor_thread.started.connect(self._lockin_monitor_worker.run)
        self._lockin_monitor_thread.finished.connect(self._lockin_monitor_worker.deleteLater)
        self._lockin_monitor_thread.start()

    def _stop_lockin_monitor(self) -> None:
        if self._lockin_monitor_worker is not None:
            self._lockin_monitor_worker.stop()
        if self._lockin_monitor_thread is not None:
            self._lockin_monitor_thread.quit()
            if not self._lockin_monitor_thread.wait(2000):
                self._lockin_monitor_thread.terminate()
                self._lockin_monitor_thread.wait(1000)
            self._lockin_monitor_thread = None
        self._lockin_monitor_worker = None

    def _start_laser_poll(self, interval_ms: int = 1000) -> None:
        if self._laser_thread is not None:
            return
        self._laser_worker = LaserPollWorker(self._laser, self._laser_queue, interval_ms)
        self._laser_thread = QThread(self)
        self._laser_worker.moveToThread(self._laser_thread)
        self._laser_worker.state_updated.connect(self.laser_state_changed.emit)
        self._laser_worker.log_requested.connect(self.log_requested.emit)
        self._laser_worker.error_occurred.connect(self.error_occurred.emit)
        self._laser_thread.started.connect(self._laser_worker.run)
        self._laser_thread.finished.connect(self._laser_worker.deleteLater)
        self._laser_thread.start()

    def _stop_laser_poll(self) -> None:
        if self._laser_worker is not None:
            self._laser_worker.stop()
        if self._laser_thread is not None:
            self._laser_thread.quit()
            if not self._laser_thread.wait(2000):
                self._laser_thread.terminate()
                self._laser_thread.wait(1000)
            self._laser_thread = None
        self._laser_worker = None

    def start_lockin_acquire(self, recorder: Optional[ODMRRecorder] = None) -> None:
        """启动 RALL? 高速采集（仅在扫频期间调用）。"""
        if self._lockin_acquire_thread is not None:
            return
        self._recorder = recorder
        self._lockin_acquire_worker = LockinAcquireWorker(
            self._lockin, self._lockin_queue, recorder
        )
        self._lockin_acquire_thread = QThread(self)
        self._lockin_acquire_worker.moveToThread(self._lockin_acquire_thread)
        self._lockin_acquire_worker.batch_ready.connect(self.lockin_batch_ready.emit)
        self._lockin_acquire_worker.log_requested.connect(self.log_requested.emit)
        self._lockin_acquire_worker.error_occurred.connect(self.error_occurred.emit)
        self._lockin_acquire_worker.finished.connect(self._on_acquire_finished)
        self._lockin_acquire_thread.started.connect(self._lockin_acquire_worker.run)
        self._lockin_acquire_thread.finished.connect(self._lockin_acquire_worker.deleteLater)
        self._lockin_acquire_thread.start()
        self._acquiring = True

    def stop_lockin_acquire(self) -> None:
        """停止 RALL? 采集。"""
        self._stop_lockin_acquire()
        if self._recorder is not None and self._recorder.is_recording:
            self._recorder.stop_recording()

    def _stop_lockin_acquire(self) -> None:
        if self._lockin_acquire_worker is not None:
            self._lockin_acquire_worker.stop()
        if self._lockin_acquire_thread is not None:
            self._lockin_acquire_thread.quit()
            if not self._lockin_acquire_thread.wait(3000):
                self._lockin_acquire_thread.terminate()
                self._lockin_acquire_thread.wait(1000)
            self._lockin_acquire_thread = None
        self._lockin_acquire_worker = None
        self._acquiring = False

    # -- slots ---------------------------------------------------------------

    def _on_smb_state_updated(self, freq_hz: float, output_on: bool, mode: str) -> None:
        self.smb_state_changed.emit(freq_hz, output_on, mode)
        if self._lockin_acquire_worker is not None:
            power = self._smb.cached_power_dbm
            self._lockin_acquire_worker.set_smb_state(freq_hz, power, output_on)

    def _on_acquire_finished(self) -> None:
        self._acquiring = False
        self._lockin_acquire_thread = None
        self._lockin_acquire_worker = None

    # -- safety operations ---------------------------------------------------

    def emergency_stop(self) -> None:
        """急停：关闭所有输出，停止扫频和采集。"""
        self._sweeping = False
        self._stop_lockin_acquire()
        if self._smb.is_connected:
            try:
                self._smb.emergency_stop()
            except Exception as exc:
                self.error_occurred.emit(f"[急停] SMB100A 失败: {exc}")
        if self._laser.is_connected:
            if not self._laser.emergency_stop():
                self.error_occurred.emit("[急停] Laser 关闭失败（3 次重试均失败）")
        self.log_requested.emit("[急停] 已执行 Emergency Stop")

    def verify_emergency_stop(self) -> Dict[str, object]:
        """验证急停效果。"""
        result: Dict[str, object] = {}
        if self._smb.is_connected:
            try:
                outp = self._smb.get_output()
                mode = self._smb.get_freq_mode()
                ok = (not outp) and (mode == "CW")
                result["smb"] = {"output_on": outp, "mode": mode, "ok": ok}
            except Exception as exc:
                result["smb"] = {"ok": False, "error": str(exc)}
        if self._laser.is_connected:
            try:
                laser_out = self._laser.get_output()
                result["laser"] = {"output_on": laser_out, "ok": not laser_out}
            except Exception as exc:
                result["laser"] = {"ok": False, "error": str(exc)}
        result["acquiring"] = self._acquiring
        result["sweeping"] = self._sweeping
        return result

    # -- convenience pass-throughs -------------------------------------------

    def set_smb_raw_log_callback(self, cb: Callable[[str, bytes], None]) -> None:
        self._smb.set_raw_log_callback(cb)

    def set_lockin_raw_log_callback(self, cb: Callable[[str, bytes], None]) -> None:
        self._lockin.set_raw_log_callback(cb)

    def set_lockin_monitor_interval(self, ms: int) -> None:
        if self._lockin_monitor_worker is not None:
            self._lockin_monitor_worker.set_interval(ms)

    def is_any_worker_running(self) -> bool:
        return (
            (self._smb_thread is not None and self._smb_thread.isRunning()) or
            (self._lockin_monitor_thread is not None and self._lockin_monitor_thread.isRunning()) or
            (self._lockin_acquire_thread is not None and self._lockin_acquire_thread.isRunning()) or
            (self._laser_thread is not None and self._laser_thread.isRunning())
        )
