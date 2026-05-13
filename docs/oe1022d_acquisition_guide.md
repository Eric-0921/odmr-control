# OE1022D 长期高速数据采集指南

基于 SINE Scientific Instruments OE1022D DSP 锁相放大器操作手册（Rev 1.9, 2025-08-14）整理。

相关文档：
- `oe1022d_data_schema_design.md` — 数据存储格式设计方案（列命名、columns.json 规范）
- `test_rall_save.py` — 采集脚本（可直接运行）

---

## 1. 设备连接

### 1.1 物理接口

OE1022D 提供两种通信接口：

| 接口 | 速率 | 特点 |
|------|------|------|
| USB 2.0 | 全速 | 支持 `RALL?` 批量读取（本方案核心） |
| RS-232 | 最高 921600 bps | 不支持 `RALL?`，仅可用缓冲采样 |

**本指南基于 USB 连接。**

### 1.2 串口参数

| 参数 | 值 |
|------|-----|
| 波特率 | 921600 |
| 数据位 | 8 |
| 校验位 | None |
| 停止位 | 1 |

> 来源：手册 Section 8.4, "OE1022D 默认波特率为 921600，校验位无，数据位 8 位，停止位 1 位"

### 1.3 命令格式

- 命令助记符为 **4 个大写 ASCII 字符**，可带参数
- 终结符为 **回车符 `\r`** (0x0D) 或换行符 `\n` (0x0A)，设备收到终结符才执行
- 多参数用逗号 `,` 分隔
- 多命令用分号 `;` 连接，单行不超过 **256 字符**
- 查询命令在助记符后加 `?`，省略设置参数

> 来源：手册 Section 5.1, "命令符使用大写，所有命令均由四个命令字符（如有必要可带上参数）和一个命令终结符组成"

### 1.4 基本问询命令

```python
import serial

ser = serial.Serial("COMx", baudrate=921600, bytesize=8, parity="N", stopbits=1, timeout=2)
ser.reset_input_buffer()
ser.reset_output_buffer()

# 设备识别
ser.write(b"*IDND?\r")
# 返回: "SSI LIA-OE1022D,SNXXXXXX,VerXXX"

# 同时读取 CH-A 的 X, Y, R, theta（推荐方式，同一时刻采样）
ser.write(b"SNAPD? 1,0,1,2,3\r")
# 返回: "0.951359, 0.0253297, 1000.00, 1.234"

# 读取单个参数（有顺序延迟，短时间常数时不如 SNAPD?）
ser.write(b"OUTPD? 1,2\r")
# 返回: "1000.00" (CH-A 的 R 值)

# 读取 AUX 输入电压
ser.write(b"OAUXD? 1\r")

# 状态查询
ser.write(b"INOVD? 1\r")   # 输入过载 (0=正常, 1=过载)
ser.write(b"GNOVD? 1\r")   # 增益过载
ser.write(b"*PLLD? 1\r")   # PLL 锁定 (0=未锁定, 1=锁定)
```

> **为什么用 `SNAPD?` 而不是多次 `OUTPD?`？**
> 手册 Section 5.2.9 明确指出："SNAPD? i, j, k 指令用于在同一个时间点记录最多 5 个不同的参数值……如果使用 OUTPD? 指令来读取两个不同的参数值，可能会有一定的延时，当这个延时值大于时间常数的时候，就会使得读取的两个数据不是同一测量条件下测量所得。"

---

## 2. 推荐方案：RALL? 二进制批量读取

### 2.1 为什么选择 RALL?

| 考量 | RALL? 方案 | 循环缓冲方案 (TRCAD?) |
|------|-----------|---------------------|
| 接口 | 仅 USB 2.0 | RS232 / USB |
| 采样率 | 固定 1kHz (1ms) | 1Hz~1kHz 可调 |
| 参数数 | **20 个同步** | 最多 4 个 |
| 传输格式 | 二进制 float64 | ASCII 浮点 |
| 持续时间 | **无限**（流式） | 受 16384 点缓冲区限制 |
| 数据丢失风险 | **低** | 高（缓冲区环形覆盖） |
| 实现复杂度 | 低 | 中（需轮询 + 分段读取） |

