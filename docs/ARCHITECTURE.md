# ODMR Control 架构设计文档

> 版本: V2.0  
> 日期: 2026-05-12  
> 架构风格: 参考 mag-field-control-v2 多层安全架构

---

## 1. 项目概述

ODMR Control 是一个基于 PyQt5 的桌面应用程序，用于控制光探测磁共振（ODMR）实验中的仪器：

- **SMB100A**（Rohde & Schwarz）：微波信号源，产生 100kHz ~ 12.75GHz 的 RF 信号
- **OE1022D**（SSI）：双通道 DSP 锁相放大器，用于微弱信号检测

### 1.1 技术栈

| 组件 | 技术 | 版本 |
|------|------|------|
| GUI 框架 | PyQt5 | ≥5.15 |
| 仪器通信 | pyvisa (SMB100A) + pyserial (OE1022D) | ≥1.13 / ≥3.5 |
| 数据存储 | pyarrow (Parquet) | ≥15.0 |
| 实时波形 | pyqtgraph | ≥0.13 |
| 数值计算 | numpy | ≥1.24 |

### 1.2 设计原则

1. **安全优先**：功率限制、频率范围校验、急停机制
2. **统一命令总线**：GUI 和 AI Agent 共享同一操作通道
3. **线程安全**：所有 I/O 操作加锁，Worker 线程与 GUI 线程分离
4. **向后兼容**：渐进式迁移，新旧架构共存
5. **数据可追溯**：Parquet + JSON 元数据，支持 ML 下游

---

## 2. 分层架构

```
┌─────────────────────────────────────────────────────────────┐
│  Layer 5: 用户界面层 (Presentation)                         │
│  ├─ app/gui.py          主 GUI (PyQt5, Siemens 工业风)     │
│  └─ app/config_io.py    配置读写 (XML/JSON)                 │
├─────────────────────────────────────────────────────────────┤
│  Layer 4: AI Agent 接口层                                   │
│  └─ core/agent_api.py   面向 AI 的高级 Python API           │
├─────────────────────────────────────────────────────────────┤
│  Layer 3: 命令总线层 (Command Bus)                          │
│  └─ core/command_service.py                                 │
│      ├─ 单线程串行执行队列                                   │
│      ├─ 安全校验（功率限制等）                               │
│      └─ 信号广播（Qt + threading）                          │
├─────────────────────────────────────────────────────────────┤
│  Layer 2: 控制门面层 (Facade)                               │
│  └─ core/instrument_controller.py                           │
│      ├─ 管理 SMB100ADriver + OE1022DDriver                  │
│      └─ 管理 3 个 Worker 线程                               │
├─────────────────────────────────────────────────────────────┤
│  Layer 1.5: 业务引擎层                                      │
│  ├─ core/sweep_engine.py    扫频序列引擎                    │
│  └─ core/timestamp_sync.py  多源时间戳同步（预留）          │
├─────────────────────────────────────────────────────────────┤
│  Layer 1: 设备驱动层 (Drivers)                              │
│  ├─ instruments/smb100a.py    VISA/SCPI 驱动               │
│  └─ instruments/oe1022d.py    Serial/ASCII+Binary 驱动     │
├─────────────────────────────────────────────────────────────┤
│  Layer 0: 数据层 (Data)                                     │
│  ├─ data/recorder.py        Parquet 记录器                 │
│  └─ data/circular_buffer.py 环形缓冲区                     │
└─────────────────────────────────────────────────────────────┘
```

---

## 3. 各层详细说明

### 3.1 命令总线层 (Command Bus)

#### 3.1.1 设计动机

在 V1 版本中，GUI 直接调用 `InstrumentController` 的方法。这带来两个问题：
1. **无法支持 AI Agent**：Agent 需要程序化接口，但不能直接操作 GUI 控件
2. **缺乏统一入口**：安全校验、日志、审计散落在各处

V2 引入**命令模式**，所有对设备的操作封装为可序列化的 `Command` 对象，通过 `CommandService` 统一下发。

