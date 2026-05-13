"""
OE1022D RALL? 数据采集 + Parquet 存储
基于 USB 2.0 的 RALL? 命令，每 50ms 读取 12288 bytes 二进制数据。

列命名规范: {source}_{quantity}_{unit}
存储格式: Parquet + columns.json + metadata.json
详见: oe1022d_data_schema_design.md

用法:
  python test_rall_save.py --port COM3              # 连接真实设备
  python test_rall_save.py --simulate               # 模拟数据（无需设备）
  python test_rall_save.py --simulate --batches 400  # 模拟 20 秒采集
"""

import struct
import time
import argparse
import json
from pathlib import Path
from datetime import datetime

import numpy as np

# ── RALL? 参数定义 ──────────────────────────────────────────────
# 每项: (col_name, raw_unit, unit, scale, source, quantity, description, tags)
RALL_PARAMS = [
    ("lockin_A_X_mv",     "V",  "mV", 1000.0, "lockin_A", "X",     "CH-A 同相分量 X",       ["lockin", "channel_A", "quadrature", "primary"]),
    ("lockin_A_Y_mv",     "V",  "mV", 1000.0, "lockin_A", "Y",     "CH-A 正交分量 Y",       ["lockin", "channel_A", "quadrature", "primary"]),
    ("lockin_A_freq_hz",  "Hz", "Hz", 1.0,    "lockin_A", "freq",  "CH-A 参考频率",         ["lockin", "channel_A", "frequency"]),
    ("lockin_A_noise_mv", "V",  "mV", 1000.0, "lockin_A", "noise", "CH-A 噪声",             ["lockin", "channel_A", "noise"]),
    ("lockin_A_Xh1_mv",   "V",  "mV", 1000.0, "lockin_A", "Xh1",   "CH-A 1次谐波 X",        ["lockin", "channel_A", "harmonic"]),
    ("lockin_A_Yh1_mv",   "V",  "mV", 1000.0, "lockin_A", "Yh1",   "CH-A 1次谐波 Y",        ["lockin", "channel_A", "harmonic"]),
    ("lockin_A_Xh2_mv",   "V",  "mV", 1000.0, "lockin_A", "Xh2",   "CH-A 2次谐波 X",        ["lockin", "channel_A", "harmonic"]),
    ("lockin_A_Yh2_mv",   "V",  "mV", 1000.0, "lockin_A", "Yh2",   "CH-A 2次谐波 Y",        ["lockin", "channel_A", "harmonic"]),
    ("lockin_B_X_mv",     "V",  "mV", 1000.0, "lockin_B", "X",     "CH-B 同相分量 X",       ["lockin", "channel_B", "quadrature", "primary"]),
    ("lockin_B_Y_mv",     "V",  "mV", 1000.0, "lockin_B", "Y",     "CH-B 正交分量 Y",       ["lockin", "channel_B", "quadrature", "primary"]),
    ("lockin_B_freq_hz",  "Hz", "Hz", 1.0,    "lockin_B", "freq",  "CH-B 参考频率",         ["lockin", "channel_B", "frequency"]),
    ("lockin_B_noise_mv", "V",  "mV", 1000.0, "lockin_B", "noise", "CH-B 噪声",             ["lockin", "channel_B", "noise"]),
    ("lockin_B_Xh1_mv",   "V",  "mV", 1000.0, "lockin_B", "Xh1",   "CH-B 1次谐波 X",        ["lockin", "channel_B", "harmonic"]),
    ("lockin_B_Yh1_mv",   "V",  "mV", 1000.0, "lockin_B", "Yh1",   "CH-B 1次谐波 Y",        ["lockin", "channel_B", "harmonic"]),
    ("lockin_B_Xh2_mv",   "V",  "mV", 1000.0, "lockin_B", "Xh2",   "CH-B 2次谐波 X",        ["lockin", "channel_B", "harmonic"]),
    ("lockin_B_Yh2_mv",   "V",  "mV", 1000.0, "lockin_B", "Yh2",   "CH-B 2次谐波 Y",        ["lockin", "channel_B", "harmonic"]),
    ("aux_adc1_v",        "V",  "V",  1.0,    "aux",      "adc1",  "辅助 ADC 输入 1",       ["aux", "adc"]),
    ("aux_adc2_v",        "V",  "V",  1.0,    "aux",      "adc2",  "辅助 ADC 输入 2",       ["aux", "adc"]),
    ("aux_adc3_v",        "V",  "V",  1.0,    "aux",      "adc3",  "辅助 ADC 输入 3",       ["aux", "adc"]),
    ("aux_adc4_v",        "V",  "V",  1.0,    "aux",      "adc4",  "辅助 ADC 输入 4",       ["aux", "adc"]),
]

