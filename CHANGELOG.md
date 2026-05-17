# ODMR Control 变更日志

> 格式遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.0.0/)

---

## [2.1.1] - 2026-05-15

### Bug 修复

| 编号 | 问题 | 严重程度 | 修复 |
|------|------|----------|------|
| FIX-021 | SMB100A `SOUR:FREQ:MODE SWEEP` 命令无效，手册要求 `SWE` | 🔴 高 | 改为 `SOUR:FREQ:MODE SWE` |
| FIX-022 | SMB100A `SWE:DWELL` 命令拼写错误，手册要求 `SWE:DWEL` | 🔴 高 | 改为 `SWE:DWEL` |
| FIX-023 | SMB100A `SWE:TRIG:SOUR` 命令路径错误，手册要求 `TRIG:FSW:SOUR` | 🔴 高 | 改为 `TRIG:FSW:SOUR` |
| FIX-024 | SMB100A LF Sweep trigger 命令路径错误，手册要求 `TRIG:LFFS:SOUR` | 🔴 高 | 改为 `TRIG:LFFS:SOUR` |
| FIX-025 | SMB100A LF Sweep step 命令不完整，手册要求 `SOUR:LFO:SWE:FREQ:STEP:LIN` | 🔴 高 | 补全为 `SOUR:LFO:SWE:FREQ:STEP:LIN` |
| FIX-026 | SMB100A AM depth 命令不完整，手册要求 `SOUR:AM:DEPT:LIN` | 🔴 高 | 补全为 `SOUR:AM:DEPT:LIN` |
| FIX-027 | OE1022D `FREQD` 使用 `.6g` 精度，损失 1 mHz 分辨率 | 🟡 中 | 改为 `.6f` |
| FIX-028 | OE1022D `SRATD` 缺少 1 ms~100 s 范围校验 | 🟡 中 | 新增 `1.0 <= step_time_ms <= 100000.0` 校验 |
| FIX-029 | OE1022D `PHASD` 缺少 ±180° 范围校验 | 🟡 中 | 新增 `-180.0 <= phase_deg <= 180.0` 校验 |

### 变更

- `instruments/smb100a.py`：6 处 SCPI 命令与 R&S SMB100A 操作手册对齐
- `instruments/oe1022d.py`：1 处 ASCII 格式修正 + 2 处参数范围校验与 OE1022D 手册对齐

---

## [2.1.0] - 2026-05-13

### 新增

- **激光器控制模块** (`instruments/laser_msl.py`)
  - MSL-U 系列 RS232 驱动（9600/8N1），十六进制指令协议
  - 设置功率: `55 AA 05 01 <H> <L> <CHK>`，开/关固定指令
  - 软件端缓存功率和输出状态（协议无查询命令）
  - `scan_ports()` 串口扫描
  - 线程安全 (`threading.Lock`)

- **激光器命令总线** (`core/commands.py`, `core/command_service.py`)
  - `LASER_CONNECT / DISCONNECT / SET_POWER / SET_OUTPUT / QUERY_STATE / EMERGENCY_STOP`
  - `laser_state_broadcast` 信号广播
  - `_check_laser_power_limit()` 功率安全校验

- **激光器 GUI 集成** (`app/gui.py`)
  - 连接页面：端口扫描、连接/断开、状态指示灯
  - 监控页面：激光功率、输出状态、波长显示
  - 全局连接/断开/急停包含激光器

- **激光器数据记录** (`data/recorder.py`)
  - Parquet Schema 新增 `laser_power_mw` (float64) 和 `laser_on` (bool)
  - `write_batch()` 新增激光器同步参数

### Bug 修复

