"""ODMR CSV 数据记录模块.

输出格式对齐 OE1022D LabVIEW 导出的 tabular 数据:
- 第一行: 当前日期 / 当前时间 / 采样间隔
- 第二行: CH-A / CH-B / ADC / SMB / Laser / System 分组
- 第三行: 字段名
- 后续行: 每个 RALL? sample 一行
"""

from __future__ import annotations

import csv
import json
import math
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

import numpy as np

from instruments.oe1022d import SAMPLES_PER_BATCH


CH_FIELDS = [
    "X",
    "Y",
    "R",
    "theta",
    "Freq",
    "Noise",
    "Xh1",
    "Yh1",
    "Rh1",
    "theta_h1",
    "Xh2",
    "Yh2",
    "Rh2",
    "theta_h2",
]
ADC_FIELDS = ["ADC1", "ADC2", "ADC3", "ADC4"]
SMB_FIELDS = ["smb_freq_hz", "smb_power_dbm", "smb_rf_on"]
LASER_FIELDS = ["laser_power_mw", "laser_on"]
MAG_FIELDS = [
    "X_target_field_nT",
    "X_total_current_mA",
    "X_output_on",
    "X_lock_zero",
    "Y_target_field_nT",
    "Y_total_current_mA",
    "Y_output_on",
    "Y_lock_zero",
    "Z_target_field_nT",
    "Z_total_current_mA",
    "Z_output_on",
    "Z_lock_zero",
]
SYSTEM_FIELDS = ["sample_index", "time_s", "batch_index"]


