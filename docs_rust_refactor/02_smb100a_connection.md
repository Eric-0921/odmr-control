# SMB100A 连接与 IDN 识别梳理

> 对应代码：`instruments/smb100a.py`、`app/gui.py` (连接面板)

---

## 1. 通信协议

| 项目 | 值 |
|------|-----|
| 库 | `pyvisa` (`pyvisa.ResourceManager`) |
| 资源类型 | VISA (USBTMC / GPIB / LAN / Serial) |
| 写终止符 | `\n` |
| 读终止符 | `\n` |
| 默认超时 | 10000 ms |
| 频率范围 | 100 kHz ~ 12.75 GHz |
| 功率范围 | -120 dBm ~ +30 dBm（驱动硬限制） |

---

## 2. 连接流程

```python
def connect(self, visa_address: str, timeout_ms: int = 10000) -> str:
    self._resource_manager = pyvisa.ResourceManager()
    self._instrument = self._resource_manager.open_resource(visa_address)
    self._instrument.timeout = timeout_ms
    self._instrument.write_termination = "\n"
    self._instrument.read_termination = "\n"
    self._instrument.clear()
    self._instrument.write("*CLS")
    idn = self._instrument.query("*IDN?")   # ← IDN 识别
    # 初始化安全状态
    self._instrument.write("OUTP OFF")
    self._instrument.write("FREQ:MODE CW")
    self._update_cached_state()
    return idn.strip()
```

### 断开流程

```python
def close(self):
    if self._instrument is not None:
        try:
            self._instrument.write("OUTP OFF")
            self._instrument.write("FREQ:MODE CW")
        except: pass
        finally:
            self._instrument.close()
            self._instrument = None
    if self._resource_manager is not None:
        self._resource_manager.close()
        self._resource_manager = None
```

---

## 3. IDN 格式与序列号提取

### IDN 响应格式

```
Rohde&Schwarz,<type>,<partno>/<serial>,<firmware>
```

示例：
```
Rohde&Schwarz,SMB100A,1403.5000K02/106789,3.1.16
```

### 序列号提取（GUI 层）

```python
@staticmethod
def _extract_smb_serial(idn: str) -> str:
    parts = idn.split(",")
    if len(parts) >= 3:
        serial_part = parts[2].split("/")[-1].strip()
        return serial_part
    return ""
```

- `parts[2]` = `1403.5000K02/106789`
- `split("/")[-1]` = `106789`（序列号）

---

## 4. 自动扫描

```python
@staticmethod
def scan_rs_devices(timeout_ms: int = 500) -> list:
    """返回 [(visa_address, idn_string), ...]"""
    rm = pyvisa.ResourceManager()
    devices = []
    for addr in rm.list_resources():
        try:
            instr = rm.open_resource(addr)
            instr.timeout = timeout_ms
            instr.write_termination = "\n"
            instr.read_termination = "\n"
            idn = instr.query("*IDN?").strip()
            if "Rohde&Schwarz" in idn or "SMB" in idn:
                devices.append((addr, idn))
            instr.close()
        except Exception:
            pass
    rm.close()
    return devices
```

- 扫描所有 VISA 资源（`list_resources()`）。
- 短超时 500ms，防止陈旧设备挂起。
- 过滤条件：`"Rohde&Schwarz" in idn or "SMB" in idn`。

---

## 5. SMB Bindings（持久化绑定）

### 数据结构

```python
"smb_bindings": {}   # {serial_number: visa_address}
```

### 行为

| 场景 | 行为 |
|------|------|
| 扫描时 | 检查 `serial in bindings`，若是则标注 `[已知]` 并自动选中 |
| 连接成功时 | 自动保存 `smb_bindings[serial] = visa_address` |
| 下次扫描 | 若 `bindings[serial] == addr`，自动选中该设备 |

### GUI 扫描对话框

```python
def _scan_rs_devices(self):
    devices = SMB100ADriver.scan_rs_devices(timeout_ms=500)
    bindings = self._cfg.get("smb_bindings", {})
    for i, (addr, idn) in enumerate(devices):
        serial = self._extract_smb_serial(idn)
        label = f"{addr}  |  {idn[:50]}"
        if serial and serial in bindings:
            label += "  [已知]"
        # ... 弹出 QInputDialog 让用户选择
    if auto_select_idx >= 0:
        default_idx = auto_select_idx  # 自动选中已知设备
```

---

## 6. GUI 连接面板交互

### 连接页控件（Row 0）

| 控件 | 类型 | 说明 |
|------|------|------|
| Protocol | QComboBox | USB / GPIB / LAN / Serial |
| Address | QLineEdit | VISA 地址，placeholder 随协议变化 |
| Scan | QPushButton | 调用 `scan_rs_devices()` |
| Connect | QPushButton | 切换连接/断开 |
| LED | QLabel (CSS) | 绿色=已连接，灰色=未连接 |

### Placeholder 提示

```python
hints = {
    "USB": "USB0::0x0AAD::0x0054::SN::INSTR",
    "GPIB": "GPIB0::28::INSTR",
    "LAN": "TCPIP0::192.168.1.100::inst0::INSTR",
    "Serial": "ASRL1::INSTR",
}
```

### 连接/断开按钮行为

```python
def _toggle_smb_connection(self):
    if 已连接:
        # 弹出确认框：断开前是否关闭 RF/LF/Modulation？
        reply = QMessageBox.question(..., QMessageBox.Yes | QMessageBox.No | QMessageBox.Cancel)
        if reply == QMessageBox.Yes:
            submit(CommandType.SMB_EMERGENCY_STOP)
        submit(CommandType.SMB_DISCONNECT)
    else:
        addr = self._smb_addr_input.text().strip()
        submit(CommandType.SMB_CONNECT, {"address": addr, "timeout_ms": 10000})
```

---

## 7. 状态缓存

驱动内部维护缓存（`threading.Lock` 保护）：

```python
self._cached_freq_hz: float = 0.0
self._cached_power_dbm: float = -30.0
self._cached_output_on: bool = False
self._cached_mode: str = "CW"
self._cached_lf_on: bool = False
self._cached_mod_on: bool = False
```

连接成功后立即调用 `_update_cached_state()` 从设备读取：
- `FREQ:CW?` → 频率
- `POW:LEV?` → 功率
- `OUTP?` → 输出状态
- `FREQ:MODE?` → CW/SWEEP 模式
- `SOUR:LFO:STAT?` → LF 输出
- `SOUR:MOD:ALL:STAT?` → 调制总状态

---

## 8. Rust 重构要点

| 方面 | 建议 |
|------|------|
| VISA 库 | `rscpi` / `visa-rs` / 调用 NI-VISA C API via `bindgen` |
| 超时处理 | 注意 `list_resources()` 可能挂起，始终设短超时 |
| 字符串处理 | IDN 解析用 `split(',')`，注意 firmware 字段可能含空格 |
| 状态缓存 | `Arc<Mutex<SmbState>>` 或 `RwLock<SmbState>` |
| 线程安全 | VISA 资源非 `Send`，考虑用 dedicated I/O thread + channel |
| 绑定持久化 | `smb_bindings: HashMap<String, String>` 写入 TOML/JSON |