| 编号 | 问题 | 严重程度 | 修复 |
|------|------|----------|------|
| FIX-016 | SMB100A `ResourceManager.release_all_resources()` 方法不存在 | 🔴 高 | 改为 `ResourceManager.close()` |
| FIX-017 | OE1022D `import serial.tools.list_ports` 局部覆盖模块级 `serial` | 🔴 高 | 改为 `from serial.tools import list_ports as _list_ports` |
| FIX-018 | OE1022D `connect()` 失败后串口未关闭 | 🔴 高 | 添加 try/except，失败时关闭串口并置 None |
| FIX-019 | OE1022D `close()` 未加锁，多线程竞争 | 🟡 中 | `close()` 加 `self._lock` |
| FIX-020 | OE1022D/SMB100A 扫描结果含 IDN 后缀导致连接失败 | 🔴 高 | GUI 使用 `currentData() or currentText()` 获取纯端口名 |

### 安全加固

| 编号 | 问题 | 严重程度 | 修复 |
|------|------|----------|------|
| SEC-001 | `LaserMSLDriver.close()` 未关闭激光输出直接断开 | 🔴 高 | `close()` 发送 `_CMD_OFF` 后再关闭串口 |
| SEC-002 | `LaserMSLDriver.emergency_stop()` 静默吞异常无重试 | 🔴 高 | 添加 3 次重试，返回 `bool` 表示成功/失败 |
| SEC-003 | `SMB_SET_SWEEP` 绕过功率安全校验 | 🔴 高 | 调用 `_check_power_limit(sweep_power)` |
| SEC-004 | `LASER_SET_POWER` 无命令层功率校验 | 🟡 中 | 添加 `_check_laser_power_limit()` |
| SEC-005 | `verify_emergency_stop()` 未验证激光器状态 | 🟡 中 | 新增激光器输出状态验证 |
| SEC-006 | `disconnect_laser()` 未关闭激光输出 | 🟡 中 | 断开前调用 `set_output(False)` |

### 变更

- `instruments/smb100a.py`：`ResourceManager` 关闭方法修正为 `close()`
- `instruments/oe1022d.py`：连接失败处理、`close()` 线程安全、`scan_ports` 导入修正
- `instruments/laser_msl.py`：`close()` 安全关闭、`emergency_stop()` 重试机制
- `core/instrument_controller.py`：`disconnect_laser()` 先关输出再断开、`verify_emergency_stop()` 含激光器验证、`emergency_stop()` 检查返回值
- `core/command_service.py`：新增 `_check_laser_power_limit()`、`SMB_SET_SWEEP` 功率校验
- `data/recorder.py`：instrument 字符串更新为 `"OE1022D+SMB100A+Laser"`
- `app/config_io.py`：新增 `laser` 配置节、`smb_bindings` 字段

---

## [2.0.1] - 2026-05-12

### Bug 修复

| 编号 | 问题 | 严重程度 | 修复 |
|------|------|----------|------|
| FIX-011 | `CommandService._process_loop` 阻塞 GUI 主线程导致窗口冻结 | 🔴 致命 | `start()` 中加入 `self.moveToThread(self._worker_thread)`，`_process_loop` 在工作线程执行 |
| FIX-012 | GUI 创建第二个 `InstrumentController` 实例，与 CommandService 管理的实例不一致 | 🔴 高 | GUI 复用 `cmd_service._ctrl`，仅在无 cmd_service 时创建独立实例 |
| FIX-013 | Lockin 状态信号（指示灯）与 SNAPD 数据混用 `lockin_data_broadcast` | 🟡 中 | 新增 `lockin_status_broadcast` 独立信号 |
| FIX-014 | `_on_smb_state_changed` / `_on_lockin_data_ready` 中 late import `TimestampedSample` | 🟢 低 | 移至模块顶层导入 |
| FIX-015 | `_on_smb_state_changed` 引用未创建的 `self._freq_display` 导致 AttributeError | 🔴 高 | 添加 `hasattr` 守卫 |

### 变更

- `core/command_service.py`：
  - `start()` 中 `QThread` 不再设置 parent，`CommandService` 通过 `moveToThread` 移入工作线程
  - 新增 `lockin_status_broadcast` 信号，从 `lockin_data_broadcast` 分离状态数据
  - `TimestampedSample` 改为顶层 import
- `app/gui.py`：
  - 移除 `self._ctrl = InstrumentController(self)`，改为 `self._ctrl = self._cmd_service._ctrl`
  - Lockin 指示灯连接改为 `lockin_status_broadcast`