RALL_TOTAL_BYTES = 12288
SAMPLES_PER_BATCH = 50


# ── RALL? 解析 ──────────────────────────────────────────────────

def parse_rall(raw: bytes) -> dict[str, np.ndarray]:
    """解析 RALL? 返回的 12288 bytes，返回新列名 + 单位转换后的数据"""
    if len(raw) < 8000:
        raise ValueError(f"数据不完整: {len(raw)} < 8000 bytes")
    data = {}
    for i, (col_name, _, _, scale, _, _, _, _) in enumerate(RALL_PARAMS):
        offset = i * 400  # 50 samples x 8 bytes
        samples = np.frombuffer(raw, dtype="<f8", count=50, offset=offset).copy()
        data[col_name] = samples * scale
    return data


def generate_simulated_rall(batch_index: int) -> bytes:
    """生成模拟的 12288 bytes RALL? 数据（用于无设备测试）"""
    raw = bytearray(RALL_TOTAL_BYTES)
    t = batch_index * 0.05
    for i in range(len(RALL_PARAMS)):
        offset = i * 400
        for j in range(50):
            val = np.sin(2 * np.pi * 10 * (t + j * 0.001)) * 0.001 + np.random.randn() * 1e-6
            struct.pack_into("<d", raw, offset + j * 8, val)
    return bytes(raw)


# ── 采集 ────────────────────────────────────────────────────────

def acquire_batches(port: str | None, n_batches: int, simulate: bool) -> dict[str, np.ndarray]:
    """采集 n_batches 批 RALL? 数据，返回按新列名组织的 dict"""
    if simulate:
        ser = None
        print(f"[模拟模式] 生成 {n_batches} 批次数据")
    else:
        import serial
        ser = serial.Serial(port=port, baudrate=921600, bytesize=8, parity="N", stopbits=1, timeout=2)
        ser.reset_input_buffer()
        ser.reset_output_buffer()
        ser.write(b"*IDND?\r")
        time.sleep(0.1)
        idn = ser.read(ser.in_waiting).decode("ascii", errors="ignore").strip()
        print(f"设备: {idn}")

    col_names = [p[0] for p in RALL_PARAMS]
    columns = {name: [] for name in col_names}
    batch_times = []
    batch_indices = []
    dropped = 0

    t_start = time.monotonic()
    for i in range(n_batches):
        t_batch = time.monotonic()

        if simulate:
            raw = generate_simulated_rall(i)
            time.sleep(0.001)
        else:
            ser.write(b"RALL?\r")
            raw = b""
            while len(raw) < RALL_TOTAL_BYTES:
                chunk = ser.read(RALL_TOTAL_BYTES - len(raw))
                if not chunk:
                    break
                raw += chunk
            if len(raw) != RALL_TOTAL_BYTES:
                dropped += 1
                continue

        batch = parse_rall(raw)
        batch_times.append(t_batch - t_start)
        batch_indices.append(i)
        for name in col_names:
            columns[name].append(batch[name])

        if (i + 1) % 200 == 0:
            print(f"  {i+1}/{n_batches} 批次完成")

    if ser:
        ser.close()

    # 拼接为连续数组
    result = {}
    for name in col_names:
        result[name] = np.concatenate(columns[name])

    # 生成索引列
    n_points = len(batch_times) * SAMPLES_PER_BATCH
    result["sample_index"] = np.arange(n_points, dtype=np.int64)
    result["time_s"] = np.repeat(batch_times, SAMPLES_PER_BATCH)
    result["batch_index"] = np.repeat(batch_indices, SAMPLES_PER_BATCH)

    print(f"采集完成: {n_points} 点/参数, {dropped} 批丢弃")
    return result


# ── 存储 ────────────────────────────────────────────────────────