#### 3.1.2 核心组件

**`core/commands.py`**
- `CommandType` 枚举：40+ 种命令类型
- `Command` frozen dataclass：支持 JSON 序列化

**`core/command_service.py`**
- `submit(cmd)`：异步提交，返回 `request_id`
- `submit_sync(cmd, timeout_ms)`：阻塞同步执行
- `_process_loop()`：在独立 QThread 中单线程处理队列
- `_execute(cmd)`：命令路由分发
- `_check_power_limit()`：功率安全限制

**关键设计决策**：单线程串行执行

```python
def _process_loop(self) -> None:
    while self._running:
        cmd = self._cmd_queue.get(timeout=0.1)
        if cmd is None:
            break
        self._handle_command(cmd)
```

所有 VISA/Serial 命令在同一个线程中排队执行，天然避免并发竞争。实测中，即使 GUI 快速点击多个按钮，设备端也只会按顺序接收命令。

#### 3.1.3 信号广播

```
InstrumentController ──► CommandService ──► GUI
     smb_state_changed    smb_state_broadcast    _on_smb_state_changed
     lockin_data_ready    lockin_data_broadcast  _on_lockin_data_ready
     lockin_batch_ready   lockin_batch_broadcast _on_lockin_batch_ready
```

CommandService 订阅底层的 InstrumentController 信号，重新广播为统一格式的信号。这种分层使 GUI 不直接依赖 InstrumentController，未来可替换底层实现。

### 3.2 控制门面层 (Facade)

**`core/instrument_controller.py`**

门面模式隐藏了底层驱动和 Worker 管理的复杂性。GUI 和 SweepEngine 只需与 `InstrumentController` 交互。

**Worker 管理**：

| Worker | 职责 | 间隔 | 信号 |
|--------|------|------|------|
| SMBPollWorker | 轮询 SMB100A 状态 | 100ms | `smb_state_changed` |
| LockinMonitorWorker | SNAPD? 实时监控 | 94ms | `lockin_data_ready` |
| LockinAcquireWorker | RALL? 高速采集 | 50ms | `lockin_batch_ready` |

**生命周期管理**：

```python
def connect_smb(self, visa_address: str) -> str:
    idn = self._smb.connect(visa_address)
    self._start_smb_poll()  # 自动启动轮询
    return idn
```

连接成功后自动启动对应 Worker，断开时自动停止。

### 3.3 设备驱动层

#### 3.3.1 SMB100ADriver

- **协议**：VISA (USBTMC/GPIB/LAN)
- **安全**：频率 100kHz ~ 12.75GHz，功率 -120 ~ +30dBm
- **状态缓存**：`cached_freq_hz`, `cached_power_dbm` 等（线程锁保护）
- **自动扫描**：类方法 `scan_rs_devices()` 枚举所有 VISA 资源并发送 `*IDN?`

#### 3.3.2 OE1022DDriver

- **协议**：Serial (RS-232 over USB), 921600 baud
- **双模式**：
  - **SNAPD?**：ASCII 查询，返回 X/Y/R/θ，用于实时监控
  - **RALL?**：二进制批量读取，20 参数 × 50 点 = 12288 bytes/50ms
- **自动扫描**：类方法 `scan_ports_with_idn()` 发送 `*IDND?` 验证设备

### 3.4 扫频引擎

**`core/sweep_engine.py`**

```python
class _SweepRunner(QObject):
    def run(self):
        # 1. 配置 SMB 扫频参数
        # 2. 启动 RALL? 高速采集
        # 3. 循环执行扫频
        # 4. 检测扫频完成（频率从 stop 跳回 start）
        # 5. 停止采集
```

**关键设计**：`_SweepRunner` 无 parent QObject

```python
def __init__(self, ctrl, sequence, recorder=None):
    super().__init__(None)  # 无 parent！
```

