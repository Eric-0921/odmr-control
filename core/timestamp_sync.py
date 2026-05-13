"""多源时间戳同步中心（预留接口）

当前实现：
- register_source: 注册数据源
- ingest: 接收带时间戳的数据

未来扩展：
- query_aligned: 按时间戳对齐查询多源数据
- 支持磁场控制系统 (mag-field-control-v2) 的数据接入
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class TimestampedSample:
    """统一时间戳样本。"""

    host_timestamp_s: float      # time.monotonic() 主机单调时钟
    device_timestamp_s: float    # 设备本地时间（若可用，目前填 0）
    source: str                  # "smb" / "lockin_A" / "lockin_B" / "mag_field_x"...
    data: Dict[str, Any]         # 具体参数值


class TimestampSyncHub:
    """多源时间戳同步中心。

    当前阶段仅实现注册和数据摄入的基础结构。
    query_aligned 留空待后续实现。
    """

    def __init__(self) -> None:
        self._sources: Dict[str, Dict[str, Any]] = {}
        self._buffer: List[TimestampedSample] = []
        self._buffer_limit = 10000

    def register_source(self, source_id: str, poll_interval_ms: int) -> None:
        """注册一个数据源。"""
        self._sources[source_id] = {
            "poll_interval_ms": poll_interval_ms,
            "registered_at": time.monotonic(),
        }

    def ingest(self, sample: TimestampedSample) -> None:
        """接收一个带时间戳的数据样本。"""
        self._buffer.append(sample)
        # 限制 buffer 大小
        if len(self._buffer) > self._buffer_limit:
            self._buffer = self._buffer[-self._buffer_limit // 2:]

    def query_aligned(
        self, timestamp_s: float, tolerance_ms: float = 50
    ) -> Dict[str, Any]:
        """按时间戳对齐查询多源数据（预留接口，待实现）。"""
        # TODO: 实现最近邻匹配算法
        return {
            "query_timestamp_s": timestamp_s,
            "tolerance_ms": tolerance_ms,
            "sources": list(self._sources.keys()),
            "status": "not_implemented",
        }

    def get_sources(self) -> Dict[str, Dict[str, Any]]:
        """返回已注册的数据源信息。"""
        return self._sources.copy()
