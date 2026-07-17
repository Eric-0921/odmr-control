# 激光器连接与功率设置梳理

> 对应代码：`instruments/laser_msl.py`、`app/gui.py`

---

## 1. 通信协议

| 项目 | 值 |
|------|-----|
| 型号 | MSL-U-532nm-300mW (CNI) |
| 库 | `pyserial` |
| 波特率 | 9600 |
| 数据位 | 8 |
| 校验 | N |
| 停止位 | 1 |
| 默认超时 | 1.0s |
| 通信方向 | **单向发送**，设备无查询响应 |
| 协议格式 | **十六进制指令** |

---

## 2. 连接流程

```python
def connect(self, port, baudrate=9600, bytesize=8, parity="N",
            stopbits=1, timeout=1.0) -> str:
    self._serial = serial.Serial(port, baudrate, bytesize, parity, stopbits, timeout=timeout)
    self._serial.reset_input_buffer()
    self._serial.reset_output_buffer()
    # 激光器无 IDN 命令，返回配置中的标识
    idn = f"MSL-U Laser,SN:{self._serial_number},Model:{self._model}"
    return idn
```

> **关键特点**：激光器**没有 IDN 响应**。连接时发送的 `idn` 字符串完全由软件构造，基于配置中的 `serial_number` 和 `model`。

### 断开流程

```python
def close(self):
    with self._lock:
        if self._serial is not None:
            try:
                self._serial.write(self._CMD_OFF)   # 先关闭输出
            except: pass
            try:
                self._serial.close()
            except: pass
            self._serial = None
            self._cached_output_on = False
```

---

## 3. 十六进制指令协议

### 指令格式总览

| 功能 | 字节序列 | 说明 |
|------|----------|------|
| 关闭输出 | `55 AA 03 00 03` | 固定指令 |
| 开启输出 | `55 AA 03 01 04` | 固定指令 |
| 设置功率 | `55 AA 05 01 H L CHK` | H=高字节, L=低字节, CHK=校验和 |

### 设置功率指令构造

```python
@staticmethod
def _build_set_power_cmd(power_mw: int) -> bytes:
    """
    指令格式: 55 AA 05 01 <H> <L> <CHK>
    CHK = (05 + 01 + H + L) & 0xFF
    """
    high = (power_mw >> 8) & 0xFF
    low = power_mw & 0xFF
    chk = (0x05 + 0x01 + high + low) & 0xFF
    return bytes([0x55, 0xAA, 0x05, 0x01, high, low, chk])
```

示例：设置 150 mW
- `power_mw = 150 = 0x0096`
- `high = 0x00`, `low = 0x96`
- `chk = (5 + 1 + 0 + 150) & 0xFF = 156 & 0xFF = 0x9C`
- 完整指令：`55 AA 05 01 00 96 9C`

---

## 4. 公共 API

```python
def set_power(self, power_mw: int) -> None:
    if power_mw < 0 or power_mw > self._max_power_mw:
        raise ValueError(f"功率 {power_mw} mW 超出范围 [0, {self._max_power_mw}]")
    cmd = self._build_set_power_cmd(power_mw)
    self._send(cmd)
    self._cached_power_mw = float(power_mw)

def set_output(self, on: bool) -> None:
    cmd = self._CMD_ON if on else self._CMD_OFF
    self._send(cmd)
    self._cached_output_on = on

def get_power(self) -> float:
    return self._cached_power_mw    # 单向设备，只能返回缓存值

def get_output(self) -> bool:
    return self._cached_output_on   # 单向设备，只能返回缓存值

def emergency_stop(self) -> bool:
    for _ in range(3):
        try:
            self.set_output(False)
            return True
        except Exception:
            pass
    return False
```

---

## 5. 端口扫描