---

## [2.0.0] - 2026-05-12

### 概述

V2.0 是一次大规模功能增强，引入统一的命令总线架构，支持 AI Agent 操作，扩展了设备控制能力和实时监控功能。

### 架构层 (Stage 0)

#### 新增

- **命令定义层** (`core/commands.py`)
  - `CommandType` 枚举：40+ 种命令类型，覆盖 SMB100A、OE1022D、采集、系统
  - `Command` frozen dataclass：支持 JSON 序列化/反序列化
  - `source` 字段区分 "gui" / "agent" / "system"

- **命令执行服务** (`core/command_service.py`)
  - 单线程串行执行队列，天然避免 VISA/Serial 并发冲突
  - `submit()` 异步提交 + `submit_sync()` 同步阻塞
  - 功率安全限制校验（`SafetyError`）
  - 统一信号广播：`smb_state_broadcast`, `lockin_data_broadcast`, `lockin_batch_broadcast`
  - 异步命令追踪：`command_completed` / `command_error` 信号

- **AI Agent API** (`core/agent_api.py`)
  - `AgentAPI` 类：面向 AI Agent 的高级 Python API
  - SMB100A 控制：`set_frequency`, `set_power`, `set_output`, `set_sweep`, `start_sweep`, `stop_sweep`, `get_smb_status`
  - OE1022D 控制：`get_lockin_snapd`, `get_lockin_status`, `set_lockin_time_constant`, `auto_gain`, `auto_reserve`, `auto_phase`
  - 系统控制：`emergency_stop`, `get_all_status`
  - 组合操作：`run_odmr_sweep()`

- **时间戳同步中心** (`core/timestamp_sync.py`)
  - `TimestampedSample` dataclass：统一时间戳样本格式
  - `TimestampSyncHub`：多源同步中心（预留接口）
  - 当前实现：`register_source()` 和 `ingest()`
  - 预留：`query_aligned()` 待实现最近邻匹配

#### 变更

- `main.py`：初始化顺序更新为 `InstrumentController` → `CommandService` → `GUI` → 可选 `AgentAPI`

### Part 1: 设备连接重构

#### 新增

- **SMB100A 多协议支持**
  - GUI 协议选择下拉框：USB / GPIB / LAN / Serial
  - 地址输入框动态 placeholder 提示
  - 协议接口说明文本动态更新
  - 自动扫描按钮：调用 `pyvisa.ResourceManager().list_resources()` + `*IDN?` 过滤罗德施瓦茨设备
  - `SMB100ADriver.scan_rs_devices()` 类方法

- **OE1022D 串口自动匹配**
  - 扫描时发送 `*IDND?` 验证 IDN 格式 (`SSI LIA-OE1022D,SNXXXXXX,VerXXX`)
  - `OE1022DDriver.scan_ports_with_idn()` 类方法
  - 配置文件 `lockin_bindings` 字段记录 SN→COM 绑定
  - 自动匹配已知设备，连接成功后自动保存绑定

#### 变更

- `app/config_io.py`：新增 `smb.amplifier_installed`（默认 `true`）和 `lockin_bindings`（默认 `{}`）
- `app/gui.py` 连接页面：
  - 移除独立的"状态总览" group
  - 状态文本嵌入每个设备连接区域内
  - 连接操作全部通过 `CommandService.submit()` 异步提交
  - 结果通过 `_on_command_completed()` 信号回调更新 UI

### Part 2: 实时监控重构

#### 新增

- **状态指示灯系统**
  - SMB100A：RF / LF Output / LF Sweep / Modulation 四灯
  - OE1022D CH-A/B：Input Overload / Gain Overload / PLL Locked 六灯
  - 颜色逻辑：过载=红色 / 正常=绿色 / PLL锁定=绿色 / 未锁定=红色

- **SMB100A 实时参数扩展**
  - 功率 (dBm)、RF 输出状态、LF 输出状态、LF 频率、调制状态、当前模式
  - 大字体参数面板（7 个参数，4 列网格布局）

