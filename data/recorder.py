"""数据记录模块

严格遵循 oe1022d_data_schema_design.md：
- 主格式：Parquet + Snappy 压缩
- 列名规范：{source}_{quantity}_{unit}
- 辅助文件：columns.json（列定义 + ML 标签）, metadata.json（元数据）

输出目录结构：
    experiment_20260511_143000/
    ├── data.parquet
    ├── columns.json
    └── metadata.json
"""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np

from instruments.oe1022d import RALL_PARAMS, SAMPLES_PER_BATCH


try:
    import pyarrow as pa
    import pyarrow.parquet as pq
    _HAS_PYARROW = True
except ImportError:
    _HAS_PYARROW = False


class ODMRRecorder:
    """Parquet 数据记录器，支持实时追加。线程安全。"""

    def __init__(self, output_dir: Path | str | None = None) -> None:
        self._output_dir: Optional[Path] = None
        self._schema: Optional[pa.Schema] = None
        self._writer: Optional[pq.ParquetWriter] = None
        self._batch_count = 0
        self._total_points = 0
        self._start_time: Optional[float] = None
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

    def set_output_dir(self, path: Path | str) -> None:
        self._output_dir = Path(path)
        self._output_dir.mkdir(parents=True, exist_ok=True)

    def _build_schema(self) -> pa.Schema:
        """构建 PyArrow Schema，含设备元数据。"""
        fields = [
            pa.field("sample_index", pa.int64(), metadata={"description": "全局采样序号", "role": "index"}),
            pa.field("time_s", pa.float64(), metadata={"description": "单调时间戳(秒)", "role": "index"}),
            pa.field("host_timestamp_s", pa.float64(), metadata={"description": "主机单调时钟(秒)", "role": "index"}),
            pa.field("device_timestamp_s", pa.float64(), metadata={"description": "设备本地时间(秒,占位)", "role": "index"}),
            pa.field("batch_index", pa.int64(), metadata={"description": "RALL? 批次序号", "role": "index"}),
        ]
        for col_name, raw_unit, unit, _scale, source, quantity, desc, tags in RALL_PARAMS:
            fields.append(pa.field(
                col_name, pa.float64(),
                metadata={
                    "description": desc.encode("utf-8"),
                    "unit": unit.encode("utf-8"),
                    "raw_unit": raw_unit.encode("utf-8"),
                    "source": source.encode("utf-8"),
                    "quantity": quantity.encode("utf-8"),
                    "role": "feature",
                    "tags": ",".join(tags).encode("utf-8"),
                }
            ))
        # 扩展：SMB 同步参数
        smb_fields = [
            pa.field("smb_freq_hz", pa.float64(), metadata={"description": "SMB100A 频率", "unit": "Hz", "role": "feature", "source": "smb", "quantity": "freq"}),
            pa.field("smb_power_dbm", pa.float64(), metadata={"description": "SMB100A 功率", "unit": "dBm", "role": "feature", "source": "smb", "quantity": "power"}),
            pa.field("smb_rf_on", pa.bool_(), metadata={"description": "RF 开关", "role": "feature", "source": "smb", "quantity": "rf_on"}),
        ]
        fields.extend(smb_fields)

        # 扩展：激光器同步参数
        laser_fields = [
            pa.field("laser_power_mw", pa.float64(), metadata={"description": "激光功率", "unit": "mW", "role": "feature", "source": "laser", "quantity": "power"}),
            pa.field("laser_on", pa.bool_(), metadata={"description": "激光开关", "role": "feature", "source": "laser", "quantity": "on"}),
        ]
        fields.extend(laser_fields)

        return pa.schema(fields, metadata={
            "instrument": "OE1022D+SMB100A+Laser",
            "command": "RALL?",
            "sample_rate_hz": "1000",
            "batch_interval_ms": "50",
            "samples_per_batch": str(SAMPLES_PER_BATCH),
            "column_naming": "{source}_{quantity}_{unit}",
        })

    def start_recording(self) -> None:
        """开始记录，创建 Schema 和 Writer。"""
        if not _HAS_PYARROW:
            raise RuntimeError("pyarrow is not installed")
        if self._output_dir is None:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            self.set_output_dir(Path(f"./experiments/experiment_{timestamp}"))
        if self._output_dir is None:
            raise RuntimeError("无法创建输出目录")

        self._schema = self._build_schema()
        parquet_path = self._output_dir / "data.parquet"
        self._writer = pq.ParquetWriter(
            parquet_path, self._schema,
            compression="snappy",
            use_dictionary=["smb_rf_on"],
        )
        self._batch_count = 0
        self._total_points = 0
        self._start_time = time.monotonic()
        self._is_recording = True

        # 同时生成 columns.json
        self._save_columns_json()

    def stop_recording(self) -> None:
        """停止记录，关闭 Writer，生成 metadata.json。幂等：多次调用安全。"""
        with self._lock:
            if not self._is_recording:
                return
            self._is_recording = False
            if self._writer is not None:
                self._writer.close()
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
    ) -> None:
        """写入一批 RALL? 数据（50 点）。线程安全。"""
        with self._lock:
            if not self._is_recording or self._writer is None or self._schema is None:
                return
            if self._start_time is None:
                return

            n = SAMPLES_PER_BATCH
            t_batch = time.monotonic() - self._start_time
            host_ts = time.monotonic()
            sample_indices = np.arange(self._total_points, self._total_points + n, dtype=np.int64)
            time_s = np.full(n, t_batch, dtype=np.float64)
            host_timestamp_s = np.full(n, host_ts, dtype=np.float64)
            device_timestamp_s = np.full(n, 0.0, dtype=np.float64)  # 占位
            batch_indices = np.full(n, self._batch_count, dtype=np.int64)

            arrays = [
                pa.array(sample_indices),
                pa.array(time_s),
                pa.array(host_timestamp_s),
                pa.array(device_timestamp_s),
                pa.array(batch_indices),
            ]

            for col_name, _, _, _, _, _, _, _ in RALL_PARAMS:
                arr = rall_data.get(col_name, np.zeros(n, dtype=np.float64))
                arrays.append(pa.array(arr))

            # SMB 同步参数：同一批次内频率/功率相同
            arrays.append(pa.array(np.full(n, smb_freq_hz, dtype=np.float64)))
            arrays.append(pa.array(np.full(n, smb_power_dbm, dtype=np.float64)))
            arrays.append(pa.array(np.full(n, smb_rf_on, dtype=bool)))

            # 激光器同步参数
            arrays.append(pa.array(np.full(n, laser_power_mw, dtype=np.float64)))
            arrays.append(pa.array(np.full(n, laser_on, dtype=bool)))

            table = pa.table(arrays, schema=self._schema)
            self._writer.write_table(table)

            self._batch_count += 1
            self._total_points += n

    def _save_columns_json(self) -> None:
        """生成 columns.json。"""
        if self._output_dir is None:
            return
        columns: Dict[str, Any] = {
            "sample_index": {
                "description": "全局采样序号",
                "dtype": "int64",
                "role": "index",
            },
            "time_s": {
                "description": "单调时间戳（秒，相对于采集起点）",
                "dtype": "float64",
                "role": "index",
            },
            "host_timestamp_s": {
                "description": "主机单调时钟（秒，绝对时间）",
                "dtype": "float64",
                "role": "index",
            },
            "device_timestamp_s": {
                "description": "设备本地时间（秒，占位）",
                "dtype": "float64",
                "role": "index",
            },
            "batch_index": {
                "description": "RALL? 批次序号（每 50ms 一个批次）",
                "dtype": "int64",
                "role": "index",
            },
        }
        for col_name, raw_unit, unit, scale, source, quantity, desc, tags in RALL_PARAMS:
            columns[col_name] = {
                "description": desc,
                "dtype": "float64",
                "role": "feature",
                "source": source,
                "quantity": quantity,
                "unit": unit,
                "raw_unit": raw_unit,
                "scale_from_raw": scale,
                "tags": tags,
            }
        # SMB 扩展
        columns["smb_freq_hz"] = {"description": "SMB100A 频率", "dtype": "float64", "role": "feature", "source": "smb", "quantity": "freq", "unit": "Hz"}
        columns["smb_power_dbm"] = {"description": "SMB100A 功率", "dtype": "float64", "role": "feature", "source": "smb", "quantity": "power", "unit": "dBm"}
        columns["smb_rf_on"] = {"description": "RF 开关状态", "dtype": "bool", "role": "feature", "source": "smb", "quantity": "rf_on"}

        # 激光器扩展
        columns["laser_power_mw"] = {"description": "激光功率", "dtype": "float64", "role": "feature", "source": "laser", "quantity": "power", "unit": "mW"}
        columns["laser_on"] = {"description": "激光开关状态", "dtype": "bool", "role": "feature", "source": "laser", "quantity": "on"}

        doc = {"version": "1.0", "columns": columns}
        path = self._output_dir / "columns.json"
        with open(path, "w", encoding="utf-8") as f:
            json.dump(doc, f, ensure_ascii=False, indent=2)

    def _save_metadata_json(self) -> None:
        """生成 metadata.json。"""
        if self._output_dir is None or self._start_time is None:
            return
        duration = time.monotonic() - self._start_time
        meta = {
            "instrument": "OE1022D+SMB100A+Laser",
            "connection": "USB 2.0 + VISA",
            "command": "RALL?",
            "sample_rate_hz": 1000,
            "batch_interval_ms": 50,
            "samples_per_batch": SAMPLES_PER_BATCH,
            "total_batches": self._batch_count,
            "total_points_per_param": self._total_points,
            "duration_s": round(duration, 3),
            "column_naming": "{source}_{quantity}_{unit}",
            "storage_format": "Parquet (Snappy)",
            "schema_reference": "oe1022d_data_schema_design.md",
            "acquisition_time": datetime.now().isoformat(),
            "timestamp_sync": {
                "strategy": "host_monotonic_clock",
                "host_timestamp_col": "host_timestamp_s",
                "device_timestamp_col": "device_timestamp_s",
                "sources": {
                    "smb": {"poll_interval_ms": 100},
                    "lockin": {"poll_interval_ms": 50},
                },
            },
            "parameters": [
                {"name": n, "unit": u, "raw_unit": ru, "source": s, "quantity": q, "description": d}
                for n, ru, u, _, s, q, d, _ in RALL_PARAMS
            ],
        }
        path = self._output_dir / "metadata.json"
        with open(path, "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)
