# 命令总线层梳理

> 对应代码：`core/commands.py`、`core/command_service.py`

---

## 1. 命令定义 `CommandType`

所有设备操作封装为枚举，共 60+ 个命令类型。

### 连接相关命令

```python
class CommandType(Enum):
    # SMB100A
    SMB_CONNECT = auto()              # params: {address, timeout_ms}
    SMB_DISCONNECT = auto()

    # OE1022D
    LOCKIN_CONNECT = auto()           # params: {port, baudrate, bytesize, parity, stopbits, timeout, transport}
    LOCKIN_DISCONNECT = auto()

    # 激光器
    LASER_CONNECT = auto()            # params: {port, baudrate, bytesize, parity, stopbits, timeout}
    LASER_DISCONNECT = auto()

    # 磁场
    MAG_CONNECT_AXIS = auto()         # params: {axis, port, baudrate, coil_constant, zero_offset_mA}
    MAG_DISCONNECT_AXIS = auto()      # params: {axis}
    MAG_CONNECT_ALL = auto()          # params: {ports: {X,Y,Z}, axes: {X:{...}, ...}, baudrate}
    MAG_DISCONNECT_ALL = auto()
    MAG_SCAN_PORTS = auto()           # params: {baudrate}
    MAG_AUTO_DETECT = auto()          # params: {baudrate, bindings}
    MAG_BIND_AXIS_IDN = auto()        # params: {axis, idn, port}
```

### 命令数据类

```python
@dataclass(frozen=True)
class Command:
    cmd_type: CommandType
    params: Dict[str, Any] = field(default_factory=dict)
    request_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    source: str = "gui"   # "gui" | "agent" | "system" | "experiment"
```

---

## 2. 命令执行服务 `CommandService`

### 架构

```
GUI / Agent
    │
    ▼ submit(Command)
CommandService（独立 QThread）
    │
    │ 单线程串行队列 (_cmd_queue)
    ▼
_process_loop → _handle_command → _execute(cmd)
    │
    ▼ _deliver_result
command_completed Signal  →  GUI 回调
_sync_results / _sync_events → submit_sync 阻塞返回
```

### 生命周期

```python
def start(self):
    self._worker_thread = QThread()
    self.moveToThread(self._worker_thread)
    self._worker_thread.started.connect(self._process_loop)
    self._worker_thread.start()

def stop(self):
    self._running = False
    self._cmd_queue.put(None)   # 放入空命令唤醒线程退出
    self._worker_thread.quit()
```

### 异步提交

```python
def submit(self, cmd: Command) -> str:
    self._cmd_queue.put(cmd)
    return cmd.request_id   # GUI 用 request_id 匹配回调
```

### 同步提交（非 GUI 线程使用）

```python
def submit_sync(self, cmd: Command, timeout_ms: int = 5000) -> Tuple[bool, str, dict]:
    event = threading.Event()
    self._sync_events[cmd.request_id] = event
    self._cmd_queue.put(cmd)
    event.wait(timeout_ms / 1000.0)
    return self._sync_results.pop(cmd.request_id)
```

> **绝对禁止**：GUI 主线程调用 `submit_sync()`，会阻塞 Qt 事件循环导致死锁。

---

## 3. 命令路由 `_execute`

以连接命令为例：

### SMB_CONNECT

```python
if ct == CommandType.SMB_CONNECT:
    idn = self._ctrl.connect_smb(p["address"], p.get("timeout_ms", 10000))
    return {"idn": idn}
```

### LOCKIN_CONNECT

```python
if ct == CommandType.LOCKIN_CONNECT:
    idn = self._ctrl.connect_lockin(
        p["port"], p.get("baudrate", 921600), ...,
        p.get("transport", "rs232"),
    )
    return {"idn": idn}
```

### LASER_CONNECT

```python
if ct == CommandType.LASER_CONNECT:
    idn = self._ctrl.connect_laser(
        p["port"], p.get("baudrate", 9600), ...,
    )
    return {"idn": idn}
```

### MAG_CONNECT_AXIS

```python
if ct == CommandType.MAG_CONNECT_AXIS:
    axis = self._normalize_axis(p.get("axis", "X"))
    idn = self._ctrl.mag.connect_axis(axis, p["port"], int(p.get("baudrate", 9600)))
    self._apply_mag_axis_config(axis, p)   # 应用线圈常数、零偏
    return {"axis": axis, "idn": idn, "state": self._ctrl.mag.get_status(axis)}
```

