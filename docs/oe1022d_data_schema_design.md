# OE1022D 数据采集存储格式设计方案

## 背景

采集数据有两个下游用途：
1. **Python / MATLAB 快速画图**：需要直接读取、按列名索引、单位明确
2. **机器学习训练**：需要批量加载、按标签筛选特征列、数值范围合理

变量组合不固定——可能只采集 X/Y，也可能采集全部 20 个参数，还可能加入 SMB100A 频率等外部变量。

---

## 1. 格式选择

### 主存储：Parquet

| 特性 | 对画图的意义 | 对 ML 的意义 |
|------|-------------|-------------|
| 列式存储 | 只读 2 列画 XY 图，不加载其余 18 列 | 特征选择时按列切片，IO 开销小 |
| pandas 原生支持 | `pd.read_parquet()` 直接画图 | sklearn/pytorch 直接消费 DataFrame |
| 内嵌 schema 元数据 | 列名即表头，无需额外说明文件 | 每列的标签信息随文件走 |
| 压缩（Snappy/ZSTD） | 磁盘友好 | 大数据集可接受 |
| MATLAB 支持 | R2019a+ `parquetread()` | — |

### 辅助文件：`columns.json`

Parquet schema 的元数据只能存字符串键值对，复杂的结构化标签（枚举值、数值范围、归一化参数）放在 JSON 文件里，与 Parquet 同目录。

```
experiment_20260511/
├── data.parquet          # 主数据
├── columns.json          # 列定义 + 标签
└── metadata.json         # 采集元数据（仪器、时间、配置）
```

---

## 2. 列命名规范

### 格式

```
{source}_{quantity}_{unit}
```

| 字段 | 含义 | 示例 |
|------|------|------|
| `source` | 数据来源 / 通道 | `lockin_A`, `lockin_B`, `smb`, `aux` |
| `quantity` | 测量物理量 | `X`, `Y`, `R`, `theta`, `freq`, `noise` |
| `unit` | 单位（小写） | `mv`, `hz`, `v`, `dbm` |

### 完整列名映射（RALL? 20 参数）

| RALL? 原始参数 | 新列名 | 单位转换 |
|---------------|--------|----------|
| A-X | `lockin_A_X_mv` | V → mV (×1000) |
| A-Y | `lockin_A_Y_mv` | V → mV |
| A-Freq | `lockin_A_freq_hz` | Hz（不变） |
| A-Noise | `lockin_A_noise_mv` | V → mV |
| A-Xh1 | `lockin_A_Xh1_mv` | V → mV |
| A-Yh1 | `lockin_A_Yh1_mv` | V → mV |
| A-Xh2 | `lockin_A_Xh2_mv` | V → mV |
| A-Yh2 | `lockin_A_Yh2_mv` | V → mV |
| B-X | `lockin_B_X_mv` | V → mV |
| B-Y | `lockin_B_Y_mv` | V → mV |
| B-Freq | `lockin_B_freq_hz` | Hz（不变） |
| B-Noise | `lockin_B_noise_mv` | V → mV |
| B-Xh1 | `lockin_B_Xh1_mv` | V → mV |
| B-Yh1 | `lockin_B_Yh1_mv` | V → mV |
| B-Xh2 | `lockin_B_Xh2_mv` | V → mV |
| B-Yh2 | `lockin_B_Yh2_mv` | V → mV |
| AUXADC1 | `aux_adc1_v` | V（不变） |
| AUXADC2 | `aux_adc2_v` | V（不变） |
| AUXADC3 | `aux_adc3_v` | V（不变） |
| AUXADC4 | `aux_adc4_v` | V（不变） |

### 可能扩展的列（SMB100A 同步采集时）

| 列名 | 含义 | 单位 |
|------|------|------|
| `smb_freq_hz` | 微波源当前频率 | Hz |
| `smb_power_dbm` | 微波源功率 | dBm |
| `smb_rf_on` | RF 开关状态 | bool |
| `smb_lf_on` | LF 开关状态 | bool |

### 公共索引列

| 列名 | 含义 | 类型 |
|------|------|------|
| `sample_index` | 全局采样序号（从 0 递增） | int64 |
| `time_s` | 单调时间戳（秒，相对于采集起点） | float64 |
| `batch_index` | RALL? 批次序号 | int64 |

---

## 3. columns.json 结构

```json
{
  "version": "1.0",
  "columns": {
    "sample_index": {
      "description": "全局采样序号",
      "dtype": "int64",
      "role": "index"
    },
    "time_s": {
      "description": "单调时间戳（秒）",
      "dtype": "float64",
      "role": "index"
    },
    "batch_index": {
      "description": "RALL? 批次序号（每 50ms 一个批次）",
      "dtype": "int64",
      "role": "index"
    },
    "lockin_A_X_mv": {
      "description": "CH-A 同相分量 X",
      "dtype": "float64",
      "role": "feature",
      "source": "lockin_A",
      "quantity": "X",
      "unit": "mV",
      "raw_unit": "V",
      "scale_from_raw": 1000.0,
      "tags": ["lockin", "channel_A", "quadrature", "primary"]
    },
    "lockin_A_Y_mv": {
      "description": "CH-A 正交分量 Y",
      "dtype": "float64",
      "role": "feature",
      "source": "lockin_A",
      "quantity": "Y",
      "unit": "mV",
      "raw_unit": "V",
      "scale_from_raw": 1000.0,
      "tags": ["lockin", "channel_A", "quadrature", "primary"]
    },
    "lockin_A_freq_hz": {
      "description": "CH-A 参考频率",
      "dtype": "float64",
      "role": "feature",
      "source": "lockin_A",
      "quantity": "freq",
      "unit": "Hz",
      "raw_unit": "Hz",
      "scale_from_raw": 1.0,
      "tags": ["lockin", "channel_A", "frequency"]
    },
    "lockin_A_noise_mv": {
      "description": "CH-A 噪声",
      "dtype": "float64",
      "role": "feature",
      "source": "lockin_A",
      "quantity": "noise",
      "unit": "mV",
      "raw_unit": "V",
      "scale_from_raw": 1000.0,
      "tags": ["lockin", "channel_A", "noise"]
    },
    "aux_adc1_v": {
      "description": "辅助 ADC 输入 1",
      "dtype": "float64",
      "role": "feature",
      "source": "aux",
      "quantity": "adc1",
      "unit": "V",
      "raw_unit": "V",
      "scale_from_raw": 1.0,
      "tags": ["aux", "adc"]
    }
  }
}
```

