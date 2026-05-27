"""实时波形环形缓冲区 (numpy 优化版)

解决 PyQt 刷新率跟不上数据流的问题：
- 预分配 numpy 数组，消除 Python deque 逐点 append 开销
- extend() 使用 numpy slice 批量写入，零 Python 逐点循环
- get() 从连续内存提取，避免从 deque 重建数组
- 固定容量，旧数据自动覆盖
- 线程安全
"""

from __future__ import annotations

import threading
from typing import Dict, List, Tuple

import numpy as np


class CircularBuffer:
    """多通道环形缓冲区，底层使用预分配 numpy 数组。"""

    def __init__(self, channels: List[str], capacity: int = 2000) -> None:
        self._channels = list(channels)
        self._capacity = max(capacity, 1)
        self._n_channels = len(channels)
        self._lock = threading.Lock()
        # 预分配 contiguous 内存
        self._data = np.zeros((self._capacity, self._n_channels), dtype=np.float64)
        self._timestamps = np.zeros(self._capacity, dtype=np.float64)
        self._index = 0  # 下一个写入位置
        self._size = 0   # 当前有效数据量

    @property
    def capacity(self) -> int:
        return self._capacity

    def append(self, data: Dict[str, float], timestamp: float | None = None) -> None:
        """追加单点数据。"""
        with self._lock:
            idx = self._index
            for i, ch in enumerate(self._channels):
                self._data[idx, i] = data.get(ch, 0.0)
            self._timestamps[idx] = timestamp if timestamp is not None else 0.0
            self._index = (idx + 1) % self._capacity
            self._size = min(self._size + 1, self._capacity)

    def extend(
        self,
        data_dict: Dict[str, np.ndarray | List[float]],
        timestamps: np.ndarray | List[float] | None = None,
    ) -> None:
        """批量追加多点数据（如 RALL? 的一批 50 点）。

        使用 numpy slice 批量写入，无 Python 逐点循环。
        """
        with self._lock:
            # 确定批量大小
            n = 0
            for vals in data_dict.values():
                if hasattr(vals, "__len__"):
                    n = len(vals)
                    break
            if n == 0:
                return

            # 构建 (n, n_channels) 批量数组
            batch = np.zeros((n, self._n_channels), dtype=np.float64)
            for i, ch in enumerate(self._channels):
                if ch in data_dict:
                    vals = data_dict[ch]
                    m = min(n, len(vals))
                    if m > 0:
                        batch[:m, i] = vals[:m]

            # 时间戳
            ts_batch = np.zeros(n, dtype=np.float64)
            if timestamps is not None and hasattr(timestamps, "__len__"):
                m = min(n, len(timestamps))
                if m > 0:
                    ts_batch[:m] = timestamps[:m]

            # 批量写入 ring buffer（处理跨越边界）
            idx = self._index
            cap = self._capacity
            if idx + n <= cap:
                self._data[idx:idx + n] = batch
                self._timestamps[idx:idx + n] = ts_batch
            else:
                first = cap - idx
                self._data[idx:] = batch[:first]
                self._data[:n - first] = batch[first:]
                self._timestamps[idx:] = ts_batch[:first]
                self._timestamps[:n - first] = ts_batch[first:]

            self._index = (idx + n) % cap
            self._size = min(self._size + n, cap)

    def get(
        self,
        channel: str,
        max_points: int | None = None,
        downsample: int = 1,
    ) -> Tuple[np.ndarray, np.ndarray]:
        """获取某通道的数据和时间戳。

        返回从旧到新的连续 numpy 数组（copy，确保线程安全）。
        """
        with self._lock:
            size = self._size
            idx = self._index
            if size == 0:
                return np.array([], dtype=np.float64), np.array([], dtype=np.float64)

            try:
                ch_idx = self._channels.index(channel)
            except ValueError:
                return np.array([], dtype=np.float64), np.array([], dtype=np.float64)

            if size < self._capacity:
                # 未满，直接切片（0..size-1 有效）
                vals = self._data[:size, ch_idx].copy()
                ts = self._timestamps[:size].copy()
            else:
                # 已满，重排为时间顺序（old..new）
                vals = np.concatenate((self._data[idx:, ch_idx], self._data[:idx, ch_idx]))
                ts = np.concatenate((self._timestamps[idx:], self._timestamps[:idx]))

        # 锁外做降采样（避免持有锁做切片）
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
            try:
                ch_idx = self._channels.index(channel)
            except ValueError:
                return 0.0
            if self._size == 0:
                return 0.0
            idx = (self._index - 1) % self._capacity
            return float(self._data[idx, ch_idx])

    def get_all_latest(self) -> Dict[str, float]:
        """获取所有通道最新值。"""
        with self._lock:
            if self._size == 0:
                return {ch: 0.0 for ch in self._channels}
            idx = (self._index - 1) % self._capacity
            return {ch: float(self._data[idx, i]) for i, ch in enumerate(self._channels)}

    def clear(self) -> None:
        """清空所有缓冲区。"""
        with self._lock:
            self._data.fill(0.0)
            self._timestamps.fill(0.0)
            self._index = 0
            self._size = 0

    def get_channels(self) -> List[str]:
        return list(self._channels)
