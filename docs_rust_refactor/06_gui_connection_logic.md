# GUI 连接面板逻辑梳理

> 对应代码：`app/gui.py`（约 5200 行中的连接页部分）

---

## 1. 页面结构

GUI 主界面左侧为导航树，右侧为堆叠页面。索引 `0` 为设备连接页：

```python
self._stack.addWidget(self._build_connection_page())   # 0: 设备连接
```

### 连接页分区

| 分区 | 内容 |
|------|------|
| SMB100A | 协议选择、VISA 地址、扫描、连接按钮、状态 LED |
| OE1022D | 串口、波特率、传输模式、扫描、连接按钮、状态 LED |
| Laser | 串口、波特率、扫描、连接按钮、状态 LED、功率控制 |
| Magnetic Field | 三轴表格（Connection Tab + Manual Tab + Vector Tab + Sequence Tab） |

---

## 2. 全局连接/断开按钮

### 顶部工具栏

```python
act_conn_all = QAction("全部连接 / Connect All", self)
act_conn_all.triggered.connect(self._connect_all)
act_disconn_all = QAction("全部断开 / Disconnect All", self)
act_disconn_all.triggered.connect(self._disconnect_all)
```

### 连接全部

```python
def _connect_all(self):
    if not self._ctrl.is_smb_connected:
        self._toggle_smb_connection()
    if not self._ctrl.is_lockin_connected:
        self._toggle_lockin_connection()
    if not self._ctrl.is_laser_connected:
        self._toggle_laser_connection()
    for axis in ("X", "Y", "Z"):
        if not self._ctrl.mag.is_connected(axis):
            self._toggle_mag_axis(axis)
```

### 断开全部

```python
def _disconnect_all(self):
    if self._ctrl.is_smb_connected:
        self._toggle_smb_connection()
    if self._ctrl.is_lockin_connected:
        self._toggle_lockin_connection()
    if self._ctrl.is_laser_connected:
        self._toggle_laser_connection()
    self._submit_mag_command(CommandType.MAG_DISCONNECT_ALL, {}, "mag_disconnect_all")
```

---

## 3. 状态显示机制

### 状态 LED（CSS 属性切换）

```python
self._smb_led.setProperty("on", "true")   # 或 "false"
self._smb_led.style().unpolish(self._smb_led)
self._smb_led.style().polish(self._smb_led)
```

### 状态标签

| 状态 | 颜色 | 文字 |
|------|------|------|
| 已连接 | `#00a651` (绿色) + `font-weight: 600` | `已连接 / Connected: {idn[:30]}` |
| 未连接 | `#999` (灰色) | `未连接 / Disconnected` |

### 底部状态栏

```python
self._status_smb.setText("SMB: " + idn[:30])
self._status_lockin.setText("Lockin: " + idn[:30])
self._status_laser.setText("Laser: " + idn[:30])
```

---

## 4. 异步命令回调机制

GUI 使用 `CommandService` 的异步命令模式（`submit`），结果通过 `command_completed` 信号回调。

### 命令挂起表

```python
self._pending_cmds: Dict[str, Tuple[str, Any]] = {}
# {request_id: (op_name, context)}
```

### 发送命令

```python
def _toggle_smb_connection(self):
    if self._cmd_service is not None:
        cmd = Command(CommandType.SMB_CONNECT, {"address": addr, ...}, source="gui")
        req = self._cmd_service.submit(cmd)
        self._pending_cmds[req] = ("smb_connect", addr)
    else:
        # fallback：直接调用 controller（无 CommandService 时）
        idn = self._ctrl.connect_smb(addr)
        # 直接更新 GUI...
```

### 处理结果

```python
def _on_command_completed(self, request_id: str, success: bool, message: str, result: dict):
    op, ctx = self._pending_cmds.pop(request_id)

    if op == "smb_connect":
        if success:
            idn = result.get("idn", "Unknown")
            # 更新 LED、标签、状态栏
            # 保存 smb_bindings
            serial = self._extract_smb_serial(idn)
            if serial:
                self._cfg.setdefault("smb_bindings", {})[serial] = ctx
        else:
            QMessageBox.critical(self, "Connection Error", message)

    elif op == "lockin_connect":
        if success:
            idn = result.get("idn", "Unknown")
            # 保存 lockin_bindings
            if idn and idn != "Unknown":
                self._cfg.setdefault("lockin_bindings", {})[idn] = ctx

    elif op == "mag_auto_detect":
        devices = result.get("devices", [])
        matched = result.get("matched", {})
        # 更新各轴 Port ComboBox 和 Binding Label
        # 保存 magnetic_field.bindings
```