Qt 禁止 parent 对象跨线程 moveToThread。无 parent 的 `_SweepRunner` 可以安全地 `moveToThread` 到独立线程中执行扫频，避免阻塞 GUI。

### 3.5 数据层

**`data/recorder.py`**

输出目录结构：

```
experiment_20260511_143000/
├── data.parquet       # 主数据（Snappy 压缩）
├── columns.json       # 列定义 + ML 标签
└── metadata.json      # 采集元数据
```

**Schema 设计**：

| 列名 | 类型 | 说明 |
|------|------|------|
| sample_index | int64 | 全局采样序号 |
| time_s | float64 | 相对采集起点的时间 |
| host_timestamp_s | float64 | 主机单调时钟（绝对时间） |
| device_timestamp_s | float64 | 设备本地时间（占位） |
| batch_index | int64 | RALL? 批次序号 |
| lockin_A_X_mv ~ aux_adc4_v | float64 | 20 个 RALL 参数 |
| smb_freq_hz | float64 | SMB100A 频率 |
| smb_power_dbm | float64 | SMB100A 功率 |
| smb_rf_on | bool | RF 开关状态 |

**时间戳同步策略**：

- 以 `time.monotonic()` 为统一时间轴
- SMB 轮询间隔 100ms，Lockin RALL? 间隔 50ms
- 下游分析时按 `host_timestamp_s` 最近邻匹配

---

## 4. 关键设计模式

### 4.1 命令模式 (Command Pattern)

```python
# 创建命令
cmd = Command(
    cmd_type=CommandType.SMB_SET_FREQUENCY,
    params={"freq_hz": 2.87e9},
    source="gui"  # 或 "agent"
)

# 异步提交
request_id = cmd_service.submit(cmd)

# 同步提交（Agent 使用）
success, msg, result = agent_api.set_frequency(2.87e9)
```

### 4.2 门面模式 (Facade Pattern)

`InstrumentController` 封装了 SMB100A + OE1022D 两台设备的所有操作，隐藏了：
- 驱动初始化和资源释放
- Worker 线程的创建和销毁
- 信号连接和断开

### 4.3 观察者模式 (Observer Pattern)

PyQt5 的 `pyqtSignal` 实现了观察者模式：
- Worker 是**被观察者**，发射状态更新信号
- GUI 是**观察者**，订阅信号并更新界面
- CommandService 作为**中介**，统一广播信号

### 4.4 策略模式 (Strategy Pattern) — 预留

`TimestampSyncHub.query_aligned()` 未来可扩展多种对齐策略：
- 最近邻匹配（Nearest Neighbor）
- 线性插值（Linear Interpolation）
- 窗口聚合（Window Aggregation）

---

## 5. 线程模型

```
Main Thread (GUI)
    └── QTimer (50ms) ──► _on_display_tick ──► pyqtgraph 刷新

QThread: CommandService Worker
    └── _process_loop() ──► 串行执行 Command 队列

QThread: SMB Poll
    └── SMBPollWorker.run() ──► 每 100ms 查询 SMB 状态

QThread: Lockin Monitor
    └── LockinMonitorWorker.run() ──► 每 94ms 查询 SNAPD?

QThread: Lockin Acquire (仅在扫频期间)
    └── LockinAcquireWorker.run() ──► 每 50ms 读取 RALL?

QThread: Sweep Engine
    └── _SweepRunner.run() ──► 执行扫频序列
```

**线程安全策略**：
- 驱动层：`threading.Lock` / `threading.RLock` 保护所有 I/O
- 数据层：`threading.Lock` 保护 `write_batch()`
- 命令层：单线程串行执行，天然避免竞争

---

## 6. 安全策略

### 6.1 功率限制

```
配置文件: smb.amplifier_installed = true/false
有放大器: 最大功率 10 dBm
无放大器: 最大功率 25 dBm
```

校验层级：
1. **前端**：GUI 输入框颜色警告（>80% 橙色 / >95% 红色 / >100% 禁止）
2. **命令层**：CommandService `_check_power_limit()` 抛出 `SafetyError`
3. **驱动层**：`SMB100ADriver.validate_power()` 硬限制 -120~+30 dBm

