# 设备连接模块梳理总览

> 本文档梳理 ODMR Control 中**设备连接、识别、配置**相关的全部代码逻辑，供 Rust 重构参考。
> 原始代码语言：Python 3.9+（PyQt5），目标迁移：Rust（Tauri / egui / 纯 CLI 待定）。

---

## 1. 四台设备总览

| 设备 | 型号 | 厂商 | 通信协议 | 连接层 | IDN 命令 | 关键配置 |
|------|------|------|----------|--------|----------|----------|
| 微波源 | SMB100A | Rohde & Schwarz | VISA/SCPI (USBTMC/GPIB/LAN) | `pyvisa` | `*IDN?` | VISA 地址、放大器状态、功率上限 |
| 锁相放大器 | OE1022D | SSI | Serial/RS-232 (921600 baud) / USB2.0 | `pyserial` | `*IDND?` | 串口、波特率、transport (rs232/usb2) |
| 激光器 | MSL-U-532nm-300mW | CNI | Serial/RS-232 (9600 baud, **单向**) | `pyserial` | **无** | 串口、最大功率 mW、波长、SN |
| 磁场线圈 | M8812 | MAYNUO | Serial/RS-232 (9600 baud) ×3 | `pyserial` | `*IDN?` | 三轴绑定、线圈常数、零偏电流 |

---

## 2. 代码文件映射

| 职责 | 文件路径 | 说明 |
|------|----------|------|
| GUI 连接面板 | `app/gui.py` (~5200 行) | 设备连接页、状态 LED、扫描按钮、绑定保存 |
| 配置加载/保存 | `app/config_io.py` | `DEFAULT_CONFIG` 内嵌默认值 + 深度合并 |
| 命令定义 | `core/commands.py` | `CommandType` 枚举、`Command` 不可变数据类 |
| 命令执行 | `core/command_service.py` | 单线程命令队列、安全校验、信号广播 |
| 设备控制器门面 | `core/instrument_controller.py` | 管理四台设备驱动 + Worker 线程 |
| 三轴磁场门面 | `core/mag_field_controller.py` | `FieldController`：XYZ 绑定、IDN 匹配、线圈常数 |
| SMB100A 驱动 | `instruments/smb100a.py` | VISA/SCPI、状态缓存、自动扫描 |
| OE1022D 驱动 | `instruments/oe1022d.py` | ASCII 命令、RALL? 二进制采集、IDN 扫描 |
| 激光器驱动 | `instruments/laser_msl.py` | 十六进制协议、功率设置、端口扫描 |
| 磁场单轴驱动 | `instruments/magnet_coil.py` | SCPI-like 串口、电流/输出控制 |
| SMB 轮询 Worker | `workers/smb_poll_worker.py` | 100ms 状态轮询 |
| Lockin 监控 Worker | `workers/lockin_monitor_worker.py` | 94ms SNAPD? 轮询 |
| Lockin 采集 Worker | `workers/lockin_acquire_worker.py` | 50ms RALL? 高速采集 |
| 激光器轮询 Worker | `workers/laser_poll_worker.py` | 1000ms 缓存状态广播（无 I/O） |
| 磁场轮询 Worker | `workers/mag_poll_worker.py` | 500ms 电流回读 ×3 |

---

## 3. 核心架构约束（Rust 重构需保留）

1. **命令总线串行化**：所有设备写操作必须单线程串行执行，避免 VISA/Serial 并发冲突。
2. **读写分离**：Worker 线程直接读设备（Lock 保护），绕过命令队列，避免拥塞。
3. **IDN 绑定持久化**：扫描 → 识别 → 保存 `bindings` → 下次自动匹配。
4. **安全校验在命令层**：功率限制、磁场限制、负值拒绝等集中在 `CommandService`，不在驱动层。
5. **线程模型**：驱动持有 `threading.Lock`/`RLock`，Rust 中需用 `std::sync::Mutex` 或 `tokio::sync::Mutex`。
