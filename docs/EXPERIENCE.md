# ODMR Control 经验文档

> 版本: V2.0  
> 日期: 2026-05-12

---

## 目录

1. [架构设计决策记录 (ADR)](#1-架构设计决策记录-adr)
2. [踩坑记录](#2-踩坑记录)
3. [性能优化经验](#3-性能优化经验)
4. [调试技巧](#4-调试技巧)
5. [代码审查记录](#5-代码审查记录)
6. [开发规范](#6-开发规范)

---

## 1. 架构设计决策记录 (ADR)

### ADR-001: 引入命令模式 (Command Pattern)

**背景**: V1 中 GUI 直接调用 `InstrumentController` 方法，无法支持 AI Agent 操作。

**决策**: 引入 `Command` + `CommandType` + `CommandService` 三层命令架构。

**理由**:
- 命令可序列化（JSON），AI Agent 可用 JSON 生成命令
- 统一入口便于安全校验、日志、审计
- 单线程串行执行天然避免并发冲突

**代价**:
- 增加一层抽象，调试时需要追踪 `request_id`
- GUI 代码中大量 `if self._cmd_service is not None else ...` 导致膨胀

**状态**: ✅ 已实施，GUI 和 Agent 共用同一接口

---

### ADR-002: 单线程串行执行 vs 多线程并发

**背景**: VISA/Serial 设备不支持并发访问。

**决策**: `CommandService` 使用单线程队列串行执行所有命令。

**替代方案**: 为每个设备分配独立线程（SMB 一个线程、Lockin 一个线程）。

**拒绝理由**:
- 扫频期间需要 SMB 和 Lockin 协调（SMB 改频率时 Lockin 不能读 RALL?）
- 单线程逻辑更简单，避免死锁

**状态**: ✅ 已实施

---

### ADR-003: Worker 直接访问 Driver vs 通过 CommandService

**背景**: Worker（SMBPoll / LockinMonitor / LockinAcquire）是只读轮询任务。

**决策**: Worker 直接访问 Driver，不经过 CommandService。

**理由**:
- Worker 的查询频率高（50~100ms），走 CommandService 会增加延迟
- 只读操作不会与用户命令冲突（Driver 已加锁）
- 保持实时性

**状态**: ✅ 已实施

---

### ADR-004: 信号三级广播

**背景**: 需要解耦 GUI 和底层设备。

**决策**: `Driver → Controller → CommandService → GUI` 三级信号。

**理由**:
- CommandService 可拦截和转换信号格式
- GUI 不直接依赖 Controller，未来可替换底层实现
- 状态指示灯和数据显示使用同一数据源

**状态**: ✅ 已实施

---

### ADR-005: 时间戳同步策略

**背景**: SMB 频率变化时刻、Lockin RALL? batch、未来磁场数据需要对齐。

**决策**: 以主机 `time.monotonic()` 为统一时间轴。

**理由**:
- `time.monotonic()` 不受系统时间调整影响
- 精度足够（ms 级）
- 简单易实现

**替代方案**: NTP 同步、PTP 硬件时间戳。

**拒绝理由**: 实验设备在同一台 PC 上，NTP/PTP 过于复杂。

**状态**: ✅ 基础实现完成，`query_aligned()` 预留待实现

---

### ADR-006: Parquet 作为数据格式

**背景**: 需要支持大容量高速采集（20 参数 × 1kHz = 20k 值/秒）。

**决策**: Parquet + Snappy 压缩。

**理由**:
- 列式存储，查询效率高
- Snappy 压缩比好，解压快
- 与 pandas/pyarrow 生态兼容，便于 ML 下游

**状态**: ✅ 已实施

---

## 2. 踩坑记录

### 🕳️ 坑 1: Qt moveToThread 禁止 parent 对象跨线程

**现象**: 扫频时 GUI 卡死，报错 "Cannot move objects with a parent"。

**原因**: `SweepEngine` 本身有 parent（GUI），如果直接 `moveToThread` 会失败。

**解决**: 创建一个**无 parent** 的内部类 `_SweepRunner`，只把 Runner moveToThread：

```python
class _SweepRunner(QObject):
    def __init__(self, ctrl, sequence, recorder=None):
        super().__init__(None)  # 关键：无 parent！
```

**经验**: 任何需要 moveToThread 的对象，都必须是无 parent 的。

---

### 🕳️ 坑 2: pyvisa `list_resources()` 会挂起在无效资源上

**现象**: 自动扫描时程序卡死 30 秒以上。

**原因**: `ResourceManager().list_resources()` 返回的所有资源中，有些是无效的（如已断开但未释放的 USB 设备），`query("*IDN?")` 会长时间等待。

**解决**: 设置短超时（500ms），并在 finally 中关闭资源：

```python
try:
    instr = rm.open_resource(addr)
    instr.timeout = 500  # 短超时
    idn = instr.query("*IDN?")
    # ...
finally:
    try:
        instr.close()
    except:
        pass
```

**经验**: 枚举 VISA 资源时务必设置短超时，并做好资源释放。

---

### 🕳️ 坑 3: pyqtgraph 实时波形刷新导致 GUI 卡顿

**现象**: 波形图刷新时 GUI 响应变慢。

**原因**: 每次刷新都重绘所有 2000 个数据点。

**解决**: 使用 `downsample=4` 降采样，只绘制 500 个点：

```python
ts_arr, vals = self._buffer.get(ch, max_points=500, downsample=4)
```

**经验**: 实时波形显示必须做降采样，人眼无法分辨 2000 个点的细节。

---

### 🕳️ 坑 4: RALL? 数据不完整导致 numpy 解析失败

**现象**: 偶尔出现 `ValueError: buffer is too small for requested array`。

**原因**: USB 传输中偶尔丢失数据，`read_exact(12288)` 返回的数据不足。

**解决**: 增加长度校验，不完整的数据丢弃不计入统计：

```python
if len(raw) != RALL_TOTAL_BYTES:
    self._dropped += 1
    continue
```

**经验**: 高速串口通信必须有数据完整性校验，不能假设每次传输都完美。

---

### 🕳️ 坑 5: `threading.Event.wait()` 在 Qt GUI 线程中阻塞事件循环

**现象**: 调用 `submit_sync()` 后 GUI 冻结。

**原因**: `submit_sync()` 内部使用 `threading.Event.wait()`，这在 Qt 的 GUI 线程中会阻塞事件循环。

**解决**: GUI 中只使用 `submit()` 异步提交，通过信号回调处理结果。`submit_sync()` 仅用于 Agent 的后台线程。

**经验**: Python 的 `threading` 原语与 Qt 的事件循环不兼容，GUI 线程中禁用阻塞等待。

---

### 🕳️ 坑 6: OE1022D `*IDND?` 响应格式不统一

**现象**: 串口扫描时有时识别不出 OE1022D。

**原因**: 不同固件版本的 `*IDND?` 响应格式有细微差异（如逗号后是否有空格）。

**解决**: 使用 `in` 子串匹配而非精确匹配：

```python
if "SSI LIA-OE1022D" in resp:
    idn = resp
```

**经验**: 仪器 IDN 字符串匹配应使用子串包含而非精确相等。

---

### 🕳️ 坑 7: Parquet Writer 未正确关闭导致文件损坏

**现象**: 采集结束后 Parquet 文件无法读取。

**原因**: 程序异常退出时 `ParquetWriter` 未调用 `close()`。

**解决**: 在 `LockinAcquireWorker.run()` 的 `finally` 块中确保 recorder 关闭：

```python
finally:
    if self._recorder is not None and self._recorder.is_recording:
        try:
            self._recorder.stop_recording()
        except Exception as exc:
            self.error_occurred.emit(f"[Lockin] Recorder stop failed: {exc}")
```

**经验**: 文件写入操作必须在 `finally` 中确保关闭，特别是在多线程环境中。

---

### 🕳️ 坑 8: 功率限制在前端和后端不一致

**现象**: GUI 显示限制 10dBm，但直接调用 driver 仍可设置 20dBm。

**原因**: V1 中功率限制只在 GUI 层校验，driver 层没有限制。

**解决**: 将功率限制上移到 CommandService 层，确保 GUI 和 Agent 都无法绕过：

```python
def _check_power_limit(self, power_dbm: float) -> None:
    max_dbm = 10.0 if config.amplifier_installed else 25.0
    if power_dbm > max_dbm:
        raise SafetyError(f"功率 {power_dbm} dBm 超过安全限制 {max_dbm} dBm")
```

**经验**: 安全校验必须在**最接近操作**的统一入口层执行，不能仅靠前端。

---

## 3. 性能优化经验

### 3.1 波形显示降采样

| 策略 | 刷新前 | 刷新后 | 效果 |
|------|--------|--------|------|
| 无降采样 | 2000 点/曲线 × 3 曲线 = 6000 点 | 同左 | GUI 卡顿 |
| downsample=4 | 500 点/曲线 × 3 曲线 = 1500 点 | 同左 | 流畅 |

人眼无法分辨 500 个数据点的细节，降采样对显示质量无影响。

### 3.2 环形缓冲区替代列表

使用 `collections.deque(maxlen=capacity)` 替代 Python list：
- 追加操作 O(1)
- 自动丢弃旧数据，无需手动清理
- 内存占用恒定

### 3.3 numpy 批量解析 RALL? 数据

```python
samples = np.frombuffer(raw, dtype="<f8", count=50, offset=offset)
```

比逐字节解析快 100 倍以上。

### 3.4 状态缓存减少 VISA 查询

SMB100ADriver 缓存 `cached_freq_hz`, `cached_power_dbm` 等状态：
- 设置时更新缓存
- 查询时优先返回缓存
- 减少不必要的 VISA 往返

---

## 4. 调试技巧

### 4.1 原始 SCPI/Serial 日志

两个 driver 都支持 `set_raw_log_callback(cb)`：

```python
def raw_log(direction: str, data: bytes):
    print(f"[{direction}] {data!r}")

controller.set_smb_raw_log_callback(raw_log)
controller.set_lockin_raw_log_callback(raw_log)
```

### 4.2 模拟设备（无需硬件）

`tests/test_instruments.py` 中提供了 FakeVISA 和 FakeSerial：

```python
class FakeVISA:
    def query(self, cmd):
        if cmd == "*IDN?":
            return "Rohde&Schwarz,SMB100A,123456,2.1.0"
        # ...
```

### 4.3 命令追踪

CommandService 的 `log_requested` 信号记录所有命令执行：

```python
cmd_service.log_requested.connect(print)
```

### 4.4 性能分析

```python
import time

t0 = time.monotonic()
# 执行操作
dt = time.monotonic() - t0
print(f"耗时: {dt*1000:.1f}ms")
```

### 4.5 线程状态检查

```python
# 检查 Worker 是否仍在运行
controller.is_any_worker_running()

# 检查 CommandService 队列长度
cmd_service._cmd_queue.qsize()
```

---

## 5. 代码审查记录

### Round 1: 关键 Bug 修复

| 编号 | 问题 | 位置 | 修复 |
|------|------|------|------|
| C1 | `_SweepRunner` 有 parent 导致 moveToThread 失败 | `sweep_engine.py` | `super().__init__(None)` |
| C2 | 多线程 VISA/Serial 竞争 | `instruments/*.py` | 全 I/O 加锁 (`threading.Lock`/`RLock`) |
| C3 | 僵尸 Worker 线程 | `core/instrument_controller.py` | `terminate()` 兜底 |
| C4 | 扫频期间 SMBPollWorker 并发读频率 | `core/instrument_controller.py` | 扫频期间暂停 SMB 轮询 |
| C5 | Parquet 线程不安全 | `data/recorder.py` | 增加 `threading.Lock` |
| C6 | 扫频超时硬编码 | `core/sweep_engine.py` | 动态计算 `点数 × 驻留 + 30s` |

### Round 2: V2 功能增强审查

| 编号 | 问题 | 严重程度 | 修复 |
|------|------|----------|------|
| R1 | `_emergency_stop()` 直接调用 `_ctrl`，绕过 CommandService | 🔴 高 | 改为通过 CommandService 提交 `SYS_EMERGENCY_STOP` |
| R2 | `SMBPollWorker` 直接访问 `_driver._query()` 私有方法 | 🔴 高 | 在 driver 中新增公开方法 `get_lf_output()` 等 |
| R3 | `_on_lockin_status_changed()` 指示灯逻辑 bug (`"true" if ov else "true"`) | 🔴 高 | 修正为 `"true" if ov else "false"` |
| R4 | `SMB_SET_MODULATION` 在 CommandService 中未实现 | 🔴 高 | 添加处理逻辑 |
| R5 | `CommandService._execute()` if-elif 链过长 | 🟡 中 | 记录 TODO，未来改用字典映射 |
| R6 | `SweepEngine._runner._stop_requested` 直接访问私有属性 | 🟡 中 | 记录 TODO，未来添加公开方法 |
| R7 | `_on_lockin_data_ready()` 中 `channel` 字段处理 | 🟡 中 | 已修正，按 channel 路由到对应显示 |
| R8 | 配置文件中 `lockin_bindings` 未持久化保存 | 🟡 中 | 记录 TODO |
| R9 | 单元测试覆盖不足 | 🟢 低 | 记录 TODO |
| R10 | `CircularBuffer.extend()` 多通道长度不一致风险 | 🟢 低 | 记录 TODO |

---

## 6. 开发规范

### 6.1 代码风格

- 遵循 PEP 8
- 类型注解：所有公共方法必须有类型注解
- 文档字符串：所有公共类和方法必须有 docstring

### 6.2 命名规范

| 类型 | 规范 | 示例 |
|------|------|------|
| 类 | PascalCase | `SMB100ADriver` |
| 方法/函数 | snake_case | `set_frequency()` |
| 常量 | UPPER_SNAKE_CASE | `RALL_TOTAL_BYTES` |
| 私有方法 | _snake_case | `_query()` |
| 列名 | {source}_{quantity}_{unit} | `lockin_A_X_mv` |

### 6.3 安全规范

1. 所有安全校验必须在 **CommandService 层** 执行
2. 功率限制：`amplifier_installed=true` 时 max 10dBm，否则 25dBm
3. 频率限制：100kHz ~ 12.75GHz
4. 急停必须关闭所有输出并切回 CW
5. 文件写入必须在 `finally` 中确保关闭

### 6.4 测试规范

1. 所有 driver 方法必须有单元测试（使用 FakeVISA/FakeSerial）
2. GUI 测试：至少验证导入成功、无语法错误
3. 集成测试：需要真实硬件

### 6.5 提交规范

```
[type]: [short description]

[body]

Refs: #[issue number]
```

类型：`feat`, `fix`, `refactor`, `docs`, `test`, `chore`