**核心理由**：
1. **20 参数同步**：RALL? 一次返回 A/B 双通道的 X, Y, Freq, Noise, Xh1, Yh1, Xh2, Yh2 加 4 路 AUX ADC，覆盖所有常用测量量
2. **无覆盖风险**：设备内部以 50ms 为周期持续刷新，读取不及时仅丢失该批次，不会破坏历史数据
3. **二进制传输**：12288 bytes 固定长度，比 ASCII 解析快一个数量级
4. **无限持续**：只要持续发送 `RALL?`，设备就持续返回数据，适合长时间实验

### 2.2 RALL? 数据布局

设备每次返回 **12,288 bytes** 二进制数据，内部以 1ms 间隔采样，每 50ms 打包最近 50 个点。

#### 前 8000 bytes：测量数据

每个参数占 400 bytes = 50 个采样点 x 8 bytes (64-bit IEEE 754 浮点数)。

| 字节偏移 | 设备参数 | 存储列名 | 说明 |
|----------|---------|----------|------|
| 0 ~ 399 | A-X | `lockin_A_X_mv` | CH-A 同相分量 (V→mV) |
| 400 ~ 799 | A-Y | `lockin_A_Y_mv` | CH-A 正交分量 (V→mV) |
| 800 ~ 1199 | A-Freq | `lockin_A_freq_hz` | CH-A 参考频率 |
| 1200 ~ 1599 | A-Noise | `lockin_A_noise_mv` | CH-A 噪声 (V→mV) |
| 1600 ~ 1999 | A-Xh1 | `lockin_A_Xh1_mv` | CH-A 1 次谐波 X (V→mV) |
| 2000 ~ 2399 | A-Yh1 | `lockin_A_Yh1_mv` | CH-A 1 次谐波 Y (V→mV) |
| 2400 ~ 2799 | A-Xh2 | `lockin_A_Xh2_mv` | CH-A 2 次谐波 X (V→mV) |
| 2800 ~ 3199 | A-Yh2 | `lockin_A_Yh2_mv` | CH-A 2 次谐波 Y (V→mV) |
| 3200 ~ 3599 | B-X | `lockin_B_X_mv` | CH-B 同相分量 (V→mV) |
| 3600 ~ 3999 | B-Y | `lockin_B_Y_mv` | CH-B 正交分量 (V→mV) |
| 4000 ~ 4399 | B-Freq | `lockin_B_freq_hz` | CH-B 参考频率 |
| 4400 ~ 4799 | B-Noise | `lockin_B_noise_mv` | CH-B 噪声 (V→mV) |
| 4800 ~ 5199 | B-Xh1 | `lockin_B_Xh1_mv` | CH-B 1 次谐波 X (V→mV) |
| 5200 ~ 5599 | B-Yh1 | `lockin_B_Yh1_mv` | CH-B 1 次谐波 Y (V→mV) |
| 5600 ~ 5999 | B-Xh2 | `lockin_B_Xh2_mv` | CH-B 2 次谐波 X (V→mV) |
| 6000 ~ 6399 | B-Yh2 | `lockin_B_Yh2_mv` | CH-B 2 次谐波 Y (V→mV) |
| 6400 ~ 6799 | AUXADC1 | `aux_adc1_v` | 辅助输入 1 |
| 6800 ~ 7199 | AUXADC2 | `aux_adc2_v` | 辅助输入 2 |
| 7200 ~ 7599 | AUXADC3 | `aux_adc3_v` | 辅助输入 3 |
| 7600 ~ 7999 | AUXADC4 | `aux_adc4_v` | 辅助输入 4 |

列名规范：`{source}_{quantity}_{unit}`，详见 `oe1022d_data_schema_design.md`。

> 来源：手册 Section 5.2.11, "RALL？指令是 USB2.0 专用，在 RS232 接口没有该指令。返回数据长度是 12288Bytes……前 8000Bytes 是返回测量数据，然后的 1216Bytes 是当前配置信息……最后的 3072Bytes 是空字符"

#### 8000~9215 bytes：设备配置快照

包含灵敏度、时间常数、滤波器设置、采样配置等，详见手册完整偏移表。关键偏移：

