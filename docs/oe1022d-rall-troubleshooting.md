# OE1022D RALL? 高速采集问题与优化记录

> 记录 OE1022D DSP Lock-In Amplifier 通过 `RALL?` 指令进行高速实时波形采集过程中遇到的关键问题、根因分析与解决方案。供后续维护和升级参考。

---

## 1. RALL? 启动失败：`PermissionError(13, '设备不识别此命令。')`

### 现象
在 RS232 连接下调用 `start_rall_stream()`（发送 `RALL?\r`）时，设备返回 `PermissionError(13)`，提示"设备不识别此命令"。

### 根因
`RALL?` 是 **USB2.0 专属高速指令**，设备固件在 RS232 模式下会拒绝该命令。OE1022D 的串口存在两种传输层：
- **RS232**：标准 UART，支持常规 ASCII 指令（如 `SNAPD?`）
- **USB2.0**：通过 STM32 USB CDC（VID:PID = `0483:5740`）虚拟串口连接，支持 `RALL?` 二进制批量传输

### 解决
1. **物理连接**：使用 USB 线连接设备背面的 USB 口到 PC（COM8 等）
2. **设备设置**：前面板 `[SYSTEM]` → `[Remote]` → 选择 **USB**
3. **代码确认**：`OE1022DDriver` 初始化时检查 `transport == "usb2"`，`rall_supported` 属性为 `True` 时才允许启动采集

---

## 2. 波形刷新率极低：~1 Hz（预期 ~20 Hz）

### 现象
开启 RALL? 采集后，波形界面刷新极其卡顿，状态栏显示 `batch_out: ~0.9 Hz`。

### 根因
`LockinMonitorWorker`（使用 `SNAPD?` ASCII 轮询）和 `LockinAcquireWorker`（使用 `RALL?` 二进制批量读取）**共享同一个 `OE1022DDriver` 实例和内部的 `threading.RLock`**。

当两者同时运行时：
- `SNAPD?` 每 ~94ms 发送一次 ASCII 查询
- `RALL?` 每 ~50ms 发送一次二进制查询
- 两个 worker 的命令在串口上**交错发送**，导致设备响应混乱
- 驱动层的 `read_exact()` 因数据长度不匹配而反复超时（默认超时 1s），严重拖慢整体吞吐

### 解决
在 `InstrumentController` 中实现 **worker 生命周期级别的互斥**：
- `start_lockin_acquire()` 中**先停止 monitor**，再启动 acquire
- `stop_lockin_acquire()` 和 `_on_acquire_finished()` 中**恢复 monitor**

```python
def start_lockin_acquire(self, recorder=None):
    if not self._lockin.rall_supported:
        raise RuntimeError("RALL? requires USB2.0")
    self._stop_lockin_monitor()   # 关键：避免串口竞争
    # ... 启动 LockinAcquireWorker

def _stop_lockin_acquire(self):
    # ... 停止 worker
    self._start_lockin_monitor()  # 关键：恢复实时监视
```

修复后 `batch_out` 恢复至 **~20 Hz**（50ms 间隔）。

---

## 3. Numpy Overflow Warning：`RuntimeWarning: overflow encountered in square`

### 现象
计算 `R = sqrt(X^2 + Y^2)` 时，终端不断打印 numpy overflow warning。

### 根因
数据本身为正常的 `float64`（~1e-4 量级），但某些 numpy 版本在处理 `x**2` 时存在**内部类型提升的误报**（与具体 numpy 构建和 CPU 指令集有关），并非真正的数值溢出。

### 解决
在计算 R 和 theta 的代码块外包裹 `np.errstate`：

```python
with np.errstate(over="ignore"):
    r = np.sqrt(x**2 + y**2)
    theta = np.degrees(np.arctan2(y, x))
```

---

## 4. 波形存在 batch 间锯齿/折线感

### 现象
波形整体流畅，但 batch 与 batch 的连接处（约每 50ms）出现明显的"折角"或锯齿感（见下图绿色框标注）。LabVIEW 配套软件无此现象。

### 根因
**时间戳在 GUI 主线程生成，受主线程调度抖动影响。**