class ODMRRecorder:
    """CSV 数据记录器，支持实时追加。线程安全。"""

    def __init__(self, output_dir: Path | str | None = None) -> None:
        self._output_dir: Optional[Path] = None
        self._csv_file = None
        self._writer: Optional[csv.writer] = None
        self._batch_count = 0
        self._total_points = 0
        self._start_time: Optional[float] = None
        self._start_datetime: Optional[datetime] = None
        self._is_recording = False
        self._lock = threading.Lock()

        if output_dir is not None:
            self.set_output_dir(output_dir)

    @property
    def is_recording(self) -> bool:
        return self._is_recording

    @property
    def output_dir(self) -> Optional[Path]:
        return self._output_dir

    @property
    def csv_path(self) -> Optional[Path]:
        if self._output_dir is None:
            return None
        return self._output_dir / "data.csv"

    def set_output_dir(self, path: Path | str) -> None:
        self._output_dir = Path(path)
        self._output_dir.mkdir(parents=True, exist_ok=True)

    def start_recording(self) -> None:
        """开始记录，创建 UTF-8 CSV。"""
        if self._output_dir is None:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            self.set_output_dir(Path(f"./experiments/experiment_{timestamp}"))
        if self._output_dir is None:
            raise RuntimeError("无法创建输出目录")

        self._batch_count = 0
        self._total_points = 0
        self._start_time = time.monotonic()
        self._start_datetime = datetime.now()
        self._csv_file = open(self._output_dir / "data.csv", "w", newline="", encoding="utf-8-sig")
        self._writer = csv.writer(self._csv_file)
        self._write_headers()
        self._is_recording = True
        self._save_columns_json()

    def stop_recording(self) -> None:
        """停止记录，关闭 CSV，生成 metadata.json。幂等：多次调用安全。"""
        with self._lock:
            if not self._is_recording:
                return
            self._is_recording = False
            if self._csv_file is not None:
                self._csv_file.flush()
                self._csv_file.close()
                self._csv_file = None
                self._writer = None
        if self._output_dir is not None:
            self._save_metadata_json()

    def write_batch(
        self,
        rall_data: Dict[str, np.ndarray],
        smb_freq_hz: float = 0.0,
        smb_power_dbm: float = 0.0,
        smb_rf_on: bool = False,
        laser_power_mw: float = 0.0,
        laser_on: bool = False,
        mag_state: Optional[Dict[str, Any]] = None,
    ) -> None:
        """写入一批 RALL? 数据（50 点）。线程安全。"""
        with self._lock:
            if not self._is_recording or self._writer is None:
                return
            if self._start_time is None:
                return

            n = self._batch_len(rall_data)
            t_batch = time.monotonic() - self._start_time
            for i in range(n):
                row = []
                row.extend(self._channel_values(rall_data, "A", i))
                row.extend(self._channel_values(rall_data, "B", i))
                row.extend(self._adc_values(rall_data, i))
                row.extend([
                    self._fmt(smb_freq_hz),
                    self._fmt(smb_power_dbm),
                    "1" if smb_rf_on else "0",
                    self._fmt(laser_power_mw),
                    "1" if laser_on else "0",
                    *self._mag_values(mag_state),
                    str(self._total_points + i),
                    self._fmt(t_batch + i * 0.001),
                    str(self._batch_count),
                ])
                self._writer.writerow(row)

            if self._csv_file is not None:
                self._csv_file.flush()
            self._batch_count += 1
            self._total_points += n

    def _write_headers(self) -> None:
        if self._writer is None or self._start_datetime is None:
            return
        date_text = f"{self._start_datetime.year}/{self._start_datetime.month}/{self._start_datetime.day}"
        time_text = self._start_datetime.strftime("%H:%M:%S")
        self._writer.writerow([
            "当前日期:", date_text, "", "当前时间:", time_text, "", "采样间隔:", "0.001 s"
        ])
        self._writer.writerow(
            ["CH-A"] + [""] * (len(CH_FIELDS) - 1) +
            ["CH-B"] + [""] * (len(CH_FIELDS) - 1) +
            ["ADC"] + [""] * (len(ADC_FIELDS) - 1) +
            ["SMB"] + [""] * (len(SMB_FIELDS) - 1) +
            ["Laser"] + [""] * (len(LASER_FIELDS) - 1) +
            ["MagneticField"] + [""] * (len(MAG_FIELDS) - 1) +
            ["System"] + [""] * (len(SYSTEM_FIELDS) - 1)
        )
        self._writer.writerow(
            CH_FIELDS + CH_FIELDS + ADC_FIELDS + SMB_FIELDS + LASER_FIELDS + MAG_FIELDS + SYSTEM_FIELDS
        )

    @staticmethod
    def _batch_len(rall_data: Dict[str, np.ndarray]) -> int:
        for value in rall_data.values():
            try:
                return min(len(value), SAMPLES_PER_BATCH)
            except TypeError:
                continue
        return SAMPLES_PER_BATCH

    def _channel_values(self, data: Dict[str, np.ndarray], ch: str, i: int) -> list[str]:
        prefix = f"lockin_{ch}_"
        x = self._mv_to_v(self._array_value(data, f"{prefix}X_mv", i))
        y = self._mv_to_v(self._array_value(data, f"{prefix}Y_mv", i))
        freq = self._array_value(data, f"{prefix}freq_hz", i)
        noise = self._mv_to_v(self._array_value(data, f"{prefix}noise_mv", i))
        xh1 = self._mv_to_v(self._array_value(data, f"{prefix}Xh1_mv", i))
        yh1 = self._mv_to_v(self._array_value(data, f"{prefix}Yh1_mv", i))
        xh2 = self._mv_to_v(self._array_value(data, f"{prefix}Xh2_mv", i))
        yh2 = self._mv_to_v(self._array_value(data, f"{prefix}Yh2_mv", i))

        r = math.hypot(x, y)
        theta = math.degrees(math.atan2(y, x)) if x or y else 0.0
        rh1 = math.hypot(xh1, yh1)
        theta_h1 = math.degrees(math.atan2(yh1, xh1)) if xh1 or yh1 else 0.0
        rh2 = math.hypot(xh2, yh2)
        theta_h2 = math.degrees(math.atan2(yh2, xh2)) if xh2 or yh2 else 0.0

        return [
            self._fmt(x), self._fmt(y), self._fmt(r), self._fmt(theta),
            self._fmt(freq), self._fmt(noise),
            self._fmt(xh1), self._fmt(yh1), self._fmt(rh1), self._fmt(theta_h1),
            self._fmt(xh2), self._fmt(yh2), self._fmt(rh2), self._fmt(theta_h2),
        ]

    def _adc_values(self, data: Dict[str, np.ndarray], i: int) -> list[str]:
        return [
            self._fmt(self._array_value(data, "aux_adc1_v", i)),
            self._fmt(self._array_value(data, "aux_adc2_v", i)),
            self._fmt(self._array_value(data, "aux_adc3_v", i)),
            self._fmt(self._array_value(data, "aux_adc4_v", i)),
        ]

    def _mag_values(self, state: Optional[Dict[str, Any]]) -> list[str]:
        axes = (state or {}).get("axes", {})
        values: list[str] = []
        for axis in ("X", "Y", "Z"):
            status = axes.get(axis, {})
            values.extend([
                self._fmt(float(status.get("target_field_nT", 0.0))),
                self._fmt(float(status.get("total_current_mA", 0.0))),
                "1" if status.get("output_on", False) else "0",
                "1" if status.get("lock_zero", False) else "0",
            ])
        return values

    @staticmethod
    def _array_value(data: Dict[str, np.ndarray], key: str, i: int) -> float:
        arr = data.get(key)
        if arr is None or len(arr) <= i:
            return 0.0
        return float(arr[i])

    @staticmethod
    def _mv_to_v(value: float) -> float:
        return value / 1000.0

    @staticmethod
    def _fmt(value: float) -> str:
        return f"{float(value):.12g}"

    def _save_columns_json(self) -> None:
        if self._output_dir is None:
            return
        columns: Dict[str, Any] = {}
        for group, fields in [
            ("CH-A", CH_FIELDS),
            ("CH-B", CH_FIELDS),
            ("ADC", ADC_FIELDS),
            ("SMB", SMB_FIELDS),
            ("Laser", LASER_FIELDS),
            ("MagneticField", MAG_FIELDS),
            ("System", SYSTEM_FIELDS),
        ]:
            for field in fields:
                columns[f"{group}_{field}"] = {"group": group, "name": field}
        doc = {
            "version": "2.0",
            "format": "csv",
            "encoding": "utf-8-sig",
            "data_file": "data.csv",
            "columns": columns,
        }
        with open(self._output_dir / "columns.json", "w", encoding="utf-8") as f:
            json.dump(doc, f, ensure_ascii=False, indent=2)

    def _save_metadata_json(self) -> None:
        if self._output_dir is None or self._start_time is None:
            return
        duration = time.monotonic() - self._start_time
        meta = {
            "instrument": "OE1022D+SMB100A+Laser+MagneticField",
            "command": "RALL?",
            "storage_format": "CSV (UTF-8 BOM)",
            "data_file": "data.csv",
            "sample_interval_s": 0.001,
            "rall_batch_interval_ms": 50,
            "samples_per_batch": SAMPLES_PER_BATCH,
            "total_batches": self._batch_count,
            "total_points": self._total_points,
            "duration_s": round(duration, 3),
            "acquisition_time": datetime.now().isoformat(),
            "layout": "OE1022D LabVIEW-style grouped CSV with CH-A, CH-B, ADC, source, laser and magnetic-field columns",
        }
        with open(self._output_dir / "metadata.json", "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)