- **Lockin 双通道标签页**
  - `QTabWidget` 包含 "Channel A" / "Channel B"
  - 每通道独立的 X/Y/R/θ 显示 + 独立 `CircularBuffer`

- **波形图交互改进**
  - 固定 X 轴时间窗口：512/1024/2048/4096/8192 ms 可选
  - 自动 Y 轴缩放：对称范围，原点居中
  - 零线显示：`pg.InfiniteLine(pos=0, angle=0)`
  - "Auto Scale Y" 复选框
  - 通道切换：Channel A / Channel B

- **Worker 信号扩展**
  - `SMBPollWorker.state_dict_updated`：完整状态字典广播
  - `LockinMonitorWorker.status_ready`：过载/PLL 状态广播
  - `InstrumentController.smb_state_dict_changed` 信号
  - `InstrumentController.lockin_status_changed` 信号

#### 变更

- `workers/smb_poll_worker.py`：新增功率、LF 状态、调制状态查询
- `workers/lockin_monitor_worker.py`：双通道交替查询（CH-A ↔ CH-B），每 10 轮查询一次状态
- `app/gui.py` 监控页面：
  - 使用 `QScrollArea` 包裹内容
  - 移除波形图（迁移到采集配置）
  - `_on_smb_state_changed()` 兼容新旧两种信号格式
  - `_on_lockin_data_ready()` 按 `channel` 字段路由

### Part 3: 微波源控制重构

#### 新增

- **功率安全限制**
  - 配置文件 `smb.amplifier_installed` 区分有/无放大器
  - 有放大器：最大功率 10 dBm
  - 无放大器：最大功率 25 dBm
  - GUI 输入框动态颜色警告：>80% 橙色 / >95% 红色 / >100% 禁止并弹窗
  - CommandService 层 `_check_power_limit()` 硬校验

- **微波源控制页面重设计**
  - 按说明书分为 5 个分组：RF频率 / RF功率 / 调制 / LF发生器 / 扫频
  - 每个参数有独立"设 / Set"按钮
  - "应用所有参数 / Apply All" 批量设置按钮

#### 变更

- `app/gui.py` 微波源控制页面：
  - 使用 `QScrollArea` 包裹
  - 功率输入框旁显示限制值
  - 放大器状态文本显示
  - 所有设置操作通过 CommandService 提交

### Part 4: 锁相放大器参数控制

#### 新增

- **新增"锁相控制"页面**（导航树第 3 项）
  - INPUT / FILTERS：`set_input_source`, `set_current_gain`, `set_grounding`, `set_coupling`, `set_line_notch`
  - REF / PHASE：`set_ref_phase`, `set_ref_source`, `set_ref_slope`, `set_ref_frequency`, `set_harmonic`
  - GAIN / TC：`set_sensitivity`, `set_reserve`, `set_time_constant`, `set_filter_slope`, `set_sync_filter`
  - CHANNEL OUTPUT：`set_output_source`, `set_output_offset`, `set_output_expand`
  - AUTO SET：`auto_gain`, `auto_reserve`, `auto_phase`

- **OE1022D 驱动扩展**
  - 新增 15+ 个配置方法（见 `instruments/oe1022d.py`）
  - 所有方法均通过 `CommandService` 路由

- **GUI 全局通道切换**
  - 页面顶部 CH A/B 下拉框
  - 影响所有参数设置的目标通道

#### 变更

- `app/gui.py` 导航树：新增第 3 项"锁相控制 / Lock-in Control"
- `core/command_service.py`：新增 `LOCKIN_SET_INPUT`, `LOCKIN_SET_REF_PHASE`, `LOCKIN_SET_GAIN_TC`, `LOCKIN_SET_OUTPUT` 路由

### Part 5: 采集配置与时间戳同步

#### 新增

- **时间戳同步架构**
  - `TimestampSyncHub` 注册 SMB (100ms) 和 Lockin (50ms) 两源
  - `CommandService` 在状态转发时自动 `ingest()` 到 Hub
  - Parquet Schema 新增 `host_timestamp_s` 和 `device_timestamp_s` 列
  - `metadata.json` 记录 `timestamp_sync` 策略和各设备轮询间隔