原始代码：
```python
ts = time.monotonic()  # GUI 收到 batch 的时间
dt = 0.05 / n
timestamps = np.arange(n) * dt + ts
```

问题在于：
1. `time.monotonic()` 是在 `_on_lockin_batch_ready`（主线程槽函数）中调用的
2. Qt 信号从 worker 线程投递到主线程存在**队列延迟**。当主线程忙于渲染时，batch 可能排队等待数毫秒至数十毫秒
3. 不同 batch 的 `ts` 间隔不是精确的 50ms，而是 `50ms + 抖动`。当抖动为 +5ms 时，batch 间出现 5ms 的时间间隙；pyqtgraph 用直线连接跨越间隙的两点，形成可见折角

### 为什么 LabVIEW 没有这个问题？
LabVIEW 配套软件很可能采用了以下一种或多种策略：
1. **采集端时间戳**：在底层采集线程（FPGA 或驱动层）就给数据打上基于硬件/循环时钟的时间戳，不依赖 UI 线程
2. **理想化时间基**：以固定 50ms 间隔生成时间轴，不测量实际到达时间，仅按 batch 序号排列
3. **更大的显示缓冲**：累积多批数据后统一做样条插值或平滑滤波再渲染
4. **图形抗锯齿**：LabVIEW 的绘图控件默认开启子像素抗锯齿，轻微的不连续会被模糊化

### 解决
**改用基于 `batch_count` 的理想连续时间戳**，彻底消除 GUI 线程抖动：

```python
stats = batch.get("acquisition_stats", {})
batch_count = int(stats.get("batch_count", 0)) if isinstance(stats, dict) else 0
base_ts = batch_count * 0.050   # 每批严格 50ms
dt = 0.050 / n                  # 1ms per sample
timestamps = np.arange(n, dtype=np.float64) * dt + base_ts
```

这样无论 GUI 主线程多忙，batch 之间的时间戳始终精确连续，batch 间隙恒定为采样间隔（1ms），视觉上完全平滑。

> **注意**：此方法假设 worker 的循环间隔为理想 50ms。若 worker 偶发延迟（如 `time.sleep` 精度不足），时间戳会与真实时间有轻微偏差，但对实时波形显示无影响。

---

## 5. 性能优化汇总

| 优化项 | 修改前 | 修改后 | 关键改动 |
|--------|--------|--------|----------|
| 缓冲区 | `deque` + 逐点 Python 循环 | 预分配 `numpy` ring buffer | `data/circular_buffer.py` 重写，`extend()` 用 numpy slice 批量写入 |
| 绘图项 | `PlotDataItem` | `PlotCurveItem` | 减少 pyqtgraph 内部开销 |
| 渲染开销 | 每帧全量 `setData` + legend 更新 | 无 legend，仅 `curve.setData` | 移除 legend，减少对象数量 |
| 时间戳 | `datetime.now().timestamp()` | `time.monotonic()` → `batch_count * 0.05` | 避免 wall clock 跳变和 GUI 抖动 |
| 刷新率控制 | 硬编码 50ms | 可选 16/33/50/100ms | 添加 `_wave_refresh_rate` 下拉框 |
| 时间窗口 | 固定 2s | 可选 1/2/5/10/30s | 添加 `_wave_time_window` 下拉框 |
| Y 轴缩放 | 每帧计算 | 每 10 帧计算 | 降低 `Auto Y` 开销 |

---

## 6. 关键设计约束（RALL? 协议）

- **总字节数**：`12288 bytes/batch`
- **样本数**：`50 samples/batch`
- **参数通道数**：`20 params`
- **数据区**：前 `8000 bytes`（20 × 50 × 8 bytes float64）
- **配置快照区**：后续字节包含设备当前配置（滤波器、增益等）
- **速率**：20 batches/second（50ms 间隔）
- **读取延迟**：发送 `RALL?` 后约 **14ms** 数据返回
- **查询模式**：非流式。每批需重新发送 `RALL?\r`，设备不会主动持续推送

---

## 7. 相关文件

