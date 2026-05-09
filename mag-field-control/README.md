# 磁场控制系统 (Mag Field Control)

三轴 Helmholtz 线圈磁场控制软件，基于 Python/PyQt5。该目录是 `odmr-control` 工程中的独立子项目，当前放在单独分支 `mag-field-control-v1`，避免影响主线 ODMR 控制程序。

软件面向一组三轴磁场发生器：硬件本质上由 X/Y/Z 三路可控电流源驱动线圈，通过设置各轴输出电流复现目标磁场。当前版本已按原 `SimplePowerController.exe` 的串口协议和电流/磁场语义做过交叉复核。

## 当前边界

- 软件内部和 GUI 使用 `mA`、`nT`。
- 串口设备侧 `CURR` 使用 `A`。
- 当前策略按单极性输出处理：负磁场、负电流请求会被阻止并记录错误。
- 回读磁场是由电源回读电流推算，不是外部磁强计实测。
- 本目录不包含原 exe、安装包或反编译文件，只保留实现和文档化后的硬件语义。

## 快速启动

```bash
cd mag-field-control

# macOS
./run_mac.command

# Windows
run_win.bat
```

推荐环境：

- macOS 本地开发：`odmr-gui` conda 环境
- Windows TP14g2 联调：`D:\anaconda3\python.exe` 或桌面快捷方式

直接运行：

```bash
python main.py
```

测试：

```bash
python -m unittest discover -v
python -m compileall instruments core app workers data tests main.py
```

## 硬件协议

通过 RS-232 串口（9600 baud, 8N1）发送 SCPI 风格指令：

| 命令 | 说明 |
|------|------|
| `*IDN?` | 查询设备标识 |
| `CURR {amp:.5f}` | 设置输出总电流，设备侧单位为 A |
| `MEAS:CURR?` | 测量当前总电流，设备返回单位为 A |
| `OUTP 0/1` | 关闭/开启输出 |
| `VOLT {v:.0f}` | 设置电压限值 |
| `SYST:REM` | 切换到远程模式 |
| `SYST:LOC` | 切换到本地模式 |

每条命令以 `\n` 结尾。软件内部和界面统一使用 mA，串口下发时将 mA 转为 A；读取 `MEAS:CURR?` 时将设备返回的 A 转回 mA。

连接时控制器会进入远程模式，设置电压限值，并将输出电流清零后关闭输出。后台轮询线程负责周期性执行 `MEAS:CURR?`，同时串行处理 GUI 或序列引擎提交的设置命令。

### 磁场换算

```
B(nT) = RecurCurrent(mA) × CoilConstant
TotalCurrent(mA) = ZeroOffset(mA) + RecurCurrent(mA)   # 锁定零场时
```

其中 `RecurCurrent` 是复现磁场电流，`ZeroOffset` 是零场偏置电流。未锁定零场时，输出总电流保持为零场偏置；锁定零场后，输出总电流为零场偏置与复现电流之和。

默认线圈常数 (nT/mA):
- X: 143.26
- Y: 141.77
- Z: 156.15

默认值来自原安装包 `para.xml`。反编译确认原程序使用同一组关系式:

- 目标复现电流: `RecurCurrent(mA) = TargetField(nT) / CoilConstant`
- 回读估算复现电流: `EstimatedRecurCurrent(mA) = ReadbackTotalCurrent(mA) - ZeroOffset(mA)`
- 回读估算磁场: `EstimatedField(nT) = EstimatedRecurCurrent(mA) × CoilConstant`

`EstimatedField` 是由电源电流回读推算得到，不是外部磁强计实测值。

### 输出状态语义

| 状态 | 设备应输出的总电流 |
|------|--------------------|
| 输出关闭 | 先下发 `CURR 0`，再 `OUTP 0` |
| 输出开启、未锁零 | `ZeroOffset` |
| 输出开启、锁定零场 | `ZeroOffset + RecurCurrent` |

`RecurCurrent` 是目标复现磁场对应的电流缓存。修改目标磁场时，若输出已开启且已锁零，会立即下发新的总电流；若未锁零，只更新缓存，不把复现电流叠加到输出。

### 安全边界

- `MAX_CURRENT_MA = 5000.0`
- `set_field(axis, nT)` 拒绝负 `nT`
- `set_current(axis, mA)` 拒绝负 `mA`
- `set_zero_offset(axis, mA)` 拒绝负零偏和超过上限的零偏
- 总输出电流超过 5000 mA 时拒绝下发
- 单轴底层驱动仍保留 `abs(total_mA) / 1000` 作为兼容保护，但用户可见路径会先阻止负值

## 架构

