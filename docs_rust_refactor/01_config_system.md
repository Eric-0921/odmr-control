# 配置系统梳理

> 对应代码：`app/config_io.py`、`config.json`（运行时）

---

## 1. 配置加载机制

```python
# 加载流程
cfg = DEFAULT_CONFIG.copy()      # 1. 以内嵌默认值为基线
if 配置文件存在:
    loaded = json.load(path)     # 2. 读取 JSON/XML
    _merge(cfg, loaded)          # 3. 深度合并（嵌套 dict 递归覆盖）
```

- **深度合并**：嵌套 `dict` 递归覆盖，不存在的键保留默认值。
- **兼容 XML**：旧版 `config.xml` 通过 `_xml_to_dict` 转换。
- **文件不存在**：自动使用全部默认值，不报错。

---

## 2. 设备连接相关配置键（DEFAULT_CONFIG）

### SMB100A

```python
"smb": {
    "visa_address": "USB0::0x0AAD::0x0054::106789::INSTR",
    "timeout_ms": 10000,
    "amplifier_installed": True,   # 决定功率上限 10 dBm（有放大器）或 25 dBm（无）
}
```

### OE1022D

```python
"lockin": {
    "port": "COM4",
    "baudrate": 921600,
    "bytesize": 8,
    "parity": "N",
    "stopbits": 1,
    "timeout": 1.0,
    "display_refresh_policy": {
        "mode": "time_constant_adaptive",
        "max_interval_ms": 300,
        "min_interval_ms": 50,
        "default_time_constant_index": 6,
    },
}
```

### 激光器

```python
"laser": {
    "port": "COM5",
    "baudrate": 9600,
    "bytesize": 8,
    "parity": "N",
    "stopbits": 1,
    "timeout": 1.0,
    "model": "MSL-U-532nm-300mW",
    "serial_number": "DL21088",
    "wavelength_nm": 532.12457,
    "max_power_mw": 150,           # 安全校验上限
    "power_stability_pct": 0.117,
    "warmup_s": 600,
    "power_mode": "APC",
}
```

### 三轴磁场

```python
"magnetic_field": {
    "baudrate": 9600,
    "poll_interval_ms": 500,
    "bindings": {                    # IDN → 轴绑定（自动匹配用）
        "X": {"idn": "", "port": "COM1"},
        "Y": {"idn": "", "port": "COM2"},
        "Z": {"idn": "", "port": "COM3"},
    },
    "coil_constant": {"X": 143.26, "Y": 141.77, "Z": 156.15},  # nT/mA
    "zero_offset": {"X": 0.0, "Y": 0.0, "Z": 0.0},              # mA
    "axes": {                        # 每轴独立配置（连接时应用）
        "X": {"port": "COM1", "coil_constant": 143.26, "zero_offset_mA": 0.0},
        "Y": {"port": "COM2", "coil_constant": 141.77, "zero_offset_mA": 0.0},
        "Z": {"port": "COM3", "coil_constant": 156.15, "zero_offset_mA": 0.0},
    },
    "sequence": {
        "settle_seconds": 0.5,
        "return_to_zero": True,
    },
}
```

### 绑定配置（运行中自动写入）

```python
"lockin_bindings": {},   # {idn_string: com_port}
"smb_bindings": {},      # {serial_number: visa_address}
```

---

## 3. 实际 config.json 示例（运行时）

运行时 `config.json` 中的磁场绑定使用**旧格式**（纯 IDN 字符串）：

```json
{
  "magnetic_field": {
    "bindings": {
      "X": "MAYNUO,M8812,080020960220402003,V2.7",
      "Y": "MAYNUO,M8812,080020960220402020,V2.7",
      "Z": "MAYNUO,M8812,080020960220402022,V2.7"
    },
    "axes": {
      "X": {"coil_constant": 156.15, "zero_offset_mA": 0.0},
      "Y": {"coil_constant": 143.26, "zero_offset_mA": 0.0},
      "Z": {"coil_constant": 141.77, "zero_offset_mA": 0.0}
    }
  }
}
```

> **兼容性注意**：`bindings` 兼容两种格式——旧格式纯字符串 IDN、新格式 `{"idn": "...", "port": "..."}`。Rust 中需用枚举或 Option 处理。

---

## 4. GUI 保存配置时的行为

```python
def _save_config(self):
    cfg["smb"]["visa_address"] = self._smb_addr_input.text()
    cfg["lockin"]["port"] = self._lockin_port_combo.currentText()
    cfg["lockin"]["baudrate"] = int(self._lockin_baud_combo.currentText())
    cfg["acquisition"]["save_dir"] = self._save_dir_input.text()
    # 磁场：保存端口、线圈常数、零偏
    for axis in ("X", "Y", "Z"):
        axes_cfg[axis]["port"] = controls["port"].currentText()
        axes_cfg[axis]["coil_constant"] = float(controls["coil"].text())
        axes_cfg[axis]["zero_offset_mA"] = float(controls["zero"].text())
```

---

## 5. Rust 重构建议

- 使用 `serde` + `serde_json` 解析配置，用 `#[serde(default)]` 实现缺失键回退。
- 配置结构体分层：`SmbConfig`, `LockinConfig`, `LaserConfig`, `MagneticFieldConfig`。
- `MagneticFieldConfig.bindings` 建议统一为 `HashMap<String, BindingEntry>`，内部做旧格式兼容。
- 配置文件热重载：目前不支持，Rust 版可考虑 `notify` crate 监听文件变更。
