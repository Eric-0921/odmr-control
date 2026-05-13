"""SMB100A 状态轮询 Worker

在独立 QThread 中定期查询频率和输出状态，
通过 pyqtSignal 回传 GUI。
"""

from __future__ import annotations

import queue
import time

from PyQt5.QtCore import QObject, pyqtSignal

from instruments.smb100a import SMB100ADriver


class SMBPollWorker(QObject):
    """Background worker for SMB100A state polling.

    新增完整状态字典广播，供指示灯和扩展参数显示使用。
    保留旧版 (freq_hz, output_on, mode) 信号以兼容现有 GUI。
    """

    state_updated = pyqtSignal(float, bool, str)  # (freq_hz, output_on, mode) — 兼容旧版
    state_dict_updated = pyqtSignal(dict)           # 完整状态字典 — 新版
    log_requested = pyqtSignal(str)
    error_occurred = pyqtSignal(str)
    finished = pyqtSignal()

    def __init__(
        self,
        driver: SMB100ADriver,
        command_queue: queue.Queue,
        interval_ms: int = 100,
    ) -> None:
        super().__init__()
        self._driver = driver
        self._queue = command_queue
        self._interval_s = interval_ms / 1000.0
        self._stop_requested = False
        self._poll_count = 0

    def run(self) -> None:
        self.log_requested.emit("[SMB100A] 轮询线程启动")
        try:
            while not self._stop_requested:
                self._drain_commands()
                try:
                    if self._driver.is_connected:
                        freq = self._driver.get_freq_cw()
                        outp = self._driver.get_output()
                        mode = self._driver.get_freq_mode()
                        self.state_updated.emit(freq, outp, mode)

                        # 扩展状态查询（每轮都查，开销极小）
                        power = self._driver.get_power()
                        lf_on = self._driver.get_lf_output()
                        lf_freq = self._driver.get_lf_freq()
                        mod_on = self._driver.get_modulation_state()
                        lf_sweep = self._driver.get_lf_mode() == "SWE"

                        state = {
                            "freq_hz": freq,
                            "output_on": outp,
                            "mode": mode,
                            "power_dbm": power,
                            "lf_on": lf_on,
                            "lf_freq_hz": lf_freq,
                            "mod_on": mod_on,
                            "lf_sweep": lf_sweep,
                        }
                        self.state_dict_updated.emit(state)

                        self._poll_count += 1
                        if self._poll_count % 50 == 0:
                            self.log_requested.emit(f"[SMB100A] 已轮询 {self._poll_count} 次")
                except Exception as exc:
                    self.error_occurred.emit(f"[SMB100A] 读取失败: {exc}")
                time.sleep(self._interval_s)
        except Exception as exc:
            self.error_occurred.emit(f"[SMB100A] 轮询线程异常: {exc}")
        finally:
            self.log_requested.emit("[SMB100A] 轮询线程结束")
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
                    f"[SMB100A] 执行: {cmd}({', '.join(str(a) for a in args)})"
                )
            except Exception as exc:
                self.error_occurred.emit(f"[SMB100A] 命令 {cmd} 失败: {exc}")
