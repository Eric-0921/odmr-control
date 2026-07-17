# 三轴亥姆霍兹线圈连接梳理

> 对应代码：`core/mag_field_controller.py`、`instruments/magnet_coil.py`、`app/gui.py`

---

## 1. 硬件架构

| 组件 | 说明 |
|------|------|
| 单轴电源 | MAYNUO M8812（每轴一台） |
| 通信 | RS-232 ×3，独立串口 |
| 默认波特率 | 9600 |
| 驱动 | `MagnetCoilController`（单轴）×3 |
| 门面 | `FieldController`（统一管理三轴） |
| 轮询 Worker | `AxisPollWorker` ×3，每轴独立 QThread |

---

## 2. 单轴驱动 `MagnetCoilController`

### 核心属性

```python
class MagnetCoilController:
    MAX_CURRENT_MA: float = 5000.0   # 硬件电流上限 5A

    def __init__(self, axis: str = "X"):
        self._axis = axis
        self._coil_constant: float = 143.26   # nT/mA，默认标定值
        self._zero_offset: float = 0.0         # mA，背景补偿电流
        self._lock_zero: bool = False          # 是否锁定零偏
        self._output_on: bool = False
        self._cached_current: float = 0.0      # 上次轮询读数
```

### 连接流程

```python
def connect(self, port: str, baudrate: int = 9600, timeout: float = 0.1) -> str:
    self._serial = serial.Serial(
        port=port, baudrate=baudrate, bytesize=8, parity="N",
        stopbits=1, timeout=timeout, dsrdtr=False,
    )
    self._serial.dtr = True
    self._serial.reset_input_buffer()
    self._serial.reset_output_buffer()
    return self.identify()   # ← 发送 *IDN?
```

### IDN 响应格式

```
MAYNUO,M8812,<serial>,V2.7
```

示例：
```
MAYNUO,M8812,080020960220402003,V2.7
```

### 断开流程

```python
def close(self):
    if self._serial is not None:
        try:
            if self._output_on:
                self.set_current(0.0)
                self.set_output(False)
            self.set_local()          # 切回本地控制模式
        except: pass
        finally:
            self._serial.close()
            self._serial = None
            self._output_on = False
```

### 低层 I/O

```python
def exchange(self, command: str, timeout: float = 0.1) -> str:
    tx_data = (command + "\n").encode("ascii")
    self._serial.write(tx_data)
    self._serial.flush()
    time.sleep(0.02)              # 20ms 等待设备响应
    old_timeout = self._serial.timeout
    self._serial.timeout = timeout
    try:
        raw = self._serial.readline()
    finally:
        self._serial.timeout = old_timeout
    return raw.decode("ascii", errors="replace").strip()
```

> **注意**：`time.sleep(0.02)` 在写入后固定等待 20ms，再读取响应。

---

## 3. 三轴门面 `FieldController`

### 初始化

```python
AXES = ("X", "Y", "Z")

class FieldController(QObject):
    def __init__(self, parent=None):
        self._controllers = {a: MagnetCoilController(a) for a in AXES}
        self._queues = {a: queue.Queue() for a in AXES}
        self._workers = {a: None for a in AXES}
        self._threads = {a: None for a in AXES}
        self._latest_current = {a: 0.0 for a in AXES}   # 实测总电流
        self._recur_current = {a: 0.0 for a in AXES}    # 用户请求复现电流
        self._poll_interval_ms = 500
```

### 单轴连接

```python
def connect_axis(self, axis: str, port: str, baudrate: int = 9600) -> str:
    ctrl = self._controllers[axis]
    idn = ctrl.connect(port, baudrate)
    ctrl.set_remote()           # 切换远程模式
    ctrl.set_voltage(75.0)      # 设置电压 75V
    ctrl.set_current(0.0)       # 电流归零
    ctrl.set_output(False)      # 关闭输出
    self._start_poll(axis)      # 启动轮询 Worker
    self.connection_changed.emit(axis, True)
    return idn
```

### 单轴断开

```python
def disconnect_axis(self, axis: str) -> None:
    self._stop_poll(axis)
    self._controllers[axis].close()
    self._latest_current[axis] = 0.0
    self._recur_current[axis] = 0.0
    self.connection_changed.emit(axis, False)
```

---

## 4. IDN 绑定与自动匹配

