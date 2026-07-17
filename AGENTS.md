# ODMR Control — Agent 指南

> 本文档供 AI 编程助手阅读。项目所有注释、文档、UI 标签均为中文，代码标识符为英文。

---

## 1. 项目概述

ODMR Control 是一个基于 PyQt5 的桌面应用程序，用于控制**光探测磁共振（ODMR）**实验。统一管理以下四台设备：

| 设备 | 型号 | 厂商 | 通信协议 | 用途 |
|------|------|------|----------|------|
| 微波源 | SMB100A | Rohde & Schwarz | VISA/SCPI (USBTMC/GPIB/LAN) | 微波激励 |
| 锁相放大器 | OE1022D | SSI | Serial/RS-232 (921600 baud) | 信号检测 |
| 激光器 | MSL-U-532nm-300mW | CNI | Serial/RS-232 (9600 baud, 单向) | 光激发 |
| 磁场线圈 | M8812 | MAYNUO | Serial/RS-232 (9600 baud) | 三轴磁场控制 |

**当前版本**: V2.1.0+
**架构风格**: 参考 mag-field-control-v2 的多层安全架构

---

## 2. 技术栈

| 组件 | 技术 | 最低版本 |
|------|------|----------|
| GUI 框架 | PyQt5 | ≥5.15 |
| 仪器通信 | pyvisa (SMB100A) + pyserial (OE1022D/激光器/磁场) | ≥1.13 / ≥3.5 |
| 实时波形 | pyqtgraph | ≥0.13 |
| 数值计算 | numpy | ≥1.24 |
| 配置校验 | jsonschema | ≥4.17 |

无 pyproject.toml / setup.py。依赖通过 `requirements.txt` 管理，纯 Python 项目，无需编译。

---

## 3. 项目结构

```
odmr-control/
├── main.py                      # 入口：初始化 InstrumentController → CommandService → GUI
├── config.json                  # 运行时配置（JSON 格式，支持 XML 回退）
├── requirements.txt             # 依赖清单
│
├── app/                         # Layer 5: GUI + 配置
│   ├── gui.py                   # PyQt5 主界面 (~5200 行，Siemens 工业风)
│   ├── config_io.py             # 配置加载/保存（JSON/XML 兼容，深度合并默认值）
│   ├── experiment_draft.py      # GUI 实验草案数据模型
│   ├── plan_compiler.py         # 草案 → schema 兼容的实验计划编译器
│   └── unit_selector.py         # 带单位选择的物理量输入组件
│
├── core/                        # Layer 2-4: 控制层
│   ├── commands.py              # CommandType 枚举 + Command 不可变数据类
│   ├── command_service.py       # 命令总线（单线程串行队列，安全校验，信号广播）
│   ├── instrument_controller.py # 门面：管理驱动 + Worker 线程
│   ├── agent_api.py             # AI Agent 同步 Python API
│   ├── sweep_engine.py          # 扫频序列引擎（_SweepRunner 必须无 parent）
│   ├── mag_field_controller.py  # 三轴磁场控制门面
│   ├── mag_sequence_engine.py   # 磁场序列执行引擎
│   ├── preflight.py             # 实验计划起飞前检查
│   ├── safety_policy.py         # 安全策略（功率/磁场/驻留时间校验）
│   ├── timestamp_sync.py        # 多源时间戳同步中心（预留）
│   ├── vector_field.py          # 球坐标 ↔ 笛卡尔磁场转换
│   ├── run_manifest.py          # 实验运行清单（不可变执行记录）
│   └── resource_manager.py      # 资源管理辅助
│
├── instruments/                 # Layer 1: 设备驱动
│   ├── smb100a.py               # SMB100A VISA/SCPI 驱动
│   ├── oe1022d.py               # OE1022D Serial 驱动（ASCII + 二进制 RALL?）
│   ├── laser_msl.py             # MSL-U 激光器 RS232 驱动
│   └── magnet_coil.py           # MAYNUO M8812 磁场电源驱动
│
├── workers/                     # 后台轮询 Worker
│   ├── smb_poll_worker.py       # 100ms 轮询 SMB100A 状态
│   ├── lockin_monitor_worker.py # 94ms 轮询 SNAPD?
│   ├── lockin_acquire_worker.py # 50ms RALL? 高速采集（仅扫频期间）
│   ├── laser_poll_worker.py     # 1000ms 广播激光器缓存状态（无 I/O）
│   └── mag_poll_worker.py       # 500ms 轮询磁场电源电流
│
├── data/                        # Layer 0: 数据层
│   ├── recorder.py              # CSV 数据记录器（线程安全）
│   └── circular_buffer.py       # 环形缓冲区（pyqtgraph 实时波形）
│
├── tests/                       # 单元测试（FakeVISA/FakeSerial，无需硬件）
│   ├── test_instruments.py      # SMB100A + OE1022D 驱动测试
│   ├── test_experiment_automation.py  # 实验自动化验证逻辑
│   ├── test_magnetic_field_commands.py # 磁场控制命令测试
│   ├── test_plan_compiler.py    # 计划编译器测试
│   ├── test_unit_selector.py    # 单位选择器测试
│   └── ...
│
├── schemas/
│   └── experiment_plan.json     # 实验计划 JSON Schema (draft-07)
│
├── docs/                        # 架构文档、API 参考、变更日志
│   ├── ARCHITECTURE.md          # 完整架构设计文档
│   ├── API_REFERENCE.md         # API 参考
│   ├── DEVICE_SPECIFICATIONS.md # 设备规格与安全规范
│   └── CHANGELOG.md             # 变更日志
│
└── experiments/                 # 运行时生成的实验数据目录
```