### 6.2 急停

```python
def emergency_stop(self):
    self._sweeping = False
    self._stop_lockin_acquire()
    self._smb.emergency_stop()  # OUTP OFF + FREQ:MODE CW
```

### 6.3 扫频超时

动态计算：`点数 × 驻留 + 30s` 裕量，避免硬编码超时。

---

## 7. GUI 页面结构

| 索引 | 页面 | 功能 |
|------|------|------|
| 0 | 设备连接 / Connection | SMB/Lockin 连接、自动扫描、状态显示 |
| 1 | 实时监控 / Monitor | 状态指示灯、SMB 参数、双通道数据 |
| 2 | 微波源控制 / Source | 频率、功率（安全限制）、调制、LF、扫频 |
| 3 | 锁相控制 / Lock-in Control | INPUT/FILTERS、REF/PHASE、GAIN/TC、OUTPUT、AUTO |
| 4 | 采集配置 / Acquisition | 存储路径、采样配置、实时波形 |
| 5 | 实验序列 / Sequence | 扫频序列、开始/停止 |
| 6 | 数据记录 / Data Log | 记录状态、当前文件 |
| 7 | 日志 / Log | SMB/Lockin/Serial 三标签页日志 |

---

## 8. 配置文件

**`config.xml` / `config.json`**

```json
{
  "smb": {
    "visa_address": "USB0::0x0AAD::0x0054::106789::INSTR",
    "timeout_ms": 10000,
    "amplifier_installed": true
  },
  "lockin": {
    "port": "COM4",
    "baudrate": 921600,
    "bytesize": 8,
    "parity": "N",
    "stopbits": 1,
    "timeout": 1.0
  },
  "lockin_bindings": {
    "SSI LIA-OE1022D,SN00001,Ver1.00": "COM4"
  },
  "sweep": {
    "power_dbm": -30.0,
    "start_freq_hz": 2.82e9,
    "stop_freq_hz": 2.92e9,
    "step_hz": 500e3,
    "dwell_ms": 500
  },
  "acquisition": {
    "save_dir": "./experiments",
    "auto_save": true
  }
}
```

---

## 9. 扩展接口

### 9.1 AI Agent API

```python
from core.agent_api import AgentAPI
from core.command_service import CommandService

api = AgentAPI(cmd_service)

# SMB100A
api.set_frequency(2.87e9)
api.set_power(-20.0)
api.set_output(True)

# OE1022D
api.get_lockin_snapd(channel=1)
api.auto_gain(channel=1)

# 系统
api.emergency_stop()
```

### 9.2 时间戳同步中心（预留）

```python
from core.timestamp_sync import TimestampSyncHub

hub = TimestampSyncHub()
hub.register_source("smb", poll_interval_ms=100)
hub.register_source("lockin", poll_interval_ms=50)
# 未来: hub.register_source("mag_field", poll_interval_ms=200)
```

---

## 10. 已知限制与 TODO

| 优先级 | 项目 | 说明 |
|--------|------|------|
| 🔴 高 | `LOCKIN_START_ACQUIRE` 的 recorder 传递 | 当前传 `None`，需完善 |
| 🔴 高 | `SweepEngine._runner._stop_requested` 直接访问私有属性 | 应改为公开方法 |
| 🟡 中 | `CommandService._execute()` if-elif 链过长 | 建议改用字典映射 |
| 🟡 中 | `TimestampSyncHub.query_aligned()` 未实现 | 预留接口 |
| 🟡 中 | 单元测试覆盖不足 | 未测试 CommandService、SweepEngine、GUI |
| 🟢 低 | `CircularBuffer.extend()` 多通道长度不一致风险 | 应添加校验 |
| 🟢 低 | `origin/smb-locked_2.1.1.py` 6 万行 legacy 代码 | 建议归档 |