### 绑定配置格式

兼容两种格式：

```python
# 旧格式（纯字符串）
"X": "MAYNUO,M8812,080020960220402003,V2.7"

# 新格式（含 port）
"X": {"idn": "MAYNUO,M8812,...,V2.7", "port": "COM1"}
```

### 标准化方法

```python
@staticmethod
def _normalize_binding(value):
    if value is None:
        return {"idn": "", "port": ""}
    if isinstance(value, dict):
        return {"idn": str(value.get("idn", "")), "port": str(value.get("port", ""))}
    return {"idn": str(value), "port": ""}
```

### 自动匹配逻辑

```python
@staticmethod
def match_devices_to_axes(detected, bindings) -> Dict[str, Optional[str]]:
    result = {a: None for a in AXES}
    normalized = {a: _normalize_binding(bindings.get(axis, "")) for a in AXES}
    has_bindings = any(normalized[a]["idn"] for a in AXES)

    if not has_bindings:
        # Fallback: 按索引顺序分配
        for i, axis in enumerate(AXES):
            if i < len(detected):
                result[axis] = detected[i]["port"]
        return result

    # Match by IDN
    idn_to_port = {d["idn"]: d["port"] for d in detected}
    for axis in AXES:
        bound_idn = normalized[axis]["idn"]
        if bound_idn and bound_idn in idn_to_port:
            result[axis] = idn_to_port[bound_idn]
    return result
```

### 扫描与检测

```python
@staticmethod
def scan_device_ports(baudrate=9600) -> List[Dict[str, str]]:
    """返回 [{"port": "COM3", "idn": "MAYNUO,M8812,..."}, ...]"""
    for info in list_ports.comports():
        port = info.device
        sp = serial.Serial(port, baudrate, bytesize=8, parity="N", stopbits=1, timeout=0.1, dsrdtr=False)
        sp.dtr = True
        sp.reset_input_buffer()
        sp.reset_output_buffer()
        time.sleep(0.05)
        sp.write(b"*IDN?\n")
        sp.flush()
        time.sleep(0.1)
        resp = sp.readline().decode("ascii", errors="replace").strip()
        sp.close()
        if resp and "," in resp:
            results.append({"port": port, "idn": resp})
```

---

## 5. 线圈常数与零偏配置

### 配置键

```json
{
  "magnetic_field": {
    "coil_constant": {"X": 156.15, "Y": 143.26, "Z": 141.77},
    "zero_offset": {"X": 0.0, "Y": 0.0, "Z": 0.0},
    "axes": {
      "X": {"port": "COM1", "coil_constant": 156.15, "zero_offset_mA": 0.0},
      "Y": {"port": "COM2", "coil_constant": 143.26, "zero_offset_mA": 0.0},
      "Z": {"port": "COM3", "coil_constant": 141.77, "zero_offset_mA": 0.0}
    }
  }
}
```

### 单位换算

```python
UNIT_TO_NT = {
    "nT": 1.0,
    "μT": 1000.0, "uT": 1000.0,
    "mT": 1e6,
    "T": 1e9,
    "G": 100000.0,
    "Oe": 100000.0,
}
```

### 磁场 ↔ 电流换算

```python
def set_field(self, axis: str, nT: float) -> bool:
    if nT < 0:
        return False          # 拒绝负磁场
    if ctrl.coil_constant <= 0:
        return False
    ma = nT / ctrl.coil_constant
    return self.set_current(axis, ma)

def get_field(self, axis: str) -> float:
    return self._recur_current[axis] * self._controllers[axis].coil_constant
```

---

## 6. 零偏锁定（Zero Lock）模型

这是磁场控制的核心业务逻辑，Rust 重构必须精确复现。

### 电流组成

```
total_current = zero_offset + recur_current   (当 lock_zero=True 时)
total_current = zero_offset                   (当 lock_zero=False 时)
```

### 关键规则

| 操作 | 行为 |
|------|------|
| `set_zero_offset(mA)` | 设置背景补偿电流 |
| `lock_zero(True)` | 开启后，输出电流 = zero_offset + recur_current |
| `set_current(mA)` | 设置复现电流（reproduce current） |
| `set_output(True)` | 输出总电流到硬件 |
| `capture_background_as_zero()` | 回读当前电流并设为 zero_offset |
| `prepare_zero_lock()` | 开启输出 + 捕获背景 + 锁定零偏 |