---

## 4. 构建与运行

### 4.1 安装依赖

```bash
pip install -r requirements.txt
```

### 4.2 运行应用

```bash
python main.py
```

应用使用 `Fusion` 样式，启动顺序：
1. `QApplication` 创建
2. `load_config()` 加载配置
3. `InstrumentController()` 初始化驱动门面
4. `CommandService(controller, config)` 创建命令总线并 `start()`
5. `ODMRControlGUI(cmd_service)` 创建 GUI
6. `app.aboutToQuit.connect(cmd_service.stop)` 确保退出时清理

### 4.3 运行测试

```bash
# 推荐：pytest（无需硬件，无需 GUI）
python -m pytest tests/ -v

# 或用 unittest 直接运行特定模块
python -m unittest tests.test_instruments -v
```

测试使用 `FakeResource` / `FakeSerial` stub，完全隔离真实硬件。

---

## 5. 架构核心约束

### 5.1 命令总线（CommandService）— 唯一入口

**所有设备操作**必须通过 `CommandService`：

- `submit(cmd)` — 异步提交，结果通过 `command_completed` / `command_error` 信号回调
- `submit_sync(cmd, timeout_ms)` — 同步阻塞，**仅限非 GUI 线程使用**

**绝对禁止**：GUI 主线程调用 `submit_sync()`，会死锁（Qt 事件循环被阻塞，信号永远无法到达）。

命令在独立 `QThread` 中**单线程串行执行**，天然避免 VISA/Serial 并发冲突。不要在此引入并行。

### 5.2 三层信号广播

```
Driver → InstrumentController → CommandService → GUI/Agent
```

| 层级 | 信号示例 | 职责 |
|------|----------|------|
| InstrumentController | `smb_state_changed`, `lockin_data_ready` | Worker 跨线程信号 |
| CommandService | `smb_state_broadcast`, `lockin_data_broadcast` | 统一出口，GUI 和 Agent 均订阅 |

CommandService 转发时可做额外处理（如时间戳同步 `ingest()`）。

### 5.3 读写分离

- **写操作**（GUI/Agent）：`CommandService` → `Driver`（Lock 保护）
- **读操作**（Worker）：直接 `driver.get_xxx()`（Lock 保护，绕过 CommandService 避免队列拥塞）

### 5.4 线程模型

| 线程 | 职责 | 间隔 |
|------|------|------|
| Main Thread | GUI + QTimer 刷新 | 50–100 ms |
| CommandService Thread | 命令队列串行执行 | — |
| SMBPollWorker | SMB100A 状态轮询 | 100 ms |
| LockinMonitorWorker | SNAPD? 实时监控 | 94 ms |
| LockinAcquireWorker | RALL? 高速采集 | 50 ms（仅扫频期间） |
| LaserPollWorker | 激光器缓存状态广播 | 1000 ms（无 I/O） |
| SweepEngine | 扫频序列执行 | — |
| AxisPollWorker ×3 | 磁场电流回读 | 500 ms |

### 5.5 Qt 跨线程安全

- `_SweepRunner` 必须 `super().__init__(None)` — **无 parent** 才能 `moveToThread`
- Worker 使用 `moveToThread(QThread)` 模式，禁止直接 `QThread` 子类化并重写 `run()`

---

## 6. 代码风格规范

### 6.1 语言规范

- **注释、文档字符串、UI 标签、错误消息**：中文
- **代码标识符（类名、函数名、变量名）**：英文
- 类型注解：全面使用 `from __future__ import annotations` + Python 3.9+ 内置泛型（`list[str]`）

### 6.2 导入顺序

```python
from __future__ import annotations

# 标准库
import json
import threading
from pathlib import Path
from typing import Any, Dict, Optional

# 第三方库
from PyQt5.QtCore import QObject, QThread, pyqtSignal
import numpy as np

# 项目内部（按层从上到下）
from core.commands import Command, CommandType
from instruments.smb100a import SMB100ADriver
```

