from __future__ import annotations

import csv
import datetime
from pathlib import Path
from typing import Dict, Optional


class MagnetFieldRecorder:
    """Append-only CSV recorder for magnetic field data."""

    FIELDNAMES = [
        "Timestamp",
        "X_target_field_nT",
        "X_total_current_mA",
        "X_estimated_recur_current_mA",
        "X_estimated_field_nT",
        "Y_target_field_nT",
        "Y_total_current_mA",
        "Y_estimated_recur_current_mA",
        "Y_estimated_field_nT",
        "Z_target_field_nT",
        "Z_total_current_mA",
        "Z_estimated_recur_current_mA",
        "Z_estimated_field_nT",
    ]
    HEADER = ",".join(FIELDNAMES)

    def __init__(self) -> None:
        self._file = None
        self._writer: Optional[csv.DictWriter] = None

    def init_file(self, filename: str) -> None:
        path = Path(filename)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._file = open(path, "w", newline="", encoding="utf-8")
        self._writer = csv.DictWriter(self._file, fieldnames=self.FIELDNAMES)
        self._writer.writeheader()

    def append_row(self, row: Dict[str, Optional[float]]) -> None:
        if self._file is None or self._writer is None:
            return
        ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        output: Dict[str, str] = {"Timestamp": ts}
        for field in self.FIELDNAMES:
            if field == "Timestamp":
                continue
            output[field] = self._format_value(row.get(field))
        self._writer.writerow(output)
        self._file.flush()

    @staticmethod
    def _format_value(value: Optional[float]) -> str:
        if value is None:
            return ""
        return f"{value:.5f}"

    def close(self) -> None:
        if self._file is not None:
            try:
                self._file.close()
            except Exception:
                pass
            self._file = None
            self._writer = None

    @staticmethod
    def timestamp_filename() -> str:
        return datetime.datetime.now().strftime("%Y%m%d_%H%M%S") + ".csv"
