# ODMR Control

PyQt5 桌面应用，用于控制光探测磁共振 (ODMR) 实验。统一管理三台仪器：

| 设备 | 型号 | 协议 | 用途 |
|------|------|------|------|
| 微波源 | Rohde & Schwarz SMB100A | VISA/SCPI | 微波激励 |
| 锁相放大器 | SSI OE1022D | Serial/RS-232 | 信号检测 |
| 激光器 | CNI MSL-U-532nm-300mW | Serial/RS-232 (单向) | 光激发 |

## 环境要求

| 项目 | 要求 |
|------|------|
| Python | >= 3.9（推荐 3.11–3.13） |
| 操作系统 | Windows 10/11（推荐）、macOS、Linux |
| 内存 | >= 4 GB |
| 磁盘 | >= 500 MB（实验数据另计） |

## 安装与部署

### 方式一：Conda 环境（推荐）

```bash
# 1. 创建并激活环境
conda create -n odmr python=3.11
conda activate odmr

# 2. 安装依赖
pip install -r requirements.txt

# 3. 验证安装
python -m pytest tests/ -v
```

### 方式二：系统 Python / venv

```bash
# 1. 创建虚拟环境
python -m venv .venv

# Windows
.venv\Scripts\activate

# macOS / Linux
source .venv/bin/activate

# 2. 安装依赖
pip install -r requirements.txt

# 3. 验证安装
python -m pytest tests/ -v
```

### 硬件驱动与 VISA 后端

SMB100A 通过 **VISA** 通信，需安装对应后端的驱动：

| 连接方式 | 后端 | 说明 |
|----------|------|------|
| USB / GPIB | NI-VISA 或 R&S VISA | 推荐 NI-VISA 21.0+ |
| LAN (TCPIP) | 无需额外驱动 | pyvisa 自带 socket 后端 |
| Serial | pyserial 即可 | 无需单独 VISA 驱动 |

> **Windows 用户**：安装 NI-VISA 后，设备管理器中会出现 "NI-VISA USB Device"，Resource Manager 可自动识别地址。
>
> **macOS/Linux 用户**：可使用 `pyvisa-py` 纯 Python 后端（功能受限，建议测试用）：
> ```bash
> pip install pyvisa-py
> ```

OE1022D 和激光器通过 **RS-232 串口** 连接，需确保：
- USB-转-串口适配器驱动已安装
- 端口不被其他程序占用
- 在 `config.json` 中配置正确的串口号

### 配置文件

首次运行前，检查 `config.json`：

```json
{
  "smb": {
    "address": "TCPIP0::192.168.1.10::inst0::INSTR",
    "amplifier_installed": true
  },
  "lockin": {
    "port": "COM3",
    "baudrate": 115200
  },
  "laser": {
    "port": "COM4",
    "max_power_mw": 150
  }
}
```

## 快速开始

```bash
# 激活环境（如使用 conda/venv）
conda activate odmr

# 启动主程序
python main.py

# 运行单元测试（无需硬件）
python -m pytest tests/ -v
```

### 首次运行检查清单

- [ ] 微波源 SMB100A 已上电，网线/USB 已连接
- [ ] 锁相放大器 OE1022D 已上电，串口已连接
- [ ] 激光器已上电，串口已连接
- [ ] `config.json` 中的地址/端口与实际一致
- [ ] 已安装 VISA 后端（USB/GPIB 场景）

## 架构

六层架构，统一命令总线：