| 文件 | 职责 |
|------|------|
| `instruments/oe1022d.py` | OE1022D 驱动，`RALL?` 发送/解析 |
| `workers/lockin_acquire_worker.py` | RALL? 采集 worker，50ms 循环 |
| `workers/lockin_monitor_worker.py` | SNAPD? 监视 worker，~94ms 循环 |
| `core/instrument_controller.py` | worker 生命周期管理，互斥逻辑 |
| `data/circular_buffer.py` | 高性能环形缓冲区 |
| `app/gui.py` | 波形页面渲染，时间戳生成，控制面板 |

---

## 5. 波形每 5 秒出现一次锯齿/折角

### 现象
波形整体流畅，但约每 5 秒出现一次明显的 batch 间折角（斜率突变）。

### 根因
`CircularBuffer(capacity=5000)` 每 5 秒（100 batch × 50 点）发生一次 **ring buffer wrap-around**。当 `_write_idx` 回到 0 时，最旧的一个完整 batch（50 点）被新数据完全覆盖。

在 `get()` 返回的时间序列中，被覆盖 batch 的前一个 batch 末尾和下一个 batch 开头之间出现 **~50ms 的时间跳跃**。pyqtgraph 的 `PlotCurveItem` 用直线连接跨越 50ms 间隙的两点，形成可见折角。

### 解决
双管齐下：
1. **增大 buffer capacity** 到 `60000`（60 秒），将 wrap-around 频率从每 5 秒一次降低到每 60 秒一次
2. **断裂时间跳跃**：在 `_on_display_tick` 中检测 `np.diff(ts_arr) < -0.003`，在间隙处插入 `np.nan`，pyqtgraph 自动断裂曲线而不连接跨越间隙的点

```python
# app/gui.py _on_display_tick
gaps = np.diff(ts_arr) < -0.003  # 正常间隔约 -1ms
if np.any(gaps):
    gap_indices = np.where(gaps)[0] + 1
    ts_arr = np.insert(ts_arr, gap_indices, np.nan)
    vals = np.insert(vals, gap_indices, np.nan)
curve.setData(ts_arr, vals)
```

---

## 6. 多设备时间戳对不齐（SMB100A + 磁场 + 锁相）

### 现象
CSV 中 SMB 频率、磁场、激光状态与锁相读数的时间戳存在偏差，做关联分析时无法精确对齐。

### 根因
1. **时间戳在 GUI 端生成**：`_on_lockin_batch_ready` 使用 `time.monotonic()` 或 `batch_count * 0.05`，反映的是 GUI 收到 batch 的时间，而非采集时间
2. **设备状态在读取后获取**：`LockinAcquireWorker` 在 `read_rall_batch()` **之后**才读取 SMB/激光/磁场状态。RALL? 返回的是过去 50ms 的数据，但设备状态是"当前"状态，两者有 ~25ms 平均偏差 + 读取耗时偏差
3. **CSV time_s 是写入时间**：`recorder.write_batch()` 使用 `time.monotonic() - start_time`，是写入 CSV 的时刻，不是数据采集时刻

### 行业最佳实践
大多数实验室自动化系统（QCoDeS、PyMeasure、LabVIEW DAQmx）采用 **"软件时间戳 + 后处理对齐"**：
- 所有数据在**采集端**（而非 GUI 端）打上时间戳
- 设备状态变化记录为**事件日志（event log）**
- 分析阶段将异步数据插值到统一时间网格
- 只有需要亚毫秒级精度时才使用硬件触发（如 SMB100A List Mode + 外部 TTL）

### 解决

#### 6.1 采集端统一时间基准
在 `LockinAcquireWorker.run()` 中，用 `t_start`（循环开始时间）作为 batch 的基准时间戳：

```python
# workers/lockin_acquire_worker.py
t_start = time.monotonic()
# ... 发送 RALL? 并读取 ...
batch["acquisition_stats"] = {
    "batch_timestamp_s": t_start,  # 采集端统一时间基准
    # ...
}
```

batch 内各点时间戳 = `t_start + np.linspace(0, 0.05, n, endpoint=False)`，精确反映采集时间。

#### 6.2 设备状态快照前置
在发送 `RALL?` **之前**快照设备状态，随 batch 一起传递：

```python
with self._state_lock:
    state_snapshot = {
        "smb_freq_hz": self._smb_freq_hz,
        "smb_power_dbm": self._smb_power_dbm,
        # ...
    }
# ... 读取 RALL? ...
batch["state_snapshot"] = state_snapshot
```

