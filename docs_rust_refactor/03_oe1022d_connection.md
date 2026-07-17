# OE1022D 锁相放大器连接梳理

> 对应代码：`instruments/oe1022d.py`、`app/gui.py`

---

## 1. 通信协议

| 项目 | RS232 模式 | USB2.0 模式 |
|------|-----------|-------------|
| 波特率 | 921600 | 921600（或更高） |
| 数据位 | 8 | 8 |
| 校验 | N | N |
| 停止位 | 1 | 1 |
| 命令终结符 | `\r` (0x0D) | `\r` |
| 默认超时 | 2.0s | 2.0s |
| `transport` 字段值 | `"rs232"` | `"usb2"` |
| RALL? 支持 | ❌ 否 | ✅ 是 |

> **关键约束**：RALL? 高速采集（12288 bytes/batch）仅在 USB2.0 接口可用。RS232 仅支持 SNAPD? 和 sample 命令。

---

## 2. 连接流程

```python
def connect(self, port, baudrate=921600, bytesize=8, parity="N",
            stopbits=1, timeout=2.0, transport="rs232") -> str:
    self._serial = serial.Serial(port, baudrate, bytesize, parity, stopbits, timeout=timeout)
    self._transport = transport.lower()
    self._serial.reset_input_buffer()
    self._serial.reset_output_buffer()
    idn = self.identify()   # ← 发送 *IDND?
    return idn
```

### 断开流程

```python
def close(self):
    with self._lock:
        if self._serial is not None:
            try:
                self._serial.close()
            except: pass
            self._serial = None
```

---

## 3. IDN 识别

### IDN 命令

```python
def identify(self) -> str:
    return self._exchange_ascii("*IDND?")
```

注意：**不是 `*IDN?`，是 `*IDND?`**（D = Device）。

### 响应格式

```
SSI LIA-OE1022D,SN:xxxxxxxx,FW:xxxx
```

### 匹配模式

```python
IDN_PATTERN = "SSI LIA-OE1022D"
```

> **兼容性注意**：不同固件版本格式有差异，使用子串匹配 `"SSI LIA-OE1022D" in resp`，而非精确匹配。

---

## 4. 自动扫描

```python
@staticmethod
def scan_ports_with_idn(baudrate=921600, timeout=0.5) -> list:
    """返回 [(port_name, idn_string_or_empty), ...]"""
    results = []
    for p in list_ports.comports():
        port = p.device
        idn = ""
        try:
            s = serial.Serial(port, baudrate, bytesize=8, parity="N", stopbits=1, timeout=timeout)
            s.reset_input_buffer()
            s.reset_output_buffer()
            s.write(b"*IDND?\r")
            resp = s.read_until(b"\r").decode("ascii", errors="ignore").strip()
            if OE1022DDriver.IDN_PATTERN in resp:
                idn = resp
            s.close()
        except Exception:
            pass
        results.append((port, idn))
    return results
```

- 扫描所有串口，发送 `*IDND?\r`。
- 读取到 `\r` 为止。
- `idn` 为空表示该端口不是 OE1022D 或无法通信。

---

## 5. Lockin Bindings（持久化绑定）

### 数据结构

```python
"lockin_bindings": {}   # {idn_string: com_port}
```

### 行为

| 场景 | 行为 |
|------|------|
| 扫描时 | OE1022D 优先显示在下拉框顶部，带 IDN 标注 |
| 已知设备 | 若 `idn in bindings`，自动选中该端口 |
| 连接成功时 | 自动保存 `lockin_bindings[idn] = port` |

### GUI 扫描逻辑

```python
def _scan_lockin_ports(self):
    results = OE1022DDriver.scan_ports_with_idn(baudrate=..., timeout=0.5)
    oe1022d_ports = [(port, idn) for port, idn in results if idn]
    other_ports = [port for port, idn in results if not idn]

    self._lockin_port_combo.clear()
    for port, idn in oe1022d_ports:
        self._lockin_port_combo.addItem(f"{port}  ({idn[:30]})", port)
    for port in other_ports:
        self._lockin_port_combo.addItem(port, port)

    # 自动匹配已知设备
    bindings = self._cfg.get("lockin_bindings", {})
    for port, idn in oe1022d_ports:
        if idn in bindings:
            idx = self._lockin_port_combo.findData(port)
            if idx >= 0:
                self._lockin_port_combo.setCurrentIndex(idx)
                break
```

---

## 6. GUI 连接面板

| 控件 | 类型 | 说明 |
|------|------|------|
| Port | QComboBox (Editable) | 串口列表，支持手动输入 |
| Baudrate | QComboBox | 默认 921600 |
| Transport | QComboBox | RS232/SNAPD 或 USB2.0/RALL |
| Scan | QPushButton | 扫描并识别 OE1022D |
| Connect | QPushButton | 切换连接/断开 |

---

## 7. 低层 I/O 细节

### ASCII 命令发送

```python
def _exchange_ascii(self, cmd: str, read_timeout: float = 2.0) -> str:
    tx = (cmd + "\r").encode("ascii")
    self._send(tx)
    resp = self._serial.readline()
    text = resp.decode("ascii", errors="replace")
    text = text.replace("\x00", "").strip()
    return text
```

### 关键注意点

- **不在读取过程中修改 `timeout`**：Windows 上修改 timeout 属性会重置 COM 端口内部缓冲区。
- **去除 null 字节填充**：OE1022D 响应可能含 `\x00`。
- **使用 `read_until(b"\r")` 而非固定长度**：响应长度不固定。

### RALL? 二进制读取

```python
def _read_exact(self, n: int, timeout: float = 2.0) -> bytes:
    data = b""
    while len(data) < n:
        chunk = self._serial.read(n - len(data))
        if not chunk:
            break
        data += chunk
    return data
```

- `RALL_TOTAL_BYTES = 12288`
- 解析前必须验证 `len(raw) == 12288`（或至少 `> 8000`）

---

## 8. Rust 重构要点

| 方面 | 建议 |
|------|------|
| 串口库 | `serialport` crate（跨平台） |
| USB 区分 | `serialport::available_ports()` 返回 `PortType::UsbPort`，可读取 VID/PID |
| 超时策略 | 构造时设定 timeout，读取过程中不修改（同 Python 约束） |
| 字符串解码 | ASCII decode，过滤 `\x00`，去除 `\r\n` |
| RALL? 数据 | `Vec<u8>` 预分配 12288 字节，`read_exact` 或循环 `read` |
| 二进制解析 | `byteorder` crate 处理大端 `f64`（`>f8`） |
| 状态机 | `enum Transport { Rs232, Usb2 }`，决定 RALL? 是否可用 |
| IDN 缓存 | 连接成功后缓存 IDN，用于绑定匹配 |
