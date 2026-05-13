"""实时波形环形缓冲区

解决 PyQt 刷新率跟不上数据流的问题：
- 线程安全的环形缓冲区
- 固定容量，旧数据自动覆盖
- 支持多通道同时存储
- 提供降采样接口（用于显示）
"""

from __future__ import annotations

import threading
from collections import deque
from typing import Dict, List, Optional, Tuple

import numpy as np


class CircularBuffer:
    """多通道环形缓冲区。"""

    def __init__(self, channels: List[str], capacity: int = 2000) -> None:
        self._channels = channels
        self._capacity = capacity
        self._lock = threading.Lock()
        self._buffers: Dict[str, deque] = {ch: deque(maxlen=capacity) for ch in channels}
        self._timestamps: deque = deque(maxlen=capacity)

    @property
    def capacity(self) -> int:
        return self._capacity

    def append(self, data: Dict[str, float], timestamp: float | None = None) -> None:
        """追加单点数据。"""
        with self._lock:
            for ch in self._channels:
                self._buffers[ch].append(data.get(ch, 0.0))
            self._timestamps.append(timestamp if timestamp is not None else 0.0)

    def extend(self, data_dict: Dict[str, List[float]], timestamps: List[float] | None = None) -> None:
        """批量追加多点数据（如 RALL? 的一批 50 点）。"""
        with self._lock:
            n = 0
            for ch, values in data_dict.items():
                if ch in self._buffers:
                    n = len(values)
                    for v in values:
                        self._buffers[ch].append(v)
            if timestamps is not None:
                for t in timestamps:
                    self._timestamps.append(t)
            else:
                for _ in range(n):
                    self._timestamps.append(0.0)

    def get(
        self,
        channel: str,
        max_points: int | None = None,
        downsample: int = 1,
    ) -> Tuple[np.ndarray, np.ndarray]:
        """获取某通道的数据和时间戳。

        Args:
            channel: 通道名
            max_points: 最大返回点数（从最新数据往前数）
            downsample: 降采样因子（每 N 点取 1）

        Returns:
            (timestamps, values) 两个 numpy 数组
        """
        with self._lock:
            buf = self._buffers.get(channel)
            if buf is None:
                return np.array([]), np.array([])
            vals = np.array(buf, dtype=np.float64)
            ts = np.array(self._timestamps, dtype=np.float64)

        if max_points is not None and len(vals) > max_points:
            vals = vals[-max_points:]
            ts = ts[-max_points:]

        if downsample > 1:
            vals = vals[::downsample]
            ts = ts[::downsample]

        return ts, vals

    def get_latest(self, channel: str) -> float:
        """获取某通道最新值。"""
        with self._lock:
            buf = self._buffers.get(channel)
            if not buf:
                return 0.0
            return float(buf[-1])

    def get_all_latest(self) -> Dict[str, float]:
        """获取所有通道最新值。"""
        with self._lock:
            return {ch: float(buf[-1]) if buf else 0.0 for ch, buf in self._buffers.items()}

    def clear(self) -> None:
        """清空所有缓冲区。"""
        with self._lock:
            for buf in self._buffers.values():
                buf.clear()
            self._timestamps.clear()

    def get_channels(self) -> List[str]:
        return list(self._channels)