### 6.3 命名约定

- 类名：`PascalCase`（`CommandService`, `SMB100ADriver`）
- 函数/方法：`snake_case`（`submit_sync`, `set_frequency`）
- 常量：`UPPER_SNAKE_CASE`（`RALL_TOTAL_BYTES`, `MIN_FREQ_HZ`）
- 私有属性/方法：单下划线前缀（`_process_loop`, `_check_power_limit`）
- 信号名：`snake_case`（`smb_state_broadcast`, `lockin_data_ready`）

### 6.4 数据列命名规范

CSV/JSON 中物理量列名：`{source}_{quantity}_{unit}`

示例：`lockin_x_mV`, `smb_freq_hz`, `laser_power_mw`, `mag_X_target_field_nT`

---

## 7. 安全策略

安全校验集中在 **CommandService 层**，不在 Driver 层（Driver 是通用硬件接口，不应包含业务策略）。

### 7.1 功率限制

| 条件 | 最大允许功率 | 配置键 |
|------|-------------|--------|
| 有放大器 | 10 dBm | `smb.amplifier_installed = true` |
| 无放大器 | 25 dBm | `smb.amplifier_installed = false` |

校验层级：
1. **GUI 前端**：输入框颜色警告（>80% 橙色 / >95% 红色 / >100% 禁止）
2. **命令层**：`CommandService._check_power_limit()` 抛出 `SafetyError`
3. **驱动层**：`SMB100ADriver.validate_power()` 硬限制 -120~+30 dBm

### 7.2 激光器功率限制

范围：`0 ~ max_power_mw`（默认 150 mW），由 `_check_laser_power_limit()` 校验。

### 7.3 磁场限制

默认最大绝对磁场：`10,000,000 nT`（10 T），由 `SafetyEnvelope.magnetic_max_abs_nT` 控制。

### 7.4 急停

`SYS_EMERGENCY_STOP` 命令：
- 关闭 SMB100A RF / LF / FM 输出，切回 CW 模式
- 停止 RALL? 采集
- 关闭激光器输出
- 磁场输出归零
- `emergency_stop()` 最多重试 3 次，返回成功/失败

### 7.5 I/O 锁

所有 VISA/Serial 操作受 `threading.Lock` / `threading.RLock` 保护。RALL? 数据读取前必须验证 `len(raw) == 12288`。

---

## 8. 配置系统

`config.json` 或 `config.xml` 位于项目根目录。`app/config_io.py` 加载时：

1. 以 `DEFAULT_CONFIG`（代码内嵌）为基线
2. 读取配置文件并**深度合并**（嵌套 dict 递归覆盖，不存在的键保留默认值）
3. 缺失文件时自动使用全部默认值

关键配置节：

| 节 | 内容 |
|----|------|
| `smb` | VISA 地址、放大器状态、频率/功率范围 |
| `lockin` | 串口参数、RALL? 配置、显示刷新策略 |
| `laser` | 串口参数、USB 适配器 SN、最大功率、波长 |
| `magnetic_field` | 三轴绑定、线圈常数、零偏电流 |
| `lockin_bindings` / `smb_bindings` / `laser_bindings` | 设备 SN → 端口绑定（自动重连） |
| `sweep` | 扫频默认参数 |
| `acquisition` | 采集间隔、保存目录 |
| `automation` | 默认安全策略（出错时行为） |

---

## 9. 数据输出格式

### 9.1 实验目录结构

```
experiment_YYYYMMDD_HHMMSS_run_xxxxxx/
├── data.csv           # 主数据（UTF-8 BOM）
├── columns.json       # 列定义 + 分组信息
└── metadata.json      # 设备、采样率、时间戳策略、总批次/点数
```

### 9.2 CSV 格式

对齐 OE1022D LabVIEW 导出的分组表头风格：
- 第 1 行：日期 / 时间 / 采样间隔
- 第 2 行：分组名（CH-A / CH-B / ADC / SMB / Laser / MagneticField / System）
- 第 3 行：字段名
- 后续行：每个 RALL? sample 一行

**采样率**：硬件固定 1 kHz（50 ms/batch × 50 points = 12288 bytes），不可软件配置。

---

## 10. 实验自动化系统

### 10.1 流程

```
GUI Draft → PlanCompiler → JSON Schema Plan → Preflight → CommandService → Runtime
```

### 10.2 核心组件

- **`ExperimentPlanDraft`**（`app/experiment_draft.py`）：GUI 可变草案模型
- **`PlanCompiler`**（`app/plan_compiler.py`）：编译为 `experiment_plan_v1` schema 兼容的计划，执行安全策略校验
- **`ExperimentPreflight`**（`core/preflight.py`）：起飞前检查（输出目录可写、设备已连接、安全包络）
- **`CommandService`**：执行 `EXPERIMENT_LOAD_JSON / VALIDATE / PREFLIGHT / START / PAUSE / RESUME / STOP`
- **`RunManifest`**（`core/run_manifest.py`）：不可变执行记录（run_id、plan_hash、时间戳）