- **OE1022D 采样配置 UI**
  - 采集配置页面新增采样配置组
  - 模式选择：RALL? (固定 1kHz) / Buffer (可配置)
  - Step Time：1/2/5/10/20/50/100 ms
  - Length 输入框
  - Trigger：Internal / External

#### 变更

- `data/recorder.py`：
  - Schema 新增 `host_timestamp_s`（主机单调时钟）和 `device_timestamp_s`（占位）
  - `write_batch()` 注入 `time.monotonic()` 时间戳
  - `_save_columns_json()` 和 `_save_metadata_json()` 更新

- `app/gui.py` 采集配置页面：
  - 新增波形图区域（从监控页面迁移）
  - 新增采样配置组
  - 使用 `QScrollArea` 包裹

### Bug 修复

| 编号 | 问题 | 修复 |
|------|------|------|
| FIX-001 | `_SweepRunner` 有 parent 导致 moveToThread 失败 | `super().__init__(None)` |
| FIX-002 | 多线程 VISA/Serial 竞争 | 全 I/O 加锁 (`threading.Lock`/`RLock`) |
| FIX-003 | 僵尸 Worker 线程 | `terminate()` 兜底 |
| FIX-004 | 扫频期间 SMBPollWorker 并发读频率 | 扫频期间暂停 SMB 轮询 |
| FIX-005 | Parquet 线程不安全 | `threading.Lock` 保护 `write_batch()` |
| FIX-006 | 扫频超时硬编码 | 动态计算 `点数 × 驻留 + 30s` |
| FIX-007 | `_emergency_stop()` 绕过 CommandService | 改为提交 `SYS_EMERGENCY_STOP` |
| FIX-008 | `SMBPollWorker` 直接访问 `_query()` 私有方法 | 新增 `get_lf_output()` 等公开方法 |
| FIX-009 | 指示灯状态逻辑 bug | 修正 `"true" if ov else "false"` |
| FIX-010 | `SMB_SET_MODULATION` 未实现 | 添加处理逻辑 |

---

## [1.1.0] - 2026-05-XX

### 概述

代码审查 Round 1，修复 6 个关键问题。

### 修复

- **C1**: `_SweepRunner` moveToThread 失败（Qt parent 跨线程限制）
- **C2**: 多线程 VISA/Serial 竞争（缺乏锁保护）
- **C3**: 僵尸 Worker 线程（`quit()` + `wait()` 失败后未 `terminate()`）
- **C4**: 扫频期间 SMBPollWorker 并发读频率（导致读数混乱）
- **C5**: Parquet 线程不安全（多线程同时 `write_batch()`）
- **C6**: 扫频超时硬编码（30s，未考虑大点数扫频）

---

## [1.0.0] - 2026-05-XX

### 概述

从单文件 demo 重构为多模块分层架构的初始版本。

### 新增

- **分层架构**：
  - `app/`：GUI 层
  - `core/`：控制层
  - `instruments/`：驱动层
  - `workers/`：Worker 线程层
  - `data/`：数据层
  - `tests/`：单元测试

- **核心功能**：
  - SMB100A 连接/断开/参数设置
  - OE1022D 连接/断开/SNAPD? 监控/RALL? 采集
  - 扫频序列引擎（单次/循环/多步）
  - Parquet 数据记录（20 参数 × 1kHz）
  - pyqtgraph 实时波形显示
  - Siemens 工业风 GUI

- **单元测试**：
  - `TestCircularBuffer`
  - `TestSMB100ADriver`（FakeVISA）
  - `TestOE1022DDriver`（FakeSerial）

---

## [0.1.0] - 2026-05-XX

### 概述

单文件 demo 版本，功能完整但架构混乱。

### 功能

- SMB100A + OE1022D 基础控制
- 单步扫频
- 实时波形显示
- 基础 GUI

### 文件

- `origin/smb-locked_2.1.1.py`（~60,000 行 legacy 代码）