### MAG_CONNECT_ALL

```python
if ct == CommandType.MAG_CONNECT_ALL:
    ports = dict(p.get("ports", {}))
    selected = [port for port in ports.values() if port]
    duplicates = sorted({port for port in selected if selected.count(port) > 1})
    if duplicates:
        raise SafetyError(f"重复串口已阻止: {', '.join(duplicates)}")
    results = self._ctrl.mag.connect_all(ports, int(p.get("baudrate", 9600)))
    for axis in ("X", "Y", "Z"):
        axis_cfg = p.get("axes", {}).get(axis, {})
        if self._ctrl.mag.is_connected(axis):
            self._apply_mag_axis_config(axis, axis_cfg)
    return {"results": results, "state": self._ctrl.mag.get_field_snapshot()}
```

### MAG_AUTO_DETECT

```python
if ct == CommandType.MAG_AUTO_DETECT:
    devices = self._ctrl.mag.scan_device_ports(baudrate)
    bindings = p.get("bindings") or self._mag_axis_bindings()
    matched_ports = self._ctrl.mag.match_devices_to_axes(devices, bindings)
    port_to_idn = {item.get("port"): item.get("idn", "") for item in devices}
    matched = {
        axis: {"port": port or "", "idn": port_to_idn.get(port, "")}
        for axis, port in matched_ports.items()
    }
    return {"devices": devices, "matched": matched}
```

---

## 4. 安全校验

### 功率限制

```python
def _check_power_limit(self, power_dbm: float):
    amplifier_installed = self._config.get("smb", {}).get("amplifier_installed", True)
    max_dbm = 10.0 if amplifier_installed else 25.0
    if power_dbm > max_dbm:
        raise SafetyError(f"功率 {power_dbm} dBm 超过安全限制 {max_dbm} dBm")
```

### 激光功率限制

```python
def _check_laser_power_limit(self, power_mw: int):
    max_mw = self._config.get("laser", {}).get("max_power_mw", 150)
    if power_mw < 0 or power_mw > max_mw:
        raise SafetyError(f"激光功率 {power_mw} mW 超出安全范围 [0, {max_mw}] mW")
```

### 资源占用检查

```python
_LEASED_COMMAND_RESOURCES = {
    CommandType.SMB_SET_FREQUENCY: "microwave",
    CommandType.SMB_SET_POWER: "microwave",
    CommandType.LOCKIN_START_ACQUIRE: "lockin",
    CommandType.LASER_SET_POWER: "laser",
    CommandType.MAG_SET_FIELD: "magnetic_field",
    # ...
}

def _enforce_resource_lease(self, cmd: Command):
    if cmd.source == "experiment":
        return
    resource = self._LEASED_COMMAND_RESOURCES.get(cmd.cmd_type)
    if resource and self._resource_manager.is_leased(resource):
        raise SafetyError(f"{resource} is leased by active run ...")
```

> 实验运行时，手动命令被阻止，避免 GUI 与自动化冲突。

---

## 5. Rust 重构要点

| 方面 | 建议 |
|------|------|
| 命令枚举 | `enum CommandType { SmbConnect, SmbDisconnect, LockinConnect, ... }` |
| 命令对象 | `struct Command { cmd_type: CommandType, params: serde_json::Value, request_id: String, source: Source }` |
| 队列 | `tokio::sync::mpsc::unbounded_channel::<Command>()` |
| 执行循环 | `tokio::spawn(async { while let Some(cmd) = rx.recv().await { ... } })` |
| 异步结果 | `tokio::sync::oneshot::Sender<Result<serde_json::Value, Error>>` 随命令附带 |
| 同步接口 | `async` 命令 + `tokio::runtime::Runtime::block_on()`（仅限非主线程） |
| 安全校验 | 在命令路由层统一校验，驱动层仅做硬件范围校验 |
| 资源租约 | `HashMap<String, String>` 记录 `resource -> run_id`，实验开始时 lease，结束时 release |
| 错误类型 | 自定义 `enum Error { SafetyError(String), DeviceError(String), Timeout } }` |
| 信号广播 | `tokio::sync::broadcast` 或 `tokio::sync::watch` 替代 Qt 信号 |