```
mag-field-control/
├── main.py                     # 入口
├── app/gui.py                  # 工业风格 GUI (西门子 WinCC 风格)
├── core/
│   ├── field_controller.py     # 统一门面: 三轴控制、轮询、信号
│   ├── sequence_engine.py      # 序列自动化引擎
│   ├── vector_field.py         # 球坐标 ↔ 笛卡尔转换
│   └── presets.py              # 磁场预设管理
├── instruments/
│   └── magnet_coil.py          # 单轴串口驱动
├── workers/
│   └── poll_worker.py          # 后台轮询线程
├── data/
│   └── recorder.py             # CSV 数据记录
├── presets/                    # 用户预设 (JSON)
├── Errors/                     # 错误日志
└── config.xml                  # 系统配置
```

### 层级关系

```
GUI / SequenceEngine
    ↓
FieldController (统一 API 门面)
    ↓
MagnetCoilController × 3 (串口驱动)
    ↑
AxisPollWorker × 3 (后台轮询 + 命令队列)
```

### 主要模块

| 模块 | 职责 |
|------|------|
| `main.py` | PyQt5 应用入口 |
| `app/gui.py` | 主窗口、页面、状态栏、配置读写、可视化 |
| `core/field_controller.py` | 三轴控制门面，负责零偏、复现电流、输出状态、轮询线程 |
| `instruments/magnet_coil.py` | 单轴串口驱动，封装 SCPI 文本协议 |
| `workers/poll_worker.py` | 后台轮询与命令队列执行 |
| `core/vector_field.py` | 球坐标和笛卡尔磁场转换 |
| `core/presets.py` | JSON 预设保存、加载和内置预设 |
| `core/sequence_engine.py` | 序列执行、暂停、单步、CSV 记录 |
| `data/recorder.py` | 手动记录 CSV 写入 |
| `tests/test_hardware_core.py` | fake serial、控制语义、记录语义、配置兼容测试 |

## 操作流程

### 1. 连接

1. 选择各轴对应的串口号和波特率
2. 点击"连接"或"全部连接"
3. 设备自动切换到远程模式并设置电压 75V

### 2. 零场校准

1. 输入各轴的零偏电流值
2. 点击"设置"应用零偏
3. 点击"开启全部输出"
4. 如需锁定零场，点击"锁定全部零场"

### 3. 复现磁场

1. 输入目标磁场值 (nT)，系统自动计算对应电流
2. 也可直接输入电流值 (mA)
3. 点击"设置"应用
4. 实测电流值由轮询自动更新
5. 回读估算磁场仅在输出开启且锁定零场后显示；未锁定零场时不会把零偏电流换算成复现磁场

### 4. 硬件自检

工具栏中的“硬件自检”会对所有已连接轴发送 `*IDN?`，结果写入日志页。未连接轴显示“未连接”。

### 5. 参数设置

参数页可修改：

- 各轴线圈常数 `CoilConstant`
- 各轴零偏电流 `ZeroOffset`
- 各轴电压限值
- 数据保存目录
- 轮询间隔 `PollIntervalMs`

保存配置后，已连接轴的轮询 worker 会按新间隔重启，但不会断开串口、不会改变输出状态，也不会清空目标电流缓存。

## 序列自动化

可编程控制磁场序列，支持：

- 多步骤序列（每步设定 X/Y/Z 场值）
- 可配置建立时间 (settle time) 和保持时间
- 三种触发方式: immediate / delay / manual
- 循环执行（有限次或无限循环）
- 完成后自动归零
- 实时数据记录到 CSV
- CSV 记录显式区分目标磁场、回读总电流、估算复现电流和估算磁场

序列以 JSON 格式保存/加载。

### 序列记录

序列开始时会自动生成 CSV 文件，记录路径为：

```text
{DataSaveDir}/seq_{sequence_name}_{YYYYMMDD_HHMMSS}.csv
```

每步在 settle 后记录一次，hold 结束时再记录一次。记录字段和手动记录保持同一套语义，避免把零偏电流错误解释为复现磁场。

序列结束后，如果勾选“完成后归零”，会调用三轴 `set_field(..., 0)`，即清空复现磁场目标；输出状态本身不被强制关闭。

## 向量场工具

NV 中心实验常用球坐标描述磁场方向。内置计算器支持：

- 球坐标 (B, θ, φ) → 笛卡尔 (Bx, By, Bz)
- 笛卡尔 → 球坐标反向计算
- XY/XZ/YZ 三视图方向预览
- 目标场与回读估算场双向量对比
- 只读计算参数面板: 线圈常数、零偏、目标复现电流、回读总电流、回读估算复现电流、电流余量
- 一键填入控制面板

物理学约定: θ 为与 Z 轴的极角 (0°–180°)，φ 为 XY 平面内与 X 轴的方位角 (0°–360°)。

示例校验：

```text
B = 10000 nT, θ = 45°, φ = 90°
Bx ≈ 0 nT
By ≈ 7071 nT
Bz ≈ 7071 nT
```

可视化页显示两组向量：

- 目标复现场：来自手动输入、向量场计算器或预设加载
- 回读估算场：来自 `MEAS:CURR?` 回读总电流和零偏/线圈常数推算

未锁零或输出未开启时，回读估算场不可用，界面显示为空。