### 字段说明

| 字段 | 用途 |
|------|------|
| `role` | ML 标注：`index`（索引/时间）、`feature`（特征）、`label`（标签）、`meta`（元信息） |
| `source` | 按来源分组筛选：`df.filter(like="lockin_A")` |
| `quantity` | 按物理量分组：所有通道的 X 分量 |
| `unit` | 当前列的单位 |
| `raw_unit` | 设备原始单位（方便反向转换） |
| `scale_from_raw` | raw_unit → unit 的乘数 |
| `tags` | 自由标签，用于 ML 特征选择，如 `["lockin", "channel_A", "primary"]` |

---

## 4. 使用示例

### 4.1 Python 画图

```python
import pandas as pd

df = pd.read_parquet("experiment/data.parquet")

# 直接画 CH-A 的 X vs Y（Lissajous 图）
df.plot("lockin_A_X_mv", "lockin_A_Y_mv", kind="scatter", s=1)

# 按来源批量筛选
ch_a_cols = [c for c in df.columns if c.startswith("lockin_A_")]
df[ch_a_cols].plot(subplots=True)

# MATLAB: parquetread("data.parquet") 同样直接可用
```

### 4.2 ML 特征选择

```python
import json

df = pd.read_parquet("experiment/data.parquet")
meta = json.load(open("experiment/columns.json"))

# 按 role 筛选特征列
feature_cols = [col for col, info in meta["columns"].items() if info["role"] == "feature"]
X = df[feature_cols]

# 按 tags 筛选（只要锁相放大器主信号）
primary_cols = [col for col, info in meta["columns"].items()
                if "primary" in info.get("tags", [])]
X_primary = df[primary_cols]

# 按 source 筛选（只要 CH-A 数据）
ch_a_cols = [col for col, info in meta["columns"].items()
             if info.get("source") == "lockin_A"]
X_cha = df[ch_a_cols]

# 归一化：利用 scale_from_raw 统一量纲
for col in feature_cols:
    info = meta["columns"][col]
    if info["unit"] == "mV":
        df[col] = df[col] / 1000.0  # 转回 V 统一量纲
```

### 4.3 添加自定义标签列（如 ODMR 实验标签）

```python
# 假设每 50ms 一个批次对应一个微波频率
# 在采集时同步记录频率，作为 ML 标签
df["label_freq_hz"] = ...  # 从 SMB100A 同步写入

# columns.json 中追加：
# "label_freq_hz": {"role": "label", "source": "smb", "quantity": "freq", "unit": "Hz"}
```

---

## 5. 存储布局建议

### 单次实验

```
experiment_20260511_143000/
├── data.parquet          # 全部采样数据
├── columns.json          # 列定义 + 标签
└── metadata.json         # 采集配置
```

### 长期运行（小时级）

数据量太大时按时间段分割：

```
experiment_20260511_143000/
├── data_0000.parquet     # 前 10 分钟
├── data_0001.parquet     # 10~20 分钟
├── ...
├── columns.json          # 所有分片共享同一列定义
└── metadata.json
```

pandas 可以直接读取目录下所有 parquet：
```python
df = pd.read_parquet("experiment_20260511_143000/")  # 自动合并
```

### ML 数据集组织

```
dataset_odmr/
├── train/
│   ├── experiment_001/
│   │   ├── data.parquet
│   │   └── columns.json
│   ├── experiment_002/
│   └── ...
├── val/
├── test/
└── dataset_meta.json     # 全局标签统计、特征列表
```

---

## 6. 与现有代码的兼容性

| 现有代码 | 兼容性 | 改动 |
|----------|--------|------|
| `CaptureStore` (CSV) | 不影响 | 两种格式并行，CSV 保留给已有实验 |
| `ExperimentWorker` | 不影响 | 新增 Parquet 输出路径即可 |
| `LockinWorker` | 不影响 | 实时显示不涉及存储格式 |
| `OE1022DController` | 不影响 | `exchange()` / `parse_rall()` 不变 |

---

## 7. 总结

| 决策 | 选择 | 理由 |
|------|------|------|
| 主格式 | Parquet | 列式、pandas 原生、压缩率高、MATLAB 支持 |
| 列名 | `{source}_{quantity}_{unit}` | 人类可读 + 正则可筛选 |
| 单位 | mV / Hz / V / dBm | 数值范围合理（避免 1e-9 级别） |
| 标签 | columns.json 的 `role` + `tags` | ML 特征选择直接可用 |
| 元数据 | Parquet schema + columns.json + metadata.json | 三层冗余，任意一层都能独立使用 |
