"""扫频序列引擎

管理扫频流程：
- 单次扫频 / 循环扫频
- 自动停止检测（频率从 stop 跳回 start）
- 与 RALL? 采集同步启动/停止
- 序列步进（多步扫频实验）

SweepEngine 本身不 moveToThread，而是创建一个无 parent 的 _SweepRunner
对象并 moveToThread，避免 Qt 禁止 parent 对象跨线程的问题。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import List, Optional

from PyQt5.QtCore import QObject, QThread, pyqtSignal

from core.instrument_controller import InstrumentController
from data.recorder import ODMRRecorder


@dataclass
class SweepStep:
    """单步扫频参数。"""
    name: str = ""
    start_freq_hz: float = 2.82e9
    stop_freq_hz: float = 2.92e9
    step_hz: float = 500e3
    dwell_ms: float = 500.0
    power_dbm: float = -30.0
    lf_freq_hz: float = 500.0
    lf_amp_mv: float = 137.0
    lf_shape: str = "SQUARE"
    fm_dev_hz: float = 4e6
    cycles: int = 1
    cycle_interval_ms: int = 200


@dataclass
class SweepSequence:
    """扫频序列。"""
    name: str = "default"
    steps: List[SweepStep] = field(default_factory=list)
    loop_count: int = 1
    loop_delay_s: float = 0.0
    return_to_zero: bool = True


class _SweepRunner(QObject):
    """实际执行扫频逻辑的对象，无 parent，可安全 moveToThread。"""

    step_started = pyqtSignal(str, int)
    step_completed = pyqtSignal(str, int)
    cycle_started = pyqtSignal(int, int)
    cycle_completed = pyqtSignal(int, int)
    progress = pyqtSignal(int, int)
    sweep_finished = pyqtSignal(bool)
    error_occurred = pyqtSignal(str)
    log_requested = pyqtSignal(str)

    def __init__(self, ctrl: InstrumentController, sequence: SweepSequence,
                 recorder: Optional[ODMRRecorder] = None) -> None:
        super().__init__(None)  # 无 parent！
        self._ctrl = ctrl
        self._sequence = sequence
        self._recorder = recorder
        self._stop_requested = False
        self._pause_requested = False

    def run(self) -> None:
        if self._sequence is None:
            self.error_occurred.emit("未配置扫频序列")
            self.sweep_finished.emit(False)
            return

        seq = self._sequence
        total_steps = len(seq.steps)
        completed = True

        # 扫频期间暂停 SMB 轮询，避免并发访问
        self._ctrl._stop_smb_poll()

        try:
            for loop in range(seq.loop_count):
                if self._stop_requested:
                    completed = False
                    break

                for step_idx, step in enumerate(seq.steps):
                    if self._stop_requested:
                        completed = False
                        break

                    self.progress.emit(step_idx, total_steps)
                    self.step_started.emit(step.name, step_idx)

                    self._execute_step(step)
                    self.step_completed.emit(step.name, step_idx)

                    if seq.loop_delay_s > 0 and not self._stop_requested:
                        time.sleep(seq.loop_delay_s)

            if seq.return_to_zero and not self._stop_requested:
                self._ctrl.smb.set_freq_mode("CW")
                self._ctrl.smb.set_output(False)
                self.log_requested.emit("[SweepEngine] 返回零态: CW, OUTP OFF")

        except Exception as exc:
            self.error_occurred.emit("[SweepEngine] 序列异常: " + str(exc))
            completed = False
        finally:
            self.sweep_finished.emit(completed)

    def _execute_step(self, step: SweepStep) -> None:
        smb = self._ctrl.smb

        # 1. 配置参数
        smb.configure_sweep(
            step.start_freq_hz,
            step.stop_freq_hz,
            step.step_hz,
            step.dwell_ms,
            step.power_dbm,
        )
        smb.set_lf_freq(step.lf_freq_hz)
        smb.set_lf_voltage(step.lf_amp_mv)
        smb.set_lf_shape(step.lf_shape)
        smb.set_fm_deviation(step.fm_dev_hz)

        self.log_requested.emit(
            "[SweepEngine] 配置扫频: %.5f~%.5f GHz, 功率 %.1f dBm"
            % (step.start_freq_hz / 1e9, step.stop_freq_hz / 1e9, step.power_dbm)
        )

        # 2. 启动 RALL? 采集（使用外部传入的 recorder）
        self._ctrl.start_lockin_acquire(self._recorder)

        # 3. 执行循环扫频
        total_cycles = max(1, step.cycles)
        for cycle in range(total_cycles):
            if self._stop_requested:
                break

            self.cycle_started.emit(cycle + 1, total_cycles)

            smb.start_sweep()
            self.log_requested.emit(
                "[SweepEngine] 扫频开始 (cycle %d/%d)" % (cycle + 1, total_cycles)
            )

            self._wait_sweep_complete(
                step.start_freq_hz,
                step.stop_freq_hz,
                step.step_hz,
                step.dwell_ms,
            )

            self.cycle_completed.emit(cycle + 1, total_cycles)

            if cycle < total_cycles - 1 and step.cycle_interval_ms > 0:
                time.sleep(step.cycle_interval_ms / 1000.0)

        # 4. 停止采集
        self._ctrl.stop_lockin_acquire()
        smb.stop_sweep()

    def _wait_sweep_complete(
        self,
        start_hz: float,
        stop_hz: float,
        step_hz: float,
        dwell_ms: float,
    ) -> None:
        """等待一个扫频周期完成。

        动态计算超时：点数 x 驻留 + 30s 裕量。
        检测逻辑：频率从接近 stop 跳回接近 start 时认为完成。
        """
        n_points = max(1, int(abs(stop_hz - start_hz) / max(abs(step_hz), 1)) + 1)
        timeout_s = n_points * (dwell_ms / 1000.0) + 30.0

        tolerance = max(abs(step_hz) / 2, 1e3)
        last_freq: Optional[float] = None
        t_start = time.monotonic()
        reached_stop = False

        while not self._stop_requested:
            if time.monotonic() - t_start > timeout_s:
                self.error_occurred.emit("[SweepEngine] 扫频超时")
                break

            if self._pause_requested:
                time.sleep(0.1)
                continue

            try:
                freq = self._ctrl.smb.get_freq_cw()
            except Exception:
                time.sleep(0.05)
                continue

            at_start = abs(freq - start_hz) <= tolerance
            at_stop = abs(freq - stop_hz) <= tolerance

            if at_stop:
                reached_stop = True

            if reached_stop and at_start and last_freq is not None:
                if abs(last_freq - stop_hz) <= tolerance:
                    self.log_requested.emit("[SweepEngine] 检测到扫频周期完成")
                    break

            last_freq = freq
            time.sleep(0.05)


class SweepEngine(QObject):
    """扫频引擎管理类（不跨线程，只管理 _SweepRunner 的生命周期）。"""

    step_started = pyqtSignal(str, int)
    step_completed = pyqtSignal(str, int)
    cycle_started = pyqtSignal(int, int)
    cycle_completed = pyqtSignal(int, int)
    progress = pyqtSignal(int, int)
    sweep_finished = pyqtSignal(bool)
    error_occurred = pyqtSignal(str)
    log_requested = pyqtSignal(str)

    def __init__(self, controller: InstrumentController, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._ctrl = controller
        self._sequence: Optional[SweepSequence] = None
        self._recorder: Optional[ODMRRecorder] = None
        self._thread: Optional[QThread] = None
        self._runner: Optional[_SweepRunner] = None
        self._pending_ok: bool = True

    def set_recorder(self, recorder: Optional[ODMRRecorder]) -> None:
        self._recorder = recorder

    def set_sequence(self, seq: SweepSequence) -> None:
        self._sequence = seq

    def load_sequence(self, steps: List[SweepStep], loop_count: int = 1) -> None:
        self._sequence = SweepSequence(
            name="manual",
            steps=steps,
            loop_count=loop_count,
        )

    def start(self) -> None:
        if self._thread is not None and self._thread.isRunning():
            self.error_occurred.emit("扫频引擎已在运行")
            return
        if not self._ctrl.is_smb_connected:
            self.error_occurred.emit("SMB100A 未连接，无法启动扫频")
            return

        self._thread = QThread()
        self._runner = _SweepRunner(self._ctrl, self._sequence, self._recorder)

        # 转发 runner 信号
        self._runner.step_started.connect(self.step_started.emit)
        self._runner.step_completed.connect(self.step_completed.emit)
        self._runner.cycle_started.connect(self.cycle_started.emit)
        self._runner.cycle_completed.connect(self.cycle_completed.emit)
        self._runner.progress.connect(self.progress.emit)
        self._runner.sweep_finished.connect(self._on_sweep_finished)
        self._runner.error_occurred.connect(self.error_occurred.emit)
        self._runner.log_requested.connect(self.log_requested.emit)

        self._runner.moveToThread(self._thread)
        self._thread.started.connect(self._runner.run)
        self._thread.finished.connect(self._runner.deleteLater)
        self._thread.finished.connect(self._thread.deleteLater)
        self._thread.start()

    def stop(self) -> None:
        if self._runner is not None:
            self._runner._stop_requested = True
        self.log_requested.emit("[SweepEngine] 收到停止请求")

    def pause(self) -> None:
        if self._runner is not None:
            self._runner._pause_requested = True

    def resume(self) -> None:
        if self._runner is not None:
            self._runner._pause_requested = False

    def _on_sweep_finished(self, ok: bool) -> None:
        """runner 的 run() 已返回（sweep_finished 信号触发），通知线程退出。"""
        self._pending_ok = ok

        # 断开旧 runner 信号，防止 deleteLater 后触发已销毁对象
        if self._runner is not None:
            try:
                self._runner.sweep_finished.disconnect(self._on_sweep_finished)
            except (TypeError, RuntimeError):
                pass

        # 通知线程退出，finished 信号触发 _cleanup_after_thread
        if self._thread is not None:
            self._thread.finished.connect(self._cleanup_after_thread)
            self._thread.quit()
        else:
            self._do_cleanup()

    def _cleanup_after_thread(self) -> None:
        """线程退出后执行清理（由 thread.finished 触发，非阻塞）。"""
        # 断开自身，防止 stop_and_wait 场景下重复触发
        if self._thread is not None:
            try:
                self._thread.finished.disconnect(self._cleanup_after_thread)
            except (TypeError, RuntimeError):
                pass
        self._do_cleanup()

    def _do_cleanup(self) -> None:
        """实际清理逻辑：停止采集、恢复轮询、释放引用。"""
        self._ctrl.stop_lockin_acquire()
        if self._ctrl.is_smb_connected:
            self._ctrl._start_smb_poll()

        self._thread = None
        self._runner = None
        self.sweep_finished.emit(self._pending_ok)

    def stop_and_wait(self, timeout_ms: int = 5000) -> None:
        """停止扫频并阻塞等待线程退出。仅限 closeEvent 等非 GUI 循环场景。"""
        if self._runner is not None:
            self._runner._stop_requested = True
        if self._thread is not None and self._thread.isRunning():
            self._thread.quit()
            self._thread.wait(timeout_ms)
        # 同步执行清理（thread.finished 可能已触发过 _do_cleanup，此处为兜底）
        self._ctrl.stop_lockin_acquire()