| 字节偏移 | 配置项 | 格式 |
|----------|--------|------|
| 8390 | A-Sensitivity | uint8 |
| 8391 | A-Reserve | uint8 |
| 8404 | A-Time Constant | uint8 |
| 8405 | A-Filter dB/oct | uint8 |
| 8406 | A-Synchronous | uint8 |
| 8441~8448 | A-Sample Time | float64 |
| 8449~8456 | A-Sample Length | int64 |
| 8462 | A-Sample Mode | uint8 |
| 8479 | A-Input Overload | uint8 |
| 8480 | A-Gain Overload | uint8 |
| 8481 | A-PLL Locked | uint8 |

#### 9216~12287 bytes：填充

空字符，无实际数据。

### 2.3 采集流程

```
┌─────────────┐     ┌──────────────┐     ┌──────────────┐
│  连接设备    │────>│  发送 RALL?  │────>│ 读取 12288B  │
│  *IDND? 验证 │     │  每 50ms 一次 │     │  解析 float64│
└─────────────┘     └──────────────┘     └──────┬───────┘
                                                │
                    ┌──────────────┐     ┌───────▼───────┐
                    │  写入 Parquet│<────│  拼接 + 转换单位│
                    │  + columns   │     │  50点/参数/批  │
                    └──────────────┘     └───────────────┘
```

---

## 3. 数据存储格式

采集脚本输出三个文件：

```
experiment_20260511_143000/
├── data.parquet      # 主数据（Parquet + Snappy 压缩）
├── columns.json      # 列定义 + ML 标签
└── metadata.json     # 采集配置元数据
```

### 3.1 Parquet 列名

格式：`{source}_{quantity}_{unit}`

| 列名 | 单位 | 来源 |
|------|------|------|
| `lockin_A_X_mv` | mV | CH-A 同相 |
| `lockin_A_Y_mv` | mV | CH-A 正交 |
| `lockin_A_freq_hz` | Hz | CH-A 频率 |
| `lockin_A_noise_mv` | mV | CH-A 噪声 |
| `lockin_A_Xh1_mv` ~ `lockin_B_Yh2_mv` | mV | 谐波 |
| `aux_adc1_v` ~ `aux_adc4_v` | V | 辅助 ADC |
| `sample_index` | — | 全局序号 |
| `time_s` | s | 单调时间戳 |
| `batch_index` | — | RALL? 批次号 |

### 3.2 columns.json 示例

每列包含 ML 训练所需的结构化标签：

```json
{
  "version": "1.0",
  "columns": {
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
    }
  }
}
```

完整规范见 `oe1022d_data_schema_design.md`。

---

## 4. 数据读取

### Python

```python
import pandas as pd
import json

# 读取数据
df = pd.read_parquet("experiment/data.parquet")

# 画 CH-A 的 X vs Y（Lissajous 图）
df.plot("lockin_A_X_mv", "lockin_A_Y_mv", kind="scatter", s=1)

# 按来源批量筛选
ch_a_cols = [c for c in df.columns if c.startswith("lockin_A_")]
df[ch_a_cols].plot(subplots=True)

# ML 特征选择
meta = json.load(open("experiment/columns.json"))
feature_cols = [col for col, info in meta["columns"].items() if info["role"] == "feature"]
X = df[feature_cols]
```

### MATLAB (R2019a+)

```matlab
% 读取 Parquet
T = parquetread("experiment/data.parquet");

% 画图
scatter(T.lockin_A_X_mv, T.lockin_A_Y_mv, 1);
```

---

## 5. 采集前优化

手册 Section 5.2.7 推荐在长期采集前执行自动校准，以获得最佳信号质量：

```python
# 自动增益 — 调整灵敏度使信号处于最佳量程
ser.write(b"AGAND 1\r")    # CH-A
time.sleep(2)  # 自动增益需要时间

# 自动动态储备 — 平衡噪声抑制与过载裕度
ser.write(b"ARSVD 1\r")
time.sleep(2)

# 自动相位 — 将参考相位锁定到信号
ser.write(b"APHSD 1\r")
time.sleep(2)

# 验证相位是否锁定
ser.write(b"PHASD? 1\r")
```

### 5.1 时间常数与滤波器选择

时间常数 (`OFLTD`) 决定低通滤波器的截止频率，直接影响噪声带宽和响应速度：

