# Rust 重构注意事项与建议

---

## 1. 跨平台串口通信

### 推荐 crate

| 功能 | Crate | 说明 |
|------|-------|------|
| 串口 I/O | `serialport` | 跨平台，支持 VID/PID/SN 读取 |
| VISA | `visa-rs` / 自建 FFI | NI-VISA 无官方 Rust binding，需 `bindgen` 生成 |
| USB 信息 | `serialport` 内置 | `SerialPortInfo.port_type` 可匹配 `UsbPort { vid, pid, serial_number }` |

### VISA 特殊处理

- `list_resources()` 可能因陈旧设备挂起，Rust 中需设短超时（500ms）并妥善处理 `Timeout`。
- VISA 资源非 `Send`，建议用 dedicated I/O thread，通过 channel 与主逻辑通信。

---

## 2. 线程模型映射

### Python 原模型

| 线程 | 职责 |
|------|------|
| Main Thread | GUI + QTimer 刷新 |
| CommandService Thread | 命令队列串行执行 |
| SMBPollWorker | 100ms 轮询 |
| LockinMonitorWorker | 94ms 轮询 |
| LockinAcquireWorker | 50ms 高速采集 |
| LaserPollWorker | 1000ms 缓存广播（无 I/O） |
| AxisPollWorker ×3 | 500ms 电流回读 |

### Rust 建议模型

```
main thread (tokio runtime)
    ├── tauri/egui GUI task
    ├── command executor task (mpsc receiver)
    ├── smb poller task (interval 100ms)
    ├── lockin monitor task (interval 94ms)
    ├── lockin acquire task (interval 50ms, optional)
    ├── laser broadcaster task (interval 1000ms)
    ├── mag X poller task (interval 500ms)
    ├── mag Y poller task (interval 500ms)
    └── mag Z poller task (interval 500ms)
```

- 用 `tokio::time::interval` 实现定时轮询。
- 驱动层用 `Arc<tokio::sync::Mutex<T>>` 或 `std::sync::Mutex<T>`（视阻塞程度而定）。
- **注意**：`serialport` 的 `read` 是阻塞的，若在 async task 中直接调用，需用 `tokio::task::spawn_blocking`。

---

## 3. 状态管理

### 建议结构

```rust
use std::sync::Arc;
use tokio::sync::RwLock;

#[derive(Default, Clone)]
pub struct AppState {
    pub smb: SmbState,
    pub lockin: LockinState,
    pub laser: LaserState,
    pub magnetic_field: MagneticFieldState,
}

pub type SharedState = Arc<RwLock<AppState>>;
```

### 状态变更广播

- GUI 订阅：用 `tokio::sync::watch::Receiver<AppState>` 或 `tokio::sync::broadcast`。
- 避免频繁 clone 大状态：可对每个设备单独用 `watch` channel。

---

## 4. 配置序列化

```rust
use serde::{Deserialize, Serialize};

#[derive(Serialize, Deserialize, Default)]
pub struct Config {
    #[serde(default)]
    pub smb: SmbConfig,
    #[serde(default)]
    pub lockin: LockinConfig,
    #[serde(default)]
    pub laser: LaserConfig,
    #[serde(default)]
    pub magnetic_field: MagneticFieldConfig,
}

#[derive(Serialize, Deserialize)]
pub struct MagneticFieldConfig {
    pub baudrate: u32,
    pub poll_interval_ms: u64,
    pub bindings: HashMap<String, AxisBinding>,
    pub axes: HashMap<String, AxisConfig>,
}

#[derive(Serialize, Deserialize)]
#[serde(untagged)]
pub enum AxisBinding {
    Legacy(String),                 // 旧格式：纯 IDN 字符串
    Modern { idn: String, port: String },
}
```

- `serde(untagged)` 实现旧格式兼容。
- 保存时统一写入新格式，避免后续兼容负担。

---

## 5. 磁场控制核心算法（必须精确复现）

### 电流计算

```rust
fn desired_total_current(
    zero_offset: f64,
    recur_current: f64,
    lock_zero: bool,
) -> f64 {
    if lock_zero {
        zero_offset + recur_current
    } else {
        zero_offset
    }
}
```

### 校验链

```rust
fn validate(&self, axis: &str, new_zero: f64, new_recur: f64, new_lock: bool) -> Result<(), Error> {
    let total = desired_total_current(new_zero, new_recur, new_lock);
    if total < 0.0 {
        return Err(Error::NegativeCurrent(total));
    }
    if total > MAX_CURRENT_MA {
        return Err(Error::CurrentOverflow { total, max: MAX_CURRENT_MA });
    }
    Ok(())
}
```

### 命令执行顺序（连接时）

```rust
async fn connect_axis(&self, axis: &str, port: &str, baudrate: u32) -> Result<String, Error> {
    let idn = controller.connect(port, baudrate).await?;
    controller.set_remote().await?;
    controller.set_voltage(75.0).await?;
    controller.set_current(0.0).await?;
    controller.set_output(false).await?;
    self.start_poll(axis).await;
    Ok(idn)
}
```

---

## 6. 激光器十六进制协议

```rust
fn build_set_power_cmd(power_mw: u16) -> [u8; 7] {
    let high = ((power_mw >> 8) & 0xFF) as u8;
    let low = (power_mw & 0xFF) as u8;
    let chk = (0x05u8.wrapping_add(0x01).wrapping_add(high).wrapping_add(low));
    [0x55, 0xAA, 0x05, 0x01, high, low, chk]
}
```

---

## 7. 错误处理策略

```rust
#[derive(Debug, thiserror::Error)]
pub enum Error {
    #[error("设备未连接")]
    NotConnected,
    #[error("串口错误: {0}")]
    Serial(#[from] serialport::Error),
    #[error("安全限制: {0}")]
    Safety(String),
    #[error("负值请求被阻止: {value} {unit}")]
    NegativeValue { value: f64, unit: String },
    #[error("电流溢出: {total} mA > {max} mA")]
    CurrentOverflow { total: f64, max: f64 },
    #[error("VISA 错误: {0}")]
    Visa(String),
    #[error("命令超时")]
    Timeout,
}
```

---

## 8. 测试策略

| 层级 | Rust 建议 |
|------|----------|
| 驱动层 | Mock serial port with `tokio::io::DuplexStream` 或 trait abstraction |
| 命令服务 | 纯 async 测试，`tokio::test` + in-memory fake drivers |
| GUI 层 | `egui` 可用 `eframe` 做 headless 测试，或分离 view model 单独测试 |
| 磁场算法 | 单元测试覆盖 zero_lock、电流校验、负值拒绝 |
| 配置兼容 | `serde_json` 反序列化测试，验证旧格式 bindings 能正确解析 |

---

## 9. 文件清单（供 Rust 重构参考）

```
docs_rust_refactor/
├── 00_overview.md              ← 总览
├── 01_config_system.md         ← 配置系统
├── 02_smb100a_connection.md    ← SMB100A 连接与 IDN
├── 03_oe1022d_connection.md    ← OE1022D 连接
├── 04_laser_connection.md      ← 激光器连接与功率
├── 05_magnetic_field_connection.md ← 三轴线圈连接
├── 06_gui_connection_logic.md  ← GUI 连接面板逻辑
├── 07_command_bus.md           ← 命令总线层
└── 08_rust_migration_notes.md  ← Rust 重构注意事项
```

> 所有文档均基于 `odmr-control` 当前代码（V2.1.0+）梳理，后续代码变更时需同步更新。