```
┌─────────────────────────────────────────────────────┐
│  Layer 5: GUI / Config                              │
│  app/gui.py, app/config_io.py                       │
├─────────────────────────────────────────────────────┤
│  Layer 4: AI Agent API                              │
│  core/agent_api.py                                  │
├─────────────────────────────────────────────────────┤
│  Layer 3: Command Bus  ◄── 所有操作的唯一入口        │
│  core/command_service.py                            │
├─────────────────────────────────────────────────────┤
│  Layer 2: Instrument Controller (Facade)            │
│  core/instrument_controller.py                      │
├─────────────────────────────────────────────────────┤
│  Layer 1.5: Sweep Engine / Timestamp Sync           │
│  core/sweep_engine.py, core/timestamp_sync.py       │
├─────────────────────────────────────────────────────┤
│  Layer 1: Device Drivers                            │
│  instruments/smb100a.py, oe1022d.py, laser_msl.py   │
├─────────────────────────────────────────────────────┤
│  Layer 0: Data Recording                            │
│  data/recorder.py, data/circular_buffer.py          │
└─────────────────────────────────────────────────────┘
```

## 中间控制层设计

中间控制层（Layer 2-3）是整个系统的核心，解决了三个关键问题：

### 1. 命令总线 (CommandService) — 为什么需要？

**问题**：GUI 主线程、AI Agent、定时器都可能同时操作设备。VISA 和 Serial 协议不支持并发访问，多线程直接调用驱动会导致数据错乱或设备死锁。

**方案**：所有操作统一序列化为 `Command` 对象，进入单线程队列串行执行。

```
GUI 线程 ──┐
AI Agent ──┤──► CommandService._process_loop() ──► Driver
定时器   ──┘     (单线程串行执行，天然线程安全)
```

- `submit(cmd)` — 异步提交，结果通过 Qt 信号回调
- `submit_sync(cmd, timeout)` — 同步阻塞，仅限非 GUI 线程使用（AI Agent 场景）

**关键约束**：GUI 线程永远不能调用 `submit_sync()`，否则会死锁（Qt 事件循环被阻塞，信号回调永远无法到达）。

### 2. 三层信号广播 — 为什么是三层？

信号流：`Driver → InstrumentController → CommandService → GUI`

| 层级 | 信号 | 职责 |
|------|------|------|
| Driver | 无信号 | 纯 I/O，不关心谁在监听 |
| InstrumentController | `smb_state_changed`, `lockin_data_ready` 等 | Worker 线程采集 → 跨线程信号 |
| CommandService | `smb_state_broadcast`, `lockin_data_broadcast` 等 | 统一出口，GUI 和 Agent 均订阅此层 |

**为什么不让 GUI 直接订阅 InstrumentController？**
- CommandService 需要在转发时做额外处理（如时间戳同步摄入）
- 未来如果 CommandService 需要拦截/修改状态数据，只需改一处
- GUI 和 Agent 订阅同一个信号源，行为一致

### 3. 读写分离 — Worker 为什么绕过 CommandService？

Worker（SMBPollWorker、LockinMonitorWorker）以 50-100ms 间隔轮询设备状态。如果每次轮询都走 CommandService 队列：
- 队列会被高频状态查询塞满，阻塞用户操作命令
- 增加不必要的序列化开销

**方案**：Worker 直接读 Driver（只读操作），通过 `driver.get_xxx()` + `threading.Lock` 保证线程安全。写操作仍然走 CommandService。

```
写操作 (GUI/Agent) ──► CommandService ──► Driver (Lock 保护)
读操作 (Worker)    ──────────────────────► Driver.get_xxx() (Lock 保护)
```

### 4. 安全校验 — 在哪一层？

安全校验在 **CommandService 层**（Layer 3），不在 Driver 层：

| 校验 | 位置 | 说明 |
|------|------|------|
| 微波功率限制 | `CommandService._check_power_limit()` | 有放大器 10 dBm / 无放大器 25 dBm |
| 激光功率限制 | `CommandService._check_laser_power_limit()` | 0 ~ max_power_mw |
| 频率范围 | `SMB100ADriver.set_freq_cw()` | 100 kHz – 12.75 GHz |
| 急停 | `CommandService` → `InstrumentController` | 关闭所有输出，停止采集 |

**为什么不在 Driver 层做安全校验？**
- Driver 是通用硬件接口，不应包含业务策略（如"有放大器时限制 10 dBm"）
- 安全策略可能随时调整（如更换放大器），应集中在配置驱动的 CommandService 层