```python
@staticmethod
def scan_ports(baudrate=9600, timeout=0.5) -> List[Tuple[str, str]]:
    results = []
    for p in list_ports.comports():
        port = p.device
        try:
            s = serial.Serial(port, baudrate, bytesize=8, parity="N", stopbits=1, timeout=timeout)
            s.reset_input_buffer()
            s.reset_output_buffer()
            s.write(LaserMSLDriver._CMD_OFF)   # 发送关闭指令验证
            s.close()
            label = "Laser (unverified)"
            if p.serial_number:
                label += f" [USB SN:{p.serial_number}]"
            if p.vid and p.pid:
                label += f" [VID:{p.vid:04X} PID:{p.pid:04X}]"
            results.append((port, label))
        except Exception:
            pass
    return results
```

- **无法验证设备身份**（单向协议），仅验证端口是否可写。
- 附加 USB 适配器信息（VID/PID/SN）辅助用户识别。

---

## 6. GUI 连接面板与功率控制

### 连接页控件

| 控件 | 类型 | 说明 |
|------|------|------|
| Port | QComboBox (Editable) | 串口列表 |
| Baudrate | QComboBox | 固定 9600 |
| Scan | QPushButton | 扫描可用端口 |
| Connect | QPushButton | 切换连接/断开 |
| Power | UnitSelector + QPushButton("设置") | 功率输入 + 设置按钮 |
| Max Label | QLabel | `Max: 150 mW`（从配置读取） |
| Output Toggle | QCheckBox | 激光输出开关 |

### 功率设置流程

```python
def _set_laser_power(self):
    if not self._ctrl.is_laser_connected:
        QMessageBox.warning(self, "警告", "请先连接激光器")
        return
    power = int(self._laser_power_selector.value_in_base_unit())
    max_mw = self._cfg["laser"].get("max_power_mw", 150)
    if power < 0 or power > max_mw:
        QMessageBox.warning(self, "警告", f"功率超出范围 [0, {max_mw}] mW")
        return
    self._cmd_service.submit(Command(CommandType.LASER_SET_POWER, {"power_mw": power}, source="gui"))
```

### 输出开关流程

```python
def _toggle_laser_output(self, state):
    on = state == Qt.Checked
    self._cmd_service.submit(Command(CommandType.LASER_SET_OUTPUT, {"enabled": on}, source="gui"))
```

### 断开前确认

```python
def _toggle_laser_connection(self):
    if self._ctrl.is_laser_connected:
        reply = QMessageBox.question(
            self, "断开激光器",
            "断开前将关闭激光输出。\n确认断开？",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes
        )
        if reply != QMessageBox.Yes:
            return
        submit(CommandType.LASER_DISCONNECT)
```

---

## 7. 激光器 Poll Worker

```python
# workers/laser_poll_worker.py
class LaserPollWorker(QObject):
    state_updated = pyqtSignal(dict)
    interval_ms = 1000

    def run(self):
        while self._running:
            state = {
                "power_mw": self._laser.cached_power_mw,
                "output_on": self._laser.cached_output_on,
                "max_power_mw": self._laser.max_power_mw,
                "wavelength_nm": self._laser.wavelength_nm,
            }
            self.state_updated.emit(state)
            time.sleep(self.interval_ms / 1000.0)
```

- **1000ms 间隔**，无实际 I/O（单向设备无法读取）。
- 仅广播软件端缓存状态，供 GUI 和采集线程同步。

---

## 8. Rust 重构要点

| 方面 | 建议 |
|------|------|
| 串口库 | `serialport` crate |
| 单向通信 | 仅 `write`，无 `read`；连接成功标志为串口打开即可 |
| 指令构造 | 用 `Vec<u8>` 或固定数组 `[u8; 7]`，校验和用 wrapping_add |
| 功率校验 | `0..=max_power_mw`，超出返回 `Err` |
| 状态缓存 | `Arc<Mutex<LaserState>>`，功率和输出状态仅软件维护 |
| 急停 | 重试 3 次发送关闭指令，任意一次成功即返回 Ok |
| 扫描 | 打开串口 → 发送 `_CMD_OFF` → 关闭 → 若全程无错则认为可用 |
| 无 IDN | GUI 显示用配置中的 `serial_number` + `model` 拼接 |
| USB 信息 | `serialport::SerialPortInfo` 中读取 USB 描述符辅助识别 |