### 10.3 实验计划 Schema

定义见 `schemas/experiment_plan.json`，支持：
- 多步骤序列 + 循环
- 磁场设定（笛卡尔 `x_nT/y_nT/z_nT` 或球坐标 `magnitude_nT/theta_deg/phi_deg`）
- 微波 CW / 扫频（device sweep 或 software step）
- 采集触发器（start_trigger / stop_trigger 各 10 种）
- 条件步骤（`type: conditional`，支持复合条件）

---

## 11. 测试策略

### 11.1 测试原则

- **无需真实硬件**：所有驱动测试使用 `FakeResource` / `FakeSerial` stub
- **无需 PyQt5 GUI**：核心逻辑测试使用 `QCoreApplication` 或纯 Python
- **测试覆盖重点**：命令验证、计划编译、安全策略、驱动状态机

### 11.2 现有测试模块

| 测试文件 | 覆盖范围 |
|----------|----------|
| `test_instruments.py` | SMB100A/OE1022D 驱动、CircularBuffer |
| `test_experiment_automation.py` | 实验计划验证、生命周期、触发器兼容性 |
| `test_magnetic_field_commands.py` | 磁场控制命令、序列引擎 |
| `test_plan_compiler.py` | 计划编译、安全策略、步骤生成 |
| `test_unit_selector.py` | 单位转换、输入验证 |
| `test_acquisition_commands.py` | 采集命令 |
| `test_experiment_runtime_artifacts.py` | 运行时产物（manifest、目录结构） |
| `test_run_console_workflow.py` | 控制台工作流 |
| `test_lockin_config.py` | 锁相配置 |

### 11.3 添加新测试

- 使用 `unittest.TestCase` 或 `pytest`
- 需要 Qt 事件循环的测试：`self._app = QCoreApplication.instance() or QCoreApplication([])`
- 清理：`tearDown()` 中务必停止实验线程、断开磁场连接

---

## 12. 常见陷阱

| 问题 | 说明 |
|------|------|
| `_SweepRunner` 有 parent | Qt 禁止 parent 对象跨线程 `moveToThread`。必须 `super().__init__(None)` |
| GUI 线程调用 `submit_sync()` | 阻塞事件循环导致死锁。GUI 只能使用 `submit()` + 信号回调 |
| VISA `list_resources()` 挂起 | 陈旧设备会导致超时。枚举始终使用短超时（500ms） |
| RALL? 数据不完整 | USB 丢包时 `len(raw) != 12288`。解析前必须验证长度 |
| OE1022D `*IDND?` 响应差异 | 不同固件版本格式不同。使用 `"SSI LIA-OE1022D" in resp` 子串匹配，而非精确匹配 |
| `origin/smb-locked_2.1.1.py` | ~60,000 行遗留代码，**不要修改**。仅作历史参考 |
| 磁场负值请求 | `FieldController.set_field()` 和 `set_current()` 会**拒绝负值**，返回 `False` |
| 零偏锁定的总电流溢出 | 开启 `lock_zero` 后，总电流 = zero_offset + recur_current，必须 ≤ MAX_CURRENT_MA |

---

## 13. 扩展指南

### 13.1 添加新 CommandType

1. 在 `core/commands.py` 的 `CommandType` 枚举中添加新成员
2. 在 `core/command_service.py` 的 `_execute()` 方法中添加路由分支
3. 如需 Agent 调用，在 `core/agent_api.py` 中添加快捷方法
4. 在 `tests/` 中添加对应测试

### 13.2 添加新设备驱动

1. 在 `instruments/` 创建驱动模块，遵循模式：
   - `connect()` / `close()` 管理生命周期
   - 所有 I/O 方法加 `threading.Lock`
   - 提供类方法 `scan_xxx()` 用于自动发现
   - 状态变更发射信号（或通过 Controller 转发）
2. 在 `core/instrument_controller.py` 中集成
3. 在 `core/commands.py` 和 `core/command_service.py` 中添加命令

### 13.3 修改安全策略

所有安全相关的数值阈值应集中在 `core/safety_policy.py` 的 `SafetyEnvelope` 中，不要分散在 GUI 或 Driver 中。

---

## 14. 参考文档

- `docs/ARCHITECTURE.md` — 完整架构设计（分层、线程模型、设计模式）
- `docs/API_REFERENCE.md` — 各类公共 API 详细说明
- `docs/DEVICE_SPECIFICATIONS.md` — 设备规格与安全规范
- `docs/CHANGELOG.md` — 版本变更日志（含 Bug 修复编号）
- `schemas/experiment_plan.json` — 实验计划 JSON Schema