| j | 时间常数 | 适用场景 |
|---|----------|----------|
| 4 | 1 ms | 快速响应，噪声较大 |
| 5 | 3 ms | |
| 6 | 10 ms | 通用选择 |
| 7 | 30 ms | |
| 8 | 100 ms | 低噪声测量 |
| 9 | 300 ms | |
| 10 | 1 s | 极低噪声 |

```python
# 设置时间常数为 10ms (j=6)
ser.write(b"OFLTD 1,6\r")

# 设置滤波器滚降为 18dB/oct (j=2)
ser.write(b"OFSLD 1,2\r")
```

### 5.2 同步滤波器

当参考频率低于 200Hz 时，手册建议启用同步滤波器以抑制偶次谐波：

```python
ser.write(b"SYNCD 1,1\r")  # 开启
```

### 5.3 陷波滤波器

抑制工频干扰：

```python
ser.write(b"ILIND 1,1\r")   # 50Hz 陷波
ser.write(b"ILIND 1,2\r")   # 50Hz + 100Hz 陷波
ser.write(b"ILIND 1,3\r")   # 100Hz 陷波
```

---

## 6. 备选方案：循环缓冲采样

当使用 RS-232 接口（不支持 `RALL?`）时，可使用手册 Section 5.2.8 描述的缓冲采样系统。

### 6.1 配置流程

```python
# 1. 清空缓冲区
ser.write(b"RESTD 3\r")

# 2. 设置采样参数
ser.write(b"SRATD 1,1\r")       # CH-A 采样步进 1ms
ser.write(b"SLEND 1,16384\r")   # 采样长度 16384（最大值）

# 3. 配置缓冲区内容
ser.write(b"SSLED 1,1,1\r")     # Buffer1 = X
ser.write(b"SSLED 1,2,2\r")     # Buffer2 = Y
ser.write(b"SSLED 1,3,0\r")     # Buffer3 = R
ser.write(b"SSLED 1,4,3\r")     # Buffer4 = theta

# 4. 触发与模式
ser.write(b"STRGD 1,0\r")       # 内部触发
ser.write(b"SPRMD 1,1\r")       # 循环模式（Loop）

# 5. 开始采样
ser.write(b"STRDD 1\r")
```

### 6.2 数据读取

```python
# 查询已采集点数
ser.write(b"SPTSD? 1\r")

# 读取缓冲区数据
ser.write(b"TRCAD? 1,1,0,1000\r")  # CH-A, Buffer1(X), 从第0点开始, 读1000点
```

### 6.3 限制

- 最大 16384 点/缓冲区，1ms 间隔下仅 **~16.4 秒**
- 4 个缓冲区可同时记录不同参数（X, Y, R, theta）
- 循环模式下缓冲区满后自动从头覆盖，必须在一圈内读出
- ASCII 传输比二进制慢，高速采样时可能来不及读出

---

## 7. 使用采集脚本

```bash
# 模拟模式（无需设备）
python test_rall_save.py --simulate

# 连接真实设备，采集 10 秒
python test_rall_save.py --port COM3

# 采集 60 秒，指定输出目录
python test_rall_save.py --port COM3 --batches 1200 --output my_experiment
```

输出目录结构：
```
captures_20260511_143000/
├── data.parquet      # 主数据
├── columns.json      # 列定义
└── metadata.json     # 元数据
```

---

## 8. 注意事项

1. **USB 驱动**：首次使用需安装 OE1022D 随附光盘中的 USB 驱动（FT232 驱动），详见手册 Section 6.1
2. **RALL? 仅限 USB**：RS-232 接口发送 `RALL?` 不会有响应
3. **读取时序**：`RALL?` 每 50ms 刷新一次，发送频率应匹配。过快发送会收到重复数据，过慢会丢批
4. **数据单位**：设备原始单位为 V（伏特）和 Hz，存储时 V 转为 mV 以避免 1e-9 级小数
5. **长时间采集的存储**：1kHz x 20 参数 x 8 bytes = 160 KB/s，1 小时约 576 MB 原始数据，Parquet + Snappy 压缩后约 300 MB
6. **缓冲区溢出**：`RALL?` 不存在缓冲区溢出问题（设备端每 50ms 刷新），但如果 Python 端处理太慢导致积压，串口缓冲区可能溢出。建议在采集循环中不做耗时计算
