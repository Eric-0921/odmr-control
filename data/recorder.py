from __future__ import annotations

import datetime as _dt
from pathlib import Path


class DataRecorder:
    def __init__(self) -> None:
        self.sweep_buffer: list[tuple[str, float]] = []

    @staticmethod
    def timestamp() -> str:
        return _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]

    def init_file(self, filename: str) -> None:
        Path(filename).write_text("Timestamp,X\n", encoding="utf-8")

    def append_x(self, filename: str, x_mv: float) -> None:
        with open(filename, "a", encoding="utf-8") as handle:
            handle.write(f"{self.timestamp()},{x_mv}\n")

    def buffer_x(self, x_mv: float) -> None:
        self.sweep_buffer.append((self.timestamp(), x_mv))

    def flush_sweep(self, filename: str) -> int:
        count = len(self.sweep_buffer)
        with open(filename, "w", encoding="utf-8") as handle:
            handle.write("Timestamp,X\n")
            for timestamp, x_mv in self.sweep_buffer:
                handle.write(f"{timestamp},{x_mv}\n")
        self.sweep_buffer = []
        return count