## 预设管理

内置预设:
- 零场
- 地磁补偿 (北京)
- 沿 X/Y/Z 轴 10μT

可自定义保存/加载任意磁场配置，预设存储在 `presets/` 目录。

负磁场预设允许加载和预览，但当前单极性输出策略会阻止下发。加载负场预设时，手动控制页会显示提示，日志也会记录说明。

## 安全特性

- 电流安全限值 0–5000 mA（负值请求会被阻止）
- 紧急停止按钮（一键关闭所有输出）
- 序列执行完自动归零
- 线程安全的电流缓存（`threading.Lock`）
- 可中断的延时机制（停止序列无需等待）
- 负场/负电流不会静默变成正向输出
- 未锁零时不显示回读估算复现磁场，避免误读零偏电流

## 配置文件

`config.xml` 兼容原 `para.xml` 格式：

```xml
<Root>
  <Commports>
    <PortX Port="COM3" BaudRate="9600"/>
    <PortY Port="COM4" BaudRate="9600"/>
    <PortZ Port="COM5" BaudRate="9600"/>
  </Commports>
  <CoilConstant X="143.26" Y="141.77" Z="156.15"/>
  <ZeroOffset X="0.00000" Y="0.00000" Z="0.00000"/>
  <DataSaveDir>./data</DataSaveDir>
  <PollIntervalMs>500</PollIntervalMs>
</Root>
```

`PollIntervalMs` 为三轴电流轮询间隔，范围 100–5000 ms。修改后会重启已连接轴的轮询 worker，但不会改变输出状态、目标电流或命令队列。

## CSV 数据语义

手动记录和序列记录使用同一套字段语义：

- `*_target_field_nT`: 目标复现磁场
- `*_total_current_mA`: 电源回读总电流
- `*_estimated_recur_current_mA`: 由回读总电流减去零偏得到的估算复现电流
- `*_estimated_field_nT`: 由估算复现电流和线圈常数计算的磁场

当输出未开启或未锁定零场时，`estimated_*` 字段留空，表示当前无法从总电流可靠估算复现磁场。

## 测试说明

测试覆盖范围：

- 串口文本协议：`CURR` 使用 A、`\n` 终止符、`OUTP`/`VOLT`/`SYST:REM`/`SYST:LOC`
- `MEAS:CURR?` A → mA 转换
- 零偏、复现电流、锁零状态下的总电流叠加
- 负电流/负磁场拒绝下发
- 5000 mA 上限
- 回读估算磁场只在输出开启且锁零时可用
- `PollIntervalMs` 旧配置兼容和保存/读取
- 手动记录和序列记录 CSV 表头与空值语义

macOS 示例：

```bash
/Users/erictseng/miniconda3/envs/odmr-gui/bin/python -m unittest discover -v
/Users/erictseng/miniconda3/envs/odmr-gui/bin/python -m compileall instruments core app workers data tests main.py
```

Windows TP14g2 示例：

```bat
D:\anaconda3\python.exe -m unittest discover -v
D:\anaconda3\python.exe -m compileall instruments core app workers data tests main.py
```

## Windows 联调记录

TP14g2 局域网地址：

```text
192.168.199.203
```

远端部署路径：

```text
C:\Users\Piwei Tseng\Documents\codex_git\mag-field-control
```

桌面快捷方式：

```text
C:\Users\Piwei Tseng\Desktop\磁场控制系统.lnk
```

快捷方式目标：

```text
Target: D:\anaconda3\pythonw.exe
Arguments: main.py
WorkingDirectory: C:\Users\Piwei Tseng\Documents\codex_git\mag-field-control
```

已知远端环境：

- 默认 `python` 为 Python 3.14，缺少 PyQt5/pyserial，不推荐用于此项目
- `D:\anaconda3\python.exe` 已安装 PyQt5 和 pyserial
- GUI offscreen 实例化时可能输出 Qt 字体目录警告，不影响测试和启动

## 开发约定

- 本分支只承载 `mag-field-control` 子项目，不合入 `main`
- 未来若要并回 odmr-control 主线，建议先保留为子包或插件式入口，避免覆盖现有 ODMR GUI
- 涉及硬件协议的修改必须先补 fake serial 测试
- 涉及磁场/电流换算的修改必须同时更新 README 和 CSV 字段语义
- 不要把原 exe、安装包、反编译大文件或运行日志提交到公开仓库

## 已知限制与后续方向

- 当前不支持双极性电源或反向磁场实际下发
- 回读估算依赖电源电流读数，不等价于空间磁场实测
- GUI 视觉仍是 PyQt5 工业风格，尚未和 odmr-control 主界面统一
- 序列自动化已经可记录 CSV，但尚未和 ODMR 采集流程联动
- 后续可以把 `FieldController` 封装成 odmr-control 的一个硬件服务，供实验流程按需调用

## 依赖

- Python 3.9+
- PyQt5
- pyserial
