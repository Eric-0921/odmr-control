from __future__ import annotations

from PyQt5.QtCore import QObject, pyqtSignal

from instruments.oe1022d import LockinSample, OE1022DController


class LockinWorker(QObject):
    data_received = pyqtSignal(object, int)
    log_requested = pyqtSignal(str)
    error_occurred = pyqtSignal(str)
    finished = pyqtSignal(int)

    def __init__(self, controller: OE1022DController, command: str, interval_ms: int) -> None:
        super().__init__()
        self.controller = controller
        self.command = command
        self.interval_ms = max(interval_ms, 10)
        self._stop_requested = False

    def stop(self) -> None:
        self._stop_requested = True

    def run(self) -> None:
        count = 0
        wait_s = self.interval_ms / 1000.0
        while not self._stop_requested:
            try:
                if not self.controller.is_connected:
                    break
                count += 1
                samples = self.controller.query_snapshot(self.command, wait_s=wait_s)
                for sample in samples:
                    self.data_received.emit(sample, count)
                if count % 10 == 0 and samples:
                    self.log_requested.emit(_format_periodic_log(samples[-1], count))
            except Exception as exc:
                self.error_occurred.emit(f"查询错误: {exc}")
        self.finished.emit(count)


def _format_periodic_log(sample: LockinSample, count: int) -> str:
    return (
        f"包{count}: X={sample.x_mv:.4f}, Y={sample.y_mv:.4f}, "
        f"R={sample.r_mv:.4f}, θ={sample.theta_deg:.4f}"
    )

