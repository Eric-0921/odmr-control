"""OE1022D 实时监控 Worker (SNAPD?)

以用户设定间隔发送 SNAPD? 命令，获取 X/Y/R/theta，
用于实时显示面板。数据量低，不存储。
"""

from __future__ import annotations

import queue
import time

from PyQt5.QtCore import QObject, pyqtSignal

from instruments.oe1022d import OE1022DDriver


class LockinMonitorWorker(QObject):
    """Background worker for OE1022D real-time monitoring via SNAPD?.

    支持双通道交替查询，数据字典包含 channel 字段。
    同时查询过载和 PLL 状态用于指示灯。
    """

    data_ready = pyqtSignal(dict)       # {channel: int, X, Y, R, theta}
    status_ready = pyqtSignal(dict)     # {channel: int, input_overload, gain_overload, pll_locked}
    log_requested = pyqtSignal(str)
    error_occurred = pyqtSignal(str)
    finished = pyqtSignal()

    def __init__(
        self,
        driver: OE1022DDriver,
        command_queue: queue.Queue,
        interval_ms: int = 94,
    ) -> None:
        super().__init__()
        self._driver = driver
        self._queue = command_queue
        self._interval_ms = max(50, interval_ms)  # 最低 50ms，避免洪水
        self._stop_requested = False
        self._query_count = 0
        self._current_channel = 1  # 交替通道：1 -> 2 -> 1 -> 2

    def run(self) -> None:
        self.log_requested.emit("[Lockin] 监控线程启动 (SNAPD?)")
        try:
            while not self._stop_requested:
                self._drain_commands()
                try:
                    if self._driver.is_connected:
                        ch = self._current_channel
                        # SNAPD? 查询
                        data = self._driver.snapd(ch, 0, 1, 2, 3)
                        data["channel"] = ch
                        self.data_ready.emit(data)

                        # 状态查询（每 10 轮查一次，避免过度查询）
                        if self._query_count % 10 == 0:
                            status = {
                                "channel": ch,
                                "input_overload": self._driver.get_input_overload(ch),
                                "gain_overload": self._driver.get_gain_overload(ch),
                                "pll_locked": self._driver.get_pll_locked(ch),
                            }
                            self.status_ready.emit(status)

                        # 切换通道
                        self._current_channel = 2 if ch == 1 else 1

                        self._query_count += 1
                        if self._query_count % 100 == 0:
                            self.log_requested.emit(
                                f"[Lockin] 已查询 {self._query_count} 次 SNAPD?"
                            )
                except Exception as exc:
                    self.error_occurred.emit(f"[Lockin] SNAPD? 失败: {exc}")
                time.sleep(self._interval_ms / 1000.0)
        except Exception as exc:
            self.error_occurred.emit(f"[Lockin] 监控线程异常: {exc}")
        finally:
            self.log_requested.emit("[Lockin] 监控线程结束")
            self.finished.emit()

    def stop(self) -> None:
        self._stop_requested = True

    def set_interval(self, ms: int) -> None:
        self._interval_ms = max(50, ms)

    def _drain_commands(self) -> None:
        while True:
            try:
                cmd, args = self._queue.get_nowait()
            except queue.Empty:
                break
            try:
                method = getattr(self._driver, cmd)
                method(*args)
                self.log_requested.emit(
                    f"[Lockin] 执行: {cmd}({', '.join(str(a) for a in args)})"
                )
            except Exception as exc:
                self.error_occurred.emit(f"[Lockin] 命令 {cmd} 失败: {exc}")