---

## 5. 各设备连接按钮状态机

### SMB100A

```
[未连接] → 点击 Connect → submit(SMB_CONNECT) → 回调成功 → [已连接]
                                              → 回调失败 → 弹窗报错
[已连接] → 点击 Disconnect → 弹窗确认(Yes/No/Cancel)
                              Yes → submit(SMB_EMERGENCY_STOP) → submit(SMB_DISCONNECT)
                              No  → submit(SMB_DISCONNECT)
                              Cancel → 无操作
```

### OE1022D

```
[未连接] → 点击 Connect → submit(LOCKIN_CONNECT) → 回调成功 → [已连接]
[已连接] → 点击 Disconnect → submit(LOCKIN_DISCONNECT) → [未连接]
```

### Laser

```
[未连接] → 点击 Connect → submit(LASER_CONNECT) → 回调成功 → [已连接]
[已连接] → 点击 Disconnect → 弹窗确认(Yes/No)
                              Yes → submit(LASER_DISCONNECT)
                              No  → 无操作
```

### Magnetic Field（单轴）

```
[未连接] → 点击 Connect → submit(MAG_CONNECT_AXIS, {port, coil_constant, zero_offset}) → [已连接]
[已连接] → 点击 Disconnect → submit(MAG_DISCONNECT_AXIS)
```

---

## 6. 扫描按钮行为对比

| 设备 | 扫描方法 | 过滤条件 | 自动选中逻辑 |
|------|----------|----------|-------------|
| SMB100A | `SMB100ADriver.scan_rs_devices()` | `"Rohde&Schwarz" in idn or "SMB" in idn` | `smb_bindings[serial] == addr` |
| OE1022D | `OE1022DDriver.scan_ports_with_idn()` | `"SSI LIA-OE1022D" in resp` | `lockin_bindings[idn] == port` |
| Laser | `LaserMSLDriver.scan_ports()` | 无（仅验证可写） | 无 |
| Magnetic Field | `FieldController.scan_device_ports()` | `"," in resp`（含逗号即认为有效） | `bindings[axis]["idn"] == detected_idn` |

---

## 7. 配置保存时机

连接成功后自动保存绑定（**不等待用户手动保存**）：

```python
# SMB
self._cfg.setdefault("smb_bindings", {})[serial] = visa_address

# Lockin
self._cfg.setdefault("lockin_bindings", {})[idn] = port

# Magnetic Field（连接单轴或 auto_detect 后）
self._cfg.setdefault("magnetic_field", {}).setdefault("bindings", {})[axis] = {
    "port": port, "idn": idn
}
```

用户点击菜单 `File → Save Config` 时才将所有配置（含绑定）写入磁盘：

```python
def _save_config(self):
    save_config(self._cfg, Path("config.json"))
```

---

## 8. Rust 重构要点

| 方面 | 建议 |
|------|------|
| GUI 框架 | `egui` / `tauri` + `leptos` / `iced` |
| 状态管理 | `Arc<Mutex<AppState>>` 或 `tokio::sync::watch::Receiver<ConnectionState>` |
| 异步命令 | `tokio::sync::mpsc` 发送命令，`tokio::sync::oneshot` 返回结果，避免回调地狱 |
| LED 指示 | 用 `egui::widgets::color_picker` 或自定义圆形 `Button` / `Shape` |
| 状态栏 | `egui::TopBottomPanel::bottom()` 放置状态标签 |
| 弹窗确认 | `egui::Window` 或 `rfd` crate 的 `MessageDialog` |
| 配置热保存 | 绑定更新后立即写文件（或 debounce 后写入），避免丢失 |
| 端口下拉框 | 扫描后清空再填充；保留当前选择，若新列表中不存在则置空 |
| IDN 截断 | GUI 显示取前 30 字符，避免标签过长 |
