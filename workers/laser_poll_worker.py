"""激光器状态轮询 Worker

由于 MSL-U 激光器协议无查询命令，
Worker 仅从 driver 缓存读取状态并广播，不执行设备 I/O。
"""

from __future__ import annotations

import queue
import time

from PyQt5.QtCore import QObject, pyqtSignal

from instruments.laser_msl import LaserMSLDriver


class LaserPollWorker(QObject):
    """Background worker for laser state broadcasting."""

    state_updated = pyqtSignal(dict)  # {power_mw, output_on, wavelength_nm, max_power_mw}
    log_requested = pyqtSignal(str)
    error_occurred = pyqtSignal(str)
    finished = pyqtSignal()

    def __init__(
        self,
        driver: LaserMSLDriver,
        command_queue: queue.Queue,
        interval_ms: int = 1000,
    ) -> None:
        super().__init__()
        self._driver = driver
        self._queue = command_queue
        self._interval_s = interval_ms / 1000.0
        self._stop_requested = False

    def run(self) -> None:
        self.log_requested.emit("[Laser] 轮询线程启动")
        try:
            while not self._stop_requested:
                self._drain_commands()
                try:
                    if self._driver.is_connected:
                        state = {
                            "power_mw": self._driver.get_power(),
                            "output_on": self._driver.get_output(),
                            "wavelength_nm": self._driver.wavelength_nm,
                            "max_power_mw": self._driver.max_power_mw,
                        }
                        self.state_updated.emit(state)
                except Exception as exc:
                    self.error_occurred.emit(f"[Laser] 状态读取失败: {exc}")
                time.sleep(self._interval_s)
        except Exception as exc:
            self.error_occurred.emit(f"[Laser] 轮询线程异常: {exc}")
        finally:
            self.log_requested.emit("[Laser] 轮询线程结束")
            self.finished.emit()

    def stop(self) -> None:
        self._stop_requested = True

    def _drain_commands(self) -> None:
        """处理命令队列中的待执行命令。"""
        while True:
            try:
                cmd, args = self._queue.get_nowait()
            except queue.Empty:
                break
            try:
                method = getattr(self._driver, cmd)
                method(*args)
                self.log_requested.emit(
                    f"[Laser] 执行: {cmd}({', '.join(str(a) for a in args)})"
                )
            except Exception as exc:
                self.error_occurred.emit(f"[Laser] 命令 {cmd} 失败: {exc}")