### 5. 线程模型

```
Main Thread          : GUI + QTimer 刷新
CommandService Thread: 命令队列串行执行
SMBPollWorker Thread : 100ms 轮询 SMB100A 状态
LockinMonitor Thread : 94ms 轮询 OE1022D 状态
LockinAcquire Thread : 50ms RALL? 高速采集（仅扫频期间）
LaserPollWorker Thread: 1000ms 广播激光器缓存状态
SweepEngine Thread   : 扫频序列执行
```

### 6. AI Agent 接口

`core/agent_api.py` 提供 `AgentAPI` 类，底层调用 `CommandService.submit_sync()`：

```python
api = AgentAPI(cmd_service)
api.set_frequency(2.85e9)
api.set_power(-30)
api.emergency_stop()
```

Agent 和 GUI 共享同一个 CommandService 实例，通过 `source` 字段区分调用来源。

## 数据输出

Parquet (Snappy) 格式，含完整元数据：

```
experiment_20260511_143000/
├── data.parquet      # 20 个 RALL? 参数 + SMB 状态 + 激光器状态
├── columns.json      # 列定义 + ML 标签
└── metadata.json     # 设备、采样率、时间戳策略
```

列命名规范：`{source}_{quantity}_{unit}`，如 `lockin_x_mV`, `smb_freq_hz`, `laser_power_mw`。

## 安全设计

| 机制 | 说明 |
|------|------|
| 功率限制 | 微波 10/25 dBm（放大器开关），激光 0-150 mW |
| 急停 | `SYS_EMERGENCY_STOP` — 关闭 RF/LF/FM/激光，停止采集 |
| 断开保护 | `disconnect_laser()` 先关输出再断开串口 |
| 急停重试 | `emergency_stop()` 最多重试 3 次，返回成功/失败 |
| I/O 锁 | 所有 VISA/Serial 操作受 `threading.Lock`/`RLock` 保护 |
| 扫频功率校验 | `SMB_SET_SWEEP` 的功率参数也受 `_check_power_limit()` 限制 |

## 配置

`config.json` 在项目根目录，关键配置节：

| 节 | 内容 |
|------|------|
| `smb` | VISA 地址、放大器状态、频率/功率范围 |
| `lockin` | 串口参数、RALL? 配置 |
| `laser` | 串口参数、USB 适配器 SN、最大功率、波长 |
| `lockin_bindings` / `smb_bindings` | 设备 SN → COM 端口绑定（自动重连） |
| `sweep` | 扫频默认参数 |
| `acquisition` | 采集间隔、保存目录 |

## 项目结构

```
odmr-control/
├── app/                    # GUI 层
│   ├── gui.py              # PyQt5 主界面 (Siemens 工业风)
│   └── config_io.py        # 配置加载/保存
├── core/                   # 控制层
│   ├── commands.py         # Command / CommandType 定义
│   ├── command_service.py  # 命令总线 (单线程串行)
│   ├── instrument_controller.py  # 设备门面 + Worker 管理
│   ├── agent_api.py        # AI Agent API
│   ├── sweep_engine.py     # 扫频序列引擎
│   └── timestamp_sync.py   # 时间戳同步
├── instruments/            # 驱动层
│   ├── smb100a.py          # SMB100A VISA 驱动
│   ├── oe1022d.py          # OE1022D Serial 驱动
│   └── laser_msl.py        # MSL-U 激光器 RS232 驱动
├── workers/                # 后台 Worker
│   ├── smb_poll_worker.py
│   ├── lockin_monitor_worker.py
│   ├── lockin_acquire_worker.py
│   └── laser_poll_worker.py
├── data/                   # 数据层
│   ├── recorder.py         # Parquet 记录器
│   └── circular_buffer.py  # 环形缓冲区
├── tests/                  # 单元测试 (FakeVISA/FakeSerial)
├── docs/                   # 文档
└── main.py                 # 入口
```