### 总电流校验

```python
def _validate_total_current(self, axis, *, zero_offset=None, recur_current=None, locked=None):
    ctrl = self._controllers[axis]
    total = self._desired_total_current(axis, zero_offset=zero_offset, recur_current=recur_current, locked=locked)
    if total < 0:
        return False
    if total - ctrl.MAX_CURRENT_MA > 0.001:
        return False
    return True
```

> **注意**：所有电流请求（`set_current`, `set_zero_offset`, `lock_zero`, `set_output`）都会触发总电流校验，超出 5000mA 上限则拒绝。

---

## 7. 负值拒绝

```python
def set_field(self, axis: str, nT: float) -> bool:
    if nT < 0:
        self._reject(axis, f"负磁场请求被阻止: {nT:.2f} nT")
        return False
    ...

def set_current(self, axis: str, mA: float) -> bool:
    if mA < 0:
        self._reject(axis, f"负电流请求被阻止: {mA:.5f} mA")
        return False
    ...

def set_zero_offset(self, axis: str, mA: float) -> bool:
    if mA < 0:
        self._reject(axis, f"负零偏电流请求被阻止: {mA:.5f} mA")
        return False
    ...
```

> **AGENTS.md 强调**：`FieldController.set_field()` 和 `set_current()` 会**拒绝负值**，返回 `False`。

---

## 8. GUI 磁场连接面板

### Connection Tab 布局

| 列 | 内容 |
|----|------|
| 轴 / Axis | X / Y / Z |
| 串口 / Port | QComboBox (Editable) |
| IDN Binding | QLabel（显示已绑定 IDN） |
| 线圈常数 (nT/mA) | QLineEdit |
| 零偏 (mA) | QLineEdit |
| 状态 | QLabel（Connected/Disconnected） |
| 连接按钮 | QPushButton（切换单轴连接） |

### 操作按钮

| 按钮 | 功能 |
|------|------|
| Scan Ports | 扫描所有串口 |
| Auto Detect | 自动扫描 + IDN 匹配 |
| Connect All | 连接全部三轴 |
| Disconnect All | 断开全部 |

### 单轴连接参数

```python
def _toggle_mag_axis(self, axis: str):
    if self._ctrl.mag.is_connected(axis):
        submit(CommandType.MAG_DISCONNECT_AXIS, {"axis": axis})
    else:
        params = {
            "axis": axis,
            "port": controls["port"].currentText().strip(),
            "baudrate": 9600,
            "coil_constant": float(controls["coil"].text()),
            "zero_offset_mA": float(controls["zero"].text()),
        }
        submit(CommandType.MAG_CONNECT_AXIS, params)
```

---

## 9. Rust 重构要点

| 方面 | 建议 |
|------|------|
| 串口库 | `serialport` crate，三轴各自独立 `Box<dyn SerialPort>` |
| 门面模式 | `MagneticFieldController` 持有 `HashMap<String, AxisController>` |
| IDN 绑定 | `HashMap<String, AxisBinding>`，加载时做旧格式兼容 |
| 自动匹配 | 扫描 → 发 `*IDN?\n` → 按 `bindings[idn]` 匹配 → 未匹配则按索引 fallback |
| 线圈常数 | `f64`，必须 > 0，连接时从配置应用到单轴驱动 |
| 零偏锁定 | 状态机：`enum ZeroLockState { Unlocked, Locked { zero_offset: f64, recur_current: f64 } }` |
| 电流校验 | 每次修改 `zero_offset` / `recur_current` / `lock_zero` / `output_on` 时计算 total，与 5000mA 比较 |
| 负值拒绝 | `set_field` / `set_current` / `set_zero_offset` 入参 `nT < 0` 或 `mA < 0` 直接返回 `Err` |
| 轮询线程 | 每轴独立 `tokio::task` 或 `std::thread`，间隔 500ms，回读 `MEAS:CURR?` |
| 队列模型 | 每轴一个 `mpsc` channel，`AxisPollWorker` 从 channel 取命令串行执行 |
| 急停验证 | `verify_emergency_stop`：回读 `OUTP?` 和 `MEAS:CURR?`，确认 `output_on == false && current < 1.0mA` |