这样每个 batch 携带的是"采集开始时"的设备状态，时间偏差从 ~50ms 降至 <1ms。

#### 6.3 Recorder 使用采集端时间戳
```python
# data/recorder.py
def write_batch(self, rall_data, batch_timestamp_s=0.0, ...):
    if batch_timestamp_s > 0:
        t_batch = batch_timestamp_s - self._start_time
    else:
        t_batch = time.monotonic() - self._start_time
    # 每行 time_s = t_batch + i * 0.001
```

#### 6.4 设备事件日志
在 `InstrumentController` 中维护 `_event_log`，每次 SMB/激光/磁场状态变化时追加记录：

```json
{"timestamp": 12345.678, "device": "smb", "param": "freq_hz", "value": 2870000000}
{"timestamp": 12346.234, "device": "mag_X", "param": "target_field_nT", "value": 1500}
```

采集结束时写入 `output_dir/events.jsonl`，供后续分析做插值对齐。

---

## 7. 性能优化汇总（完整版）

| 优化项 | 修改前 | 修改后 | 关键改动 |
|--------|--------|--------|----------|
| 缓冲区容量 | 5000 点（5s） | 60000 点（60s） | 消除 wrap-around 锯齿 |
| 曲线断裂 | 无 | 间隙处插入 `np.nan` | `app/gui.py` `_on_display_tick` |
| 缓冲区结构 | `deque` + 逐点 Python 循环 | 预分配 `numpy` ring buffer | `data/circular_buffer.py` 重写 |
| 绘图项 | `PlotDataItem` | `PlotCurveItem` | 减少 pyqtgraph 内部开销 |
| 时间戳来源 | GUI 端 `time.monotonic()` | 采集端 `t_start` | `workers/lockin_acquire_worker.py` |
| 设备状态获取 | 读取 RALL? 之后 | 发送 RALL? 之前快照 | `workers/lockin_acquire_worker.py` |
| CSV time_s | 写入时间 | `batch_timestamp_s + i*0.001` | `data/recorder.py` |
| 事件日志 | 无 | `events.jsonl` | `core/instrument_controller.py` |
| 刷新率控制 | 硬编码 50ms | 可选 16/33/50/100ms | `app/gui.py` 下拉框 |
| 时间窗口 | 固定 2s | 可选 1/2/5/10/30s | `app/gui.py` 下拉框 |
| Y 轴缩放 | 每帧计算 | 每 10 帧计算 | 降低 `Auto Y` 开销 |

---

## 8. 残余锯齿：`autoDownsample` + `skipFiniteCheck=True`

### 现象
经过 buffer 增大 + nan 断裂 + 采集端时间戳修复后，锯齿频率大幅降低，但仍偶发可见。

### 根因分析（两个叠加因素）

**因素 B：`autoDownsample=True`**
- `PlotCurveItem` 默认开启自动降采样。当数据点超过视图像素宽度时，pyqtgraph 按固定步长跳过点
- 降采样在 batch 边界处可能产生不连续的采样相位，视觉上形成锯齿

**因素 D：`skipFiniteCheck=True`**
- 为性能优化设置了 `skipFiniteCheck=True`
- 这导致 pyqtgraph **完全忽略 `nan`**，插入的 `np.nan` 断裂曲线根本没有生效
- ring-buffer wrap-around 的时间间隙仍然被直线连接

### 解决
```python
# app/gui.py
curve = pg.PlotCurveItem(
    pen=pg.mkPen(color, width=1.5), name=label,
    autoDownsample=False,   # 关闭自动降采样
    skipFiniteCheck=False,  # 确保 nan 能断裂曲线
)
```

### 仍未完全消除的可能原因
- **Windows `time.sleep` 精度**：默认 timer resolution 15.625ms，`time.sleep(0.036)` 实际间隔波动 ±15ms，batch 间真实间隙 1~20ms
- 对于需要亚毫秒级精度的场景，建议在 worker 启动时调用 `timeBeginPeriod(1)` 提高 timer resolution，或改用 `time.perf_counter()` + busy-wait spin

---

*最后更新：2026-05-27*
