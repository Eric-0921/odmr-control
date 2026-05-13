"""OE1022D 高速采集 Worker (RALL?)

在扫频期间以 50ms 间隔发送 RALL? 并读取 12288 bytes，
解析后通过信号批量回传，同时写入 Parquet。

严格对齐 50ms 时序，允许 ±5ms 抖动。
"""

from __future__ import annotations

import queue
import time

import numpy as np
from PyQt5.QtCore import QObject, pyqtSignal

from data.recorder import ODMRRecorder
from instruments.oe1022d import OE1022DDriver, RALL_TOTAL_BYTES, SAMPLES_PER_BATCH


class LockinAcquireWorker(QObject):
    """Background worker for OE1022D high-speed acquisition via RALL?."""

    batch_ready = pyqtSignal(dict)  # parsed RALL? data dict
    log_requested = pyqtSignal(str)
    error_occurred = pyqtSignal(str)
    finished = pyqtSignal()

    def __init__(
        self,
        driver: OE1022DDriver,
        command_queue: queue.Queue,
        recorder: ODMRRecorder | None = None,
    ) -> None:
        super().__init__()
        self._driver = driver
        self._queue = command_queue
        self._recorder = recorder
        self._stop_requested = False
        self._batch_count = 0
        self._dropped = 0
        self._smb_freq_hz = 0.0
        self._smb_power_dbm = 0.0
        self._smb_rf_on = False

    def set_smb_state(self, freq_hz: float, power_dbm: float, rf_on: bool) -> None:
        """同步 SMB100A 当前状态，用于写入 Parquet。"""
        self._smb_freq_hz = freq_hz
        self._smb_power_dbm = power_dbm
        self._smb_rf_on = rf_on

    def run(self) -> None:
        self.log_requested.emit("[Lockin] 采集线程启动 (RALL?)")
        if not self._driver.is_connected:
            self.error_occurred.emit("[Lockin] 设备未连接，采集线程退出")
            self.finished.emit()
            return

        # 发送第一个 RALL? 启动流
        try:
            self._driver.start_rall_stream()
        except Exception as exc:
            self.error_occurred.emit(f"[Lockin] RALL? 启动失败: {exc}")
            self.finished.emit()
            return

        interval_s = 0.050  # 50 ms 标准间隔
        try:
            while not self._stop_requested:
                t_start = time.monotonic()

                self._drain_commands()

                try:
                    raw = self._driver.read_rall_batch(timeout=1.0)
                    if len(raw) != RALL_TOTAL_BYTES:
                        self._dropped += 1
                        self.error_occurred.emit(
                            f"[Lockin] RALL? 数据不完整: {len(raw)}/{RALL_TOTAL_BYTES} bytes"
                        )
                        continue

                    batch = self._driver.parse_rall(raw)
                    self.batch_ready.emit(batch)
                    self._batch_count += 1

                    if self._recorder is not None and self._recorder.is_recording:
                        self._recorder.write_batch(
                            batch,
                            smb_freq_hz=self._smb_freq_hz,
                            smb_power_dbm=self._smb_power_dbm,
                            smb_rf_on=self._smb_rf_on,
                        )

                    if self._batch_count % 200 == 0:
                        self.log_requested.emit(
                            f"[Lockin] 已采集 {self._batch_count} 批, 丢 {self._dropped} 批"
                        )

                except Exception as exc:
                    self.error_occurred.emit(f"[Lockin] RALL? 读取失败: {exc}")
                    self._dropped += 1

                # 严格 50ms 对齐
                elapsed = time.monotonic() - t_start
                sleep_time = interval_s - elapsed
                if sleep_time > 0.005:  # 只睡有意义的时间
                    time.sleep(sleep_time)

        except Exception as exc:
            self.error_occurred.emit(f"[Lockin] 采集线程异常: {exc}")
        finally:
            # 确保 recorder 被关闭，防止 Parquet 文件损坏
            if self._recorder is not None and self._recorder.is_recording:
                try:
                    self._recorder.stop_recording()
                    self.log_requested.emit("[Lockin] Recorder stopped in finally")
                except Exception as exc:
                    self.error_occurred.emit(f"[Lockin] Recorder stop failed: {exc}")
            self.log_requested.emit(
                f"[Lockin] 采集线程结束，共 {self._batch_count} 批，丢弃 {self._dropped} 批"
            )
            self.finished.emit()

    def stop(self) -> None:
        self._stop_requested = True

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