def save_parquet(data: dict, out_path: Path):
    """保存为 Parquet，列名和 schema 符合 oe1022d_data_schema_design.md 规范"""
    import pyarrow as pa
    import pyarrow.parquet as pq

    # 构建 schema
    fields = [
        pa.field("sample_index", pa.int64(), metadata={"description": "全局采样序号", "role": "index"}),
        pa.field("time_s", pa.float64(), metadata={"description": "单调时间戳(秒)", "role": "index"}),
        pa.field("batch_index", pa.int64(), metadata={"description": "RALL? 批次序号", "role": "index"}),
    ]
    for col_name, raw_unit, unit, _, source, quantity, desc, tags in RALL_PARAMS:
        fields.append(pa.field(
            col_name, pa.float64(),
            metadata={
                "description": desc.encode(),
                "unit": unit.encode(),
                "raw_unit": raw_unit.encode(),
                "source": source.encode(),
                "quantity": quantity.encode(),
                "role": "feature",
                "tags": ",".join(tags),
            }
        ))

    schema = pa.schema(fields, metadata={
        "instrument": "OE1022D",
        "command": "RALL?",
        "sample_rate_hz": "1000",
        "batch_interval_ms": "50",
        "samples_per_batch": str(SAMPLES_PER_BATCH),
        "column_naming": "{source}_{quantity}_{unit}",
    })

    # 按 schema 顺序构建 arrays
    arrays = [pa.array(data[f.name]) for f in schema]
    table = pa.table(arrays, schema=schema)
    pq.write_table(table, out_path, compression="snappy")
    print(f"Parquet: {out_path} ({out_path.stat().st_size / 1024 / 1024:.2f} MB)")


def save_columns_json(out_path: Path):
    """根据 RALL_PARAMS 生成 columns.json"""
    columns = {
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

    doc = {"version": "1.0", "columns": columns}
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2)
    print(f"columns.json: {out_path}")


def save_metadata(out_path: Path, n_batches: int, duration_s: float):
    """保存采集元数据"""
    meta = {
        "instrument": "OE1022D",
        "connection": "USB 2.0",
        "command": "RALL?",
        "sample_rate_hz": 1000,
        "batch_interval_ms": 50,
        "samples_per_batch": SAMPLES_PER_BATCH,
        "total_batches": n_batches,
        "total_points_per_param": n_batches * SAMPLES_PER_BATCH,
        "duration_s": round(duration_s, 3),
        "column_naming": "{source}_{quantity}_{unit}",
        "storage_format": "Parquet (Snappy)",
        "schema_reference": "oe1022d_data_schema_design.md",
        "acquisition_time": datetime.now().isoformat(),
        "parameters": [
            {"name": n, "unit": u, "raw_unit": ru, "source": s, "quantity": q, "description": d}
            for n, ru, u, _, s, q, d, _ in RALL_PARAMS
        ],
    }
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    print(f"metadata.json: {out_path}")


# ── 主流程 ──────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="OE1022D RALL? 数据采集 (Parquet)")
    parser.add_argument("--port", help="串口号 (如 COM3)")
    parser.add_argument("--simulate", action="store_true", help="使用模拟数据")
    parser.add_argument("--batches", type=int, default=200, help="采集批次数 (默认 200 = 10秒)")
    parser.add_argument("--output", type=str, default=None, help="输出目录名")
    args = parser.parse_args()

    if not args.simulate and not args.port:
        parser.error("需要 --port 或 --simulate")

    # 输出目录
    if args.output:
        out_dir = Path(__file__).parent / args.output
    else:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_dir = Path(__file__).parent / f"captures_{timestamp}"
    out_dir.mkdir(exist_ok=True)
    print(f"输出目录: {out_dir}\n")

    # 采集
    t_start = time.monotonic()
    data = acquire_batches(args.port, args.batches, args.simulate)
    duration = time.monotonic() - t_start

    # 保存
    save_parquet(data, out_dir / "data.parquet")
    save_columns_json(out_dir / "columns.json")
    save_metadata(out_dir / "metadata.json", args.batches, duration)

    # 验证
    import pyarrow.parquet as pq
    table = pq.read_table(out_dir / "data.parquet")
    print(f"\n验证: {table.num_rows} 行 x {table.num_columns} 列")
    print(f"列名: {table.column_names}")


if __name__ == "__main__":
    main()
