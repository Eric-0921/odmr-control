"""OE1022D 高速采集 Worker (RALL?)

在扫频期间以 50ms 间隔发送 RALL? 并读取 12288 bytes，
解析后通过信号批量回传，同时写入 CSV。

严格对齐 50ms 时序，允许 ±5ms 抖动。
"""

from __future__ import annotations

import queue
import threading
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
        self._last_batch_time_s = 0.0
        self._state_lock = threading.Lock()
        self._smb_freq_hz = 0.0
        self._smb_power_dbm = 0.0
        self._smb_rf_on = False
        self._laser_power_mw = 0.0
        self._laser_on = False
        self._mag_state = {}

    def set_smb_state(self, freq_hz: float, power_dbm: float, rf_on: bool) -> None:
        """同步 SMB100A 当前状态，用于写入 CSV。"""
        with self._state_lock:
            self._smb_freq_hz = freq_hz
            self._smb_power_dbm = power_dbm
            self._smb_rf_on = rf_on

    def set_laser_state(self, power_mw: float, output_on: bool) -> None:
        """同步激光器缓存状态，用于写入 CSV。"""
        with self._state_lock:
            self._laser_power_mw = power_mw
            self._laser_on = output_on

    def set_mag_state(self, state: dict) -> None:
        """同步三轴磁场缓存状态，用于写入 CSV。"""
        with self._state_lock:
            self._mag_state = dict(state)

    def set_recorder(self, recorder: ODMRRecorder | None) -> None:
        """采集不中断时切换/附加 recorder。"""
        with self._state_lock:
            self._recorder = recorder

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
        consecutive_errors = 0
        MAX_CONSECUTIVE_ERRORS = 5
        try:
            while not self._stop_requested:
                t_start = time.monotonic()

                # 在发送 RALL? 之前快照设备状态（采集端时间基准）
                with self._state_lock:
                    state_snapshot = {
                        "smb_freq_hz": self._smb_freq_hz,
                        "smb_power_dbm": self._smb_power_dbm,
                        "smb_rf_on": self._smb_rf_on,
                        "laser_power_mw": self._laser_power_mw,
                        "laser_on": self._laser_on,
                        "mag_state": dict(self._mag_state),
                    }

                self._drain_commands()

                try:
                    # 每批重新发送 RALL? 命令（查询式：设备不会自动持续发送）
                    t_rall_start = time.monotonic()
                    self._driver.start_rall_stream()
                    raw = self._driver.read_rall_batch(timeout=1.0)
                    t_rall_elapsed = (time.monotonic() - t_rall_start) * 1000.0
                    if self._batch_count % 20 == 0:
                        self.log_requested.emit(
                            f"[Lockin] RALL? 读取耗时: {t_rall_elapsed:.1f} ms, 数据: {len(raw)} bytes"
                        )
                    if len(raw) != RALL_TOTAL_BYTES:
                        self._dropped += 1
                        consecutive_errors += 1
                        self.error_occurred.emit(
                            f"[Lockin] RALL? 数据不完整: {len(raw)}/{RALL_TOTAL_BYTES} bytes"
                        )
                        if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                            self.error_occurred.emit(
                                f"[Lockin] 连续 {consecutive_errors} 次失败，停止采集"
                            )
                            break
                        continue

                    consecutive_errors = 0
                    batch = self._driver.parse_rall(raw)
                    config_snapshot = self._driver.parse_rall_config(raw)
                    batch["config_snapshot"] = config_snapshot
                    batch["acquisition_stats"] = {
                        "batch_count": self._batch_count + 1,
                        "batch_timestamp_s": t_start,  # 采集端统一时间基准
                        "dropped_batches": self._dropped,
                        "batch_rate_hz": self._batch_rate(t_start),
                    }
                    batch["state_snapshot"] = state_snapshot
                    self.batch_ready.emit(batch)
                    self._batch_count += 1

                    with self._state_lock:
                        recorder = self._recorder

                    if recorder is not None and recorder.is_recording:
                        recorder.write_batch(
                            batch,
                            batch_timestamp_s=t_start,
                            smb_freq_hz=state_snapshot["smb_freq_hz"],
                            smb_power_dbm=state_snapshot["smb_power_dbm"],
                            smb_rf_on=state_snapshot["smb_rf_on"],
                            laser_power_mw=state_snapshot["laser_power_mw"],
                            laser_on=state_snapshot["laser_on"],
                            mag_state=state_snapshot["mag_state"],
                        )

                    if self._batch_count % 200 == 0:
                        self.log_requested.emit(
                            f"[Lockin] 已采集 {self._batch_count} 批, 丢 {self._dropped} 批"
                        )

                except Exception as exc:
                    self._dropped += 1
                    consecutive_errors += 1
                    # 检测端口断开
                    if not self._driver.is_connected:
                        self.error_occurred.emit("[Lockin] 设备连接断开，停止采集")
                        break
                    self.error_occurred.emit(f"[Lockin] RALL? 读取失败: {exc}")
                    if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                        self.error_occurred.emit(
                            f"[Lockin] 连续 {consecutive_errors} 次失败，停止采集"
                        )
                        break

                # 严格 50ms 对齐
                elapsed = time.monotonic() - t_start
                sleep_time = interval_s - elapsed
                if sleep_time > 0.005:  # 只睡有意义的时间
                    time.sleep(sleep_time)

        except Exception as exc:
            self.error_occurred.emit(f"[Lockin] 采集线程异常: {exc}")
        finally:
            # 确保 recorder 被关闭，防止 CSV 文件句柄泄漏
            with self._state_lock:
                recorder = self._recorder
            if recorder is not None and recorder.is_recording:
                try:
                    recorder.stop_recording()
                    self.log_requested.emit("[Lockin] Recorder stopped in finally")
                except Exception as exc:
                    self.error_occurred.emit(f"[Lockin] Recorder stop failed: {exc}")
            self.log_requested.emit(
                f"[Lockin] 采集线程结束，共 {self._batch_count} 批，丢弃 {self._dropped} 批"
            )
            self.finished.emit()

    def stop(self) -> None:
        self._stop_requested = True

    def _batch_rate(self, now_s: float) -> float:
        if self._last_batch_time_s <= 0:
            self._last_batch_time_s = now_s
            return 0.0
        dt = now_s - self._last_batch_time_s
        self._last_batch_time_s = now_s
        if dt <= 0:
            return 0.0
        return 1.0 / dt

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
