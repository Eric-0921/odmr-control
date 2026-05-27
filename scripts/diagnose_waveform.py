#!/usr/bin/env python3
"""OE1022D 波形锯齿诊断脚本

用法：
    python scripts/diagnose_waveform.py          # 离线模拟
    python scripts/diagnose_waveform.py --hardware  # 连接真实硬件采集 60s
    python scripts/diagnose_waveform.py --csv experiments/xxx/data.csv  # 分析已有 CSV

诊断目标：定位残余锯齿的根因（A/B/C/D）
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import numpy as np

# 将项目根目录加入 path
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from data.circular_buffer import CircularBuffer


def offline_simulation(duration_s: float = 60.0) -> None:
    """离线模拟 LockinAcquireWorker 循环 + CircularBuffer + 渲染逻辑。

    使用真实的 time.sleep() 模拟 worker 循环，统计 batch 间 t_start 间隔。
    """
    print(f"=== 离线模拟：{duration_s}s 采集 ===")

    # 模拟参数
    interval_s = 0.050
    n_per_batch = 50
    dt = interval_s / n_per_batch  # 1ms
    buffer_capacity = 60000

    buf = CircularBuffer(channels=["R"], capacity=buffer_capacity)

    # 统计
    t_starts: list[float] = []
    t_intervals: list[float] = []

    t0 = time.monotonic()
    batch_count = 0

    # 模拟 worker 循环
    while True:
        t_start = time.monotonic()
        if t_start - t0 >= duration_s:
            break

        # 模拟 RALL? 读取耗时 ~14ms
        time.sleep(0.014)

        # 生成模拟数据（正弦波 + 噪声）
        base_ts = t_start
        timestamps = np.arange(n_per_batch, dtype=np.float64) * dt + base_ts
        # 1Hz 正弦波，振幅 100，加少量噪声
        phase = base_ts * 2 * np.pi  # 1Hz
        vals = 100.0 * np.sin(phase + np.arange(n_per_batch) * dt * 2 * np.pi)
        vals += np.random.normal(0, 2, n_per_batch)

        buf.extend({"R": vals}, timestamps)

        # 模拟 50ms 对齐
        elapsed = time.monotonic() - t_start
        sleep_time = interval_s - elapsed
        if sleep_time > 0.005:
            time.sleep(sleep_time)

        t_next = time.monotonic()
        t_intervals.append(t_next - t_start)
        t_starts.append(t_start)
        batch_count += 1

    print(f"Batch 数量: {batch_count}")
    print(f"Batch 间隔统计:")
    arr = np.array(t_intervals)
    print(f"  mean={arr.mean()*1000:.3f}ms  std={arr.std()*1000:.3f}ms")
    print(f"  min={arr.min()*1000:.3f}ms  max={arr.max()*1000:.3f}ms")
    print(f"  >55ms 占比: {np.mean(arr > 0.055)*100:.2f}%")
    print(f"  >60ms 占比: {np.mean(arr > 0.060)*100:.2f}%")

    # 模拟 _on_display_tick 的渲染逻辑
    print("\n=== 模拟渲染逻辑 ===")
    ts_arr, vals_arr = buf.get("R", max_points=int(duration_s * 1000), downsample=1)
    print(f"Buffer 总点数: {len(ts_arr)}")

    # 检查时间戳连续性（相对时间）
    if len(ts_arr) > 1:
        rel_ts = ts_arr - ts_arr[-1]
        diffs = np.diff(rel_ts)
        gaps = diffs < -0.003  # 正常间隔约 -1ms
        print(f"时间间隙 >3ms 的数量: {np.sum(gaps)}")
        if np.sum(gaps) > 0:
            print(f"  间隙位置 (相对时间): {rel_ts[np.where(gaps)[0] + 1][:5]}...")
            print(f"  间隙大小 (ms): {np.abs(diffs[gaps])[:5] * 1000}...")

    # 绘制结果
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        fig, axes = plt.subplots(2, 2, figsize=(14, 10))

        # 1. 波形图（模拟 _on_display_tick 逻辑，含 nan 断裂）
        ax = axes[0, 0]
        if len(ts_arr) > 1:
            rel_ts = ts_arr - ts_arr[-1]
            plot_ts = rel_ts.copy()
            plot_vals = vals_arr.copy()
            gaps = np.diff(plot_ts) < -0.003
            if np.any(gaps):
                gap_indices = np.where(gaps)[0] + 1
                plot_ts = np.insert(plot_ts, gap_indices, np.nan)
                plot_vals = np.insert(plot_vals, gap_indices, np.nan)
            ax.plot(plot_ts, plot_vals, linewidth=0.8)
        ax.set_xlabel("Relative Time (s)")
        ax.set_ylabel("R (arb)")
        ax.set_title("Waveform with nan-gap break (simulated render)")
        ax.grid(True, alpha=0.3)

        # 2. 波形图（无 nan 断裂，直接连接）
        ax = axes[0, 1]
        if len(ts_arr) > 1:
            rel_ts = ts_arr - ts_arr[-1]
            ax.plot(rel_ts, vals_arr, linewidth=0.8)
        ax.set_xlabel("Relative Time (s)")
        ax.set_ylabel("R (arb)")
        ax.set_title("Waveform without nan-gap break (direct connect)")
        ax.grid(True, alpha=0.3)

        # 3. batch 间隔直方图
        ax = axes[1, 0]
        ax.hist(arr * 1000, bins=50, edgecolor="black")
        ax.axvline(50, color="red", linestyle="--", label="Target 50ms")
        ax.set_xlabel("Batch Interval (ms)")
        ax.set_ylabel("Count")
        ax.set_title("Batch Interval Distribution")
        ax.legend()
        ax.grid(True, alpha=0.3)

        # 4. 时间戳 diff 分布
        ax = axes[1, 1]
        if len(ts_arr) > 1:
            rel_ts = ts_arr - ts_arr[-1]
            point_diffs = np.abs(np.diff(rel_ts)) * 1000  # ms
            ax.hist(point_diffs, bins=100, range=(0, 5), edgecolor="black")
            ax.axvline(1, color="green", linestyle="--", label="Expected 1ms")
            ax.axvline(3, color="red", linestyle="--", label="Gap threshold 3ms")
            ax.set_xlabel("Point Interval (ms)")
            ax.set_ylabel("Count")
            ax.set_title("Point Interval Distribution")
            ax.legend()
            ax.grid(True, alpha=0.3)

        plt.tight_layout()
        out_path = PROJECT_ROOT / "scripts" / "diagnose_waveform_offline.png"
        plt.savefig(out_path, dpi=150)
        print(f"\n图表已保存: {out_path}")

    except Exception as exc:
        print(f"matplotlib 绘图失败: {exc}")


def analyze_csv(csv_path: Path) -> None:
    """分析已采集的 CSV，检查时间戳间隙。"""
    print(f"=== 分析 CSV: {csv_path} ===")
    import csv

    times: list[float] = []
    with open(csv_path, "r", encoding="utf-8-sig") as f:
        reader = csv.reader(f)
        # 跳过前 3 行表头
        for _ in range(3):
            next(reader, None)
        for row in reader:
            if len(row) < 2:
                continue
            try:
                t = float(row[-2])  # time_s 在 System 组倒数第二列
                times.append(t)
            except ValueError:
                continue

    times_arr = np.array(times)
    print(f"总数据点: {len(times_arr)}")

    if len(times_arr) > 1:
        diffs = np.diff(times_arr) * 1000  # ms
        print(f"时间间隔统计:")
        print(f"  mean={diffs.mean():.3f}ms  std={diffs.std():.3f}ms")
        print(f"  min={diffs.min():.3f}ms  max={diffs.max():.3f}ms")
        print(f"  >3ms 数量: {np.sum(diffs > 3)} ({np.mean(diffs > 3)*100:.2f}%)")
        print(f"  >10ms 数量: {np.sum(diffs > 10)} ({np.mean(diffs > 10)*100:.2f}%)")

        # 检查 batch 边界（每 50 点一个 batch）
        batch_boundaries = diffs[49::50]
        if len(batch_boundaries) > 0:
            print(f"\nBatch 边界间隔统计:")
            print(f"  mean={batch_boundaries.mean():.3f}ms  std={batch_boundaries.std():.3f}ms")
            print(f"  min={batch_boundaries.min():.3f}ms  max={batch_boundaries.max():.3f}ms")

        try:
            import matplotlib
            matplotlib.use("Agg")
            import matplotlib.pyplot as plt

            fig, axes = plt.subplots(1, 2, figsize=(12, 4))

            ax = axes[0]
            ax.hist(diffs, bins=100, range=(0, 5), edgecolor="black")
            ax.axvline(1, color="green", linestyle="--", label="Expected 1ms")
            ax.axvline(3, color="red", linestyle="--", label="Gap threshold 3ms")
            ax.set_xlabel("Point Interval (ms)")
            ax.set_ylabel("Count")
            ax.set_title("CSV Point Interval Distribution")
            ax.legend()
            ax.grid(True, alpha=0.3)

            ax = axes[1]
            ax.plot(times_arr[1:], diffs, ".", markersize=1, alpha=0.5)
            ax.axhline(3, color="red", linestyle="--", label="Gap threshold 3ms")
            ax.set_xlabel("Time (s)")
            ax.set_ylabel("Interval (ms)")
            ax.set_title("Interval vs Time")
            ax.legend()
            ax.grid(True, alpha=0.3)

            plt.tight_layout()
            out_path = PROJECT_ROOT / "scripts" / "diagnose_waveform_csv.png"
            plt.savefig(out_path, dpi=150)
            print(f"\n图表已保存: {out_path}")
        except Exception as exc:
            print(f"matplotlib 绘图失败: {exc}")


def hardware_acquisition(duration_s: float = 60.0) -> None:
    """连接真实硬件采集 60s，导出 CSV 并分析。"""
    print(f"=== 真实硬件采集: {duration_s}s ===")
    from instruments.oe1022d import OE1022DDriver, RALL_TOTAL_BYTES
    from data.recorder import ODMRRecorder

    driver = OE1022DDriver()
    try:
        driver.connect("COM8", transport="usb2")
        print(f"已连接: {driver.idn}")
    except Exception as exc:
        print(f"连接失败: {exc}")
        return

    # 创建 recorder
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    recorder = ODMRRecorder(output_dir=PROJECT_ROOT / "experiments" / f"diagnose_{timestamp}")
    recorder.start_recording()
    print(f"Recording to: {recorder.csv_path}")

    t0 = time.monotonic()
    batch_count = 0
    t_intervals: list[float] = []

    try:
        driver.start_rall_stream()
        while time.monotonic() - t0 < duration_s:
            t_start = time.monotonic()

            driver.start_rall_stream()
            raw = driver.read_rall_batch(timeout=1.0)
            if len(raw) == RALL_TOTAL_BYTES:
                batch = driver.parse_rall(raw)
                batch["acquisition_stats"] = {
                    "batch_count": batch_count + 1,
                    "batch_timestamp_s": t_start,
                    "dropped_batches": 0,
                    "batch_rate_hz": 0.0,
                }
                recorder.write_batch(batch, batch_timestamp_s=t_start)
                batch_count += 1

            elapsed = time.monotonic() - t_start
            sleep_time = 0.050 - elapsed
            if sleep_time > 0.005:
                time.sleep(sleep_time)

            t_intervals.append(time.monotonic() - t_start)

        print(f"采集完成: {batch_count} batches")
        arr = np.array(t_intervals)
        print(f"Batch 间隔: mean={arr.mean()*1000:.3f}ms std={arr.std()*1000:.3f}ms")

    finally:
        recorder.stop_recording()
        driver.disconnect()
        print(f"CSV: {recorder.csv_path}")
        if recorder.csv_path and recorder.csv_path.exists():
            analyze_csv(recorder.csv_path)


def main() -> None:
    parser = argparse.ArgumentParser(description="OE1022D 波形锯齿诊断")
    parser.add_argument("--hardware", action="store_true", help="连接真实硬件采集")
    parser.add_argument("--csv", type=Path, help="分析已有 CSV")
    parser.add_argument("--duration", type=float, default=60.0, help="采集时长（秒）")
    args = parser.parse_args()

    if args.csv:
        analyze_csv(args.csv)
    elif args.hardware:
        hardware_acquisition(args.duration)
    else:
        offline_simulation(args.duration)


if __name__ == "__main__":
    main()
