from __future__ import annotations

import queue
import time

from PyQt5.QtCore import QObject, pyqtSignal

from instruments.magnet_coil import MagnetCoilController


class AxisPollWorker(QObject):
    """Background worker that periodically polls MEAS:CURR? and processes set-commands."""

    current_read = pyqtSignal(str, float)   # (axis_name, current_mA)
    log_requested = pyqtSignal(str)
    error_occurred = pyqtSignal(str)
    finished = pyqtSignal(str)              # axis_name

    def __init__(
        self,
        controller: MagnetCoilController,
        command_queue: queue.Queue,
        interval_ms: int = 500,
    ) -> None:
        super().__init__()
        self._controller = controller
        self._queue = command_queue
        self._interval_s = interval_ms / 1000.0
        self._interval_ms = interval_ms
        self._stop_requested = False
        self._poll_count = 0

    @property
    def interval_ms(self) -> int:
        return self._interval_ms

    def run(self) -> None:
        axis = self._controller.axis
        self.log_requested.emit(f"[{axis}] 轮询线程启动")
        try:
            while not self._stop_requested:
                # process pending set-commands
                self._drain_commands()
                # poll current
                try:
                    ma = self._controller.get_current()
                    self._controller.update_cached_current(ma)
                    self.current_read.emit(axis, ma)
                    self._poll_count += 1
                    if self._poll_count % 20 == 0:
                        self.log_requested.emit(f"[{axis}] 已轮询 {self._poll_count} 次")
                except Exception as exc:
                    self.error_occurred.emit(f"[{axis}] 读取电流失败: {exc}")
                time.sleep(self._interval_s)
        except Exception as exc:
            self.error_occurred.emit(f"[{axis}] 轮询线程异常退出: {exc}")
        finally:
            self.log_requested.emit(f"[{axis}] 轮询线程结束")
            self.finished.emit(axis)

    def stop(self) -> None:
        self._stop_requested = True

    def _drain_commands(self) -> None:
        while True:
            try:
                cmd, args = self._queue.get_nowait()
            except queue.Empty:
                break
            try:
                method = getattr(self._controller, cmd)
                method(*args)
                self.log_requested.emit(
                    f"[{self._controller.axis}] 执行命令: {cmd}({', '.join(str(a) for a in args)})"
                )
            except Exception as exc:
                self.error_occurred.emit(
                    f"[{self._controller.axis}] 命令 {cmd} 失败: {exc}"
                )
