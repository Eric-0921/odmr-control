# ODMR Control API 参考文档

> 版本: V2.0  
> 日期: 2026-05-12

---

## 目录

1. [CommandService](#commandservice)
2. [AgentAPI](#agentapi)
3. [InstrumentController](#instrumentcontroller)
4. [SweepEngine](#sweepengine)
5. [SMB100ADriver](#smb100adriver)
6. [OE1022DDriver](#oe1022ddriver)
7. [ODMRRecorder](#odmrrecorder)
8. [TimestampSyncHub](#timestampsynchub)

---

## 1. CommandService

**文件**: `core/command_service.py`

统一的设备命令执行入口。GUI 和 AI Agent 的唯一操作通道。

### 1.1 构造函数

```python
class CommandService(QObject):
    def __init__(
        self,
        controller: InstrumentController,
        config: Optional[Dict[str, Any]] = None,
        parent: Optional[QObject] = None,
    ) -> None
```

| 参数 | 类型 | 说明 |
|------|------|------|
| controller | InstrumentController | 仪器控制器实例 |
| config | dict | 配置字典（含 smb.amplifier_installed 等） |
| parent | QObject | 父对象 |

### 1.2 信号

```python
command_completed = pyqtSignal(str, bool, str, dict)
# 参数: request_id, success, message, result

command_error = pyqtSignal(str, str)
# 参数: request_id, error_message

smb_state_broadcast = pyqtSignal(dict)
# 参数: {"freq_hz": float, "output_on": bool, "mode": str, "power_dbm": float, ...}

lockin_data_broadcast = pyqtSignal(dict)
# 参数: {"channel": int, "X": float, "Y": float, "R": float, "theta": float}

lockin_batch_broadcast = pyqtSignal(dict)
# 参数: RALL? 解析后的批次数据
```

### 1.3 方法

#### `start()` → `None`

启动命令处理工作线程。

#### `stop()` → `None`

停止命令处理工作线程。

#### `submit(cmd: Command) -> str`

异步提交命令，返回 `request_id`。

```python
cmd = Command(CommandType.SMB_SET_FREQUENCY, {"freq_hz": 2.87e9})
req_id = cmd_service.submit(cmd)
# 结果通过 command_completed 信号回调
```

#### `submit_sync(cmd: Command, timeout_ms: int = 5000) -> Tuple[bool, str, dict]`

同步执行命令（阻塞）。

```python
ok, msg, result = cmd_service.submit_sync(cmd)
# ok: bool, msg: str, result: dict
```

---

## 2. AgentAPI

**文件**: `core/agent_api.py`

面向 AI Agent 的高级 Python API。所有方法最终转换为 Command 提交到 CommandService。

### 2.1 构造函数

```python
class AgentAPI:
    def __init__(self, service: CommandService) -> None
```

### 2.2 SMB100A 方法

#### `set_frequency(freq_hz: float, timeout_ms: int = 5000) -> Tuple[bool, str, dict]`

设置 CW 频率 (Hz)。

#### `set_power(power_dbm: float, timeout_ms: int = 5000) -> Tuple[bool, str, dict]`

设置输出功率 (dBm)。受安全限制约束。

#### `set_output(enabled: bool, timeout_ms: int = 5000) -> Tuple[bool, str, dict]`

开关 RF 输出。

#### `set_lf_output(enabled: bool, freq_hz: Optional[float] = None, amplitude_v: Optional[float] = None, timeout_ms: int = 5000) -> Tuple[bool, str, dict]`

设置 LF 输出。

#### `set_sweep(start_hz: float, stop_hz: float, step_hz: float, dwell_ms: float, power_dbm: Optional[float] = None, timeout_ms: int = 5000) -> Tuple[bool, str, dict]`

配置扫频参数。

#### `start_sweep(timeout_ms: int = 5000) -> Tuple[bool, str, dict]`

启动扫频。

#### `stop_sweep(timeout_ms: int = 5000) -> Tuple[bool, str, dict]`

停止扫频。

#### `get_smb_status(timeout_ms: int = 5000) -> Tuple[bool, str, dict]`

获取 SMB100A 当前状态。

### 2.3 OE1022D 方法

#### `get_lockin_snapd(channel: int = 1, timeout_ms: int = 5000) -> Tuple[bool, str, dict]`

获取通道的 X/Y/R/theta。

#### `get_lockin_status(channel: int = 1, timeout_ms: int = 5000) -> Tuple[bool, str, dict]`

获取通道状态（过载、PLL）。

#### `set_lockin_time_constant(channel: int, index: int, timeout_ms: int = 5000) -> Tuple[bool, str, dict]`

设置时间常数。

#### `auto_gain(channel: int = 1, timeout_ms: int = 5000) -> Tuple[bool, str, dict]`

执行自动增益。

#### `auto_reserve(channel: int = 1, timeout_ms: int = 5000) -> Tuple[bool, str, dict]`

执行自动动态储备。

#### `auto_phase(channel: int = 1, timeout_ms: int = 5000) -> Tuple[bool, str, dict]`

执行自动相位。

### 2.4 系统方法

#### `emergency_stop(timeout_ms: int = 5000) -> Tuple[bool, str, dict]`

执行系统级急停。

#### `get_all_status(timeout_ms: int = 5000) -> Tuple[bool, str, dict]`

获取所有已连接设备的状态。

### 2.5 高级组合操作

#### `run_odmr_sweep(start_freq_hz: float, stop_freq_hz: float, step_hz: float, dwell_ms: float, power_dbm: float, cycles: int = 1, timeout_ms: int = 30000) -> Tuple[bool, str, dict]`

执行一次完整的 ODMR 扫频实验（简化版）。

---

## 3. InstrumentController

**文件**: `core/instrument_controller.py`

统一仪器控制器（门面模式）。

### 3.1 信号

```python
smb_state_changed = pyqtSignal(float, bool, str)        # freq_hz, output_on, mode
smb_state_dict_changed = pyqtSignal(dict)                # 完整状态字典
lockin_data_ready = pyqtSignal(dict)                     # SNAPD? 数据
lockin_status_changed = pyqtSignal(dict)                 # 过载/PLL 状态
lockin_batch_ready = pyqtSignal(dict)                    # RALL? 批次
sweep_progress = pyqtSignal(int, int)                    # current, total
sweep_finished = pyqtSignal(bool)                        # completed_ok
error_occurred = pyqtSignal(str)
log_requested = pyqtSignal(str)
```

### 3.2 属性

| 属性 | 类型 | 说明 |
|------|------|------|
| smb | SMB100ADriver | SMB100A 驱动实例 |
| lockin | OE1022DDriver | OE1022D 驱动实例 |
| is_smb_connected | bool | SMB100A 是否已连接 |
| is_lockin_connected | bool | OE1022D 是否已连接 |
| is_sweeping | bool | 是否正在扫频 |
| is_acquiring | bool | 是否正在采集 |

### 3.3 连接管理

```python
def connect_smb(self, visa_address: str, timeout_ms: int = 10000) -> str
    # 返回: IDN 字符串
    # 副作用: 自动启动 SMBPollWorker

def disconnect_smb(self) -> None
    # 副作用: 自动停止 SMBPollWorker

def connect_lockin(
    self, port: str, baudrate: int = 921600,
    bytesize: int = 8, parity: str = "N",
    stopbits: int = 1, timeout: float = 1.0,
) -> str
    # 返回: IDN 字符串
    # 副作用: 自动启动 LockinMonitorWorker

def disconnect_lockin(self) -> None
    # 副作用: 自动停止 LockinMonitorWorker + LockinAcquireWorker
```

### 3.4 采集管理

```python
def start_lockin_acquire(self, recorder: Optional[ODMRRecorder] = None) -> None
    # 启动 RALL? 高速采集（仅在扫频期间调用）

def stop_lockin_acquire(self) -> None
    # 停止 RALL? 采集
```

### 3.5 安全操作

```python
def emergency_stop(self) -> None
    # 急停：关闭 RF/LF/FM，切回 CW，停止采集

def verify_emergency_stop(self) -> Dict[str, object]
    # 验证急停效果，返回 {"smb": {...}, "acquiring": bool, "sweeping": bool}
```

---

## 4. SweepEngine

**文件**: `core/sweep_engine.py`

### 4.1 数据类

```python
@dataclass
class SweepStep:
    name: str = ""
    start_freq_hz: float = 2.82e9
    stop_freq_hz: float = 2.92e9
    step_hz: float = 500e3
    dwell_ms: float = 500.0
    power_dbm: float = -30.0
    lf_freq_hz: float = 500.0
    lf_amp_mv: float = 137.0
    lf_shape: str = "SQUARE"
    fm_dev_hz: float = 4e6
    cycles: int = 1
    cycle_interval_ms: int = 200

@dataclass
class SweepSequence:
    name: str = "default"
    steps: List[SweepStep] = field(default_factory=list)
    loop_count: int = 1
    loop_delay_s: float = 0.0
    return_to_zero: bool = True
```

### 4.2 SweepEngine

```python
class SweepEngine(QObject):
    def set_recorder(self, recorder: Optional[ODMRRecorder]) -> None
    def set_sequence(self, seq: SweepSequence) -> None
    def load_sequence(self, steps: List[SweepStep], loop_count: int = 1) -> None
    def start(self) -> None
    def stop(self) -> None
    def pause(self) -> None
    def resume(self) -> None
```

---

## 5. SMB100ADriver

**文件**: `instruments/smb100a.py`

### 5.1 类常量

```python
MIN_FREQ_HZ: float = 100e3      # 100 kHz
MAX_FREQ_HZ: float = 12.75e9    # 12.75 GHz
MIN_POWER_DBM: float = -120.0
MAX_POWER_DBM: float = 30.0
```

### 5.2 类方法

```python
@staticmethod
def scan_rs_devices(timeout_ms: int = 500) -> List[Tuple[str, str]]
    # 返回: [(visa_address, idn_string), ...]
```

### 5.3 连接

```python
def connect(self, visa_address: str, timeout_ms: int = 10000) -> str
    # 初始化安全状态: OUTP OFF, FREQ:MODE CW

def close(self) -> None
    # 安全断开: OUTP OFF, FREQ:MODE CW, 释放资源
```

### 5.4 属性

```python
is_connected: bool
cached_freq_hz: float
cached_power_dbm: float
cached_output_on: bool
cached_mode: str  # "CW" or "SWEEP"
```

### 5.5 频率与功率

```python
def set_freq_cw(self, hz: float) -> None
def get_freq_cw(self) -> float
def set_power(self, dbm: float) -> None
def get_power(self) -> float
def set_output(self, on: bool) -> None
def get_output(self) -> bool
```

### 5.6 扫频

```python
def set_sweep_start(self, hz: float) -> None
def set_sweep_stop(self, hz: float) -> None
def set_sweep_step(self, hz: float) -> None
def set_sweep_dwell(self, ms: float) -> None
def set_sweep_mode(self, mode: str = "AUTO") -> None
def set_freq_mode(self, mode: str) -> None          # "CW" | "SWEEP"
def get_freq_mode(self) -> str
def configure_sweep(self, start_hz, stop_hz, step_hz, dwell_ms, power_dbm) -> None
def start_sweep(self) -> None
def stop_sweep(self) -> None
```

### 5.7 LF 输出

```python
def set_lf_output(self, on: bool) -> None
def set_lf_freq(self, hz: float) -> None
def set_lf_voltage(self, mv: float) -> None
def set_lf_shape(self, shape: str) -> None            # SINE|SQUARE|TRIANGLE|SAWTOOTH|ISAWTOOTH
def set_lf_impedance(self, imp: str = "LOW") -> None
def get_lf_output(self) -> bool
def get_lf_freq(self) -> float
def get_lf_mode(self) -> str
```

### 5.8 FM 调制

```python
def set_fm_state(self, on: bool) -> None
def set_fm_deviation(self, hz: float) -> None
def get_modulation_state(self) -> bool
```

### 5.9 安全

```python
def emergency_stop(self) -> None
    # OUTP OFF, SOUR:LFO:STAT OFF, SOUR:FM:STAT OFF, FREQ:MODE CW
```

---

## 6. OE1022DDriver

**文件**: `instruments/oe1022d.py`

### 6.1 类常量

```python
DEFAULT_BAUDRATE = 921600
IDN_PATTERN = "SSI LIA-OE1022D"
RALL_TOTAL_BYTES = 12288
SAMPLES_PER_BATCH = 50
```

### 6.2 类方法

```python
@staticmethod
def scan_ports_with_idn(baudrate: int = 921600, timeout: float = 0.5) -> List[Tuple[str, str]]
    # 返回: [(port_name, idn_or_empty), ...]
```

### 6.3 连接

```python
def connect(self, port: str, baudrate: int = 921600, ...) -> str
def close(self) -> None
def identify(self) -> str       # *IDND?
```

### 6.4 SNAPD? 实时监控

```python
def snapd(self, channel: int = 1, *params: int) -> Dict[str, float]
    # 返回: {"X": mV, "Y": mV, "R": mV, "theta": deg}
```

### 6.5 RALL? 高速采集

```python
def start_rall_stream(self) -> None
def read_rall_batch(self, timeout: float = 2.0) -> bytes
@staticmethod
def parse_rall(raw: bytes) -> Dict[str, np.ndarray]
    # 返回: {col_name: np.ndarray(shape=(50,), dtype=float64), ...}
def get_rall_column_info(self) -> List[Dict]
```

### 6.6 状态查询

```python
def get_input_overload(self, channel: int = 1) -> bool
def get_gain_overload(self, channel: int = 1) -> bool
def get_pll_locked(self, channel: int = 1) -> bool
```

### 6.7 INPUT / FILTERS

```python
def set_input_source(self, channel: int = 1, source: int = 0) -> None       # 0=A, 1=AB, 2=I6, 3=I8
def set_current_gain(self, channel: int = 1, gain: int = 0) -> None        # 0=1, 1=10, 2=100
def set_grounding(self, channel: int = 1, ground: int = 0) -> None         # 0=Float, 1=Ground
def set_coupling(self, channel: int = 1, coupling: int = 0) -> None         # 0=AC, 1=DC
def set_line_notch(self, channel: int = 1, mode: int = 1) -> None          # 0=Off, 1=50Hz, 2=50+100, 3=100Hz
```

### 6.8 REF / PHASE

```python
def set_ref_phase(self, channel: int = 1, phase_deg: float = 0.0) -> None
def set_ref_source(self, channel: int = 1, source: int = 0) -> None         # 0=Ext, 1=Int
def set_ref_slope(self, channel: int = 1, slope: int = 0) -> None           # 0=Sine, 1=PosTTL, 2=NegTTL
def set_ref_frequency(self, channel: int = 1, freq_hz: float = 1000.0) -> None
def set_harmonic(self, channel: int = 1, harmonic: int = 1) -> None
```

### 6.9 GAIN / TC

```python
def set_sensitivity(self, channel: int = 1, index: int = 10) -> None
def set_reserve(self, channel: int = 1, reserve: int = 1) -> None           # 0=Min, 1=Auto, 2=Max
def set_time_constant(self, channel: int = 1, index: int = 6) -> None
def set_filter_slope(self, channel: int = 1, index: int = 2) -> None       # 0=6dB, 1=12dB, 2=18dB, 3=24dB
def set_sync_filter(self, channel: int = 1, on: bool = True) -> None
```

### 6.10 CHANNEL OUTPUT

```python
def set_output_source(self, channel: int = 1, output_ch: int = 1, source: int = 0) -> None
def set_output_offset(self, channel: int = 1, output_ch: int = 1, offset: int = 0) -> None
def set_output_expand(self, channel: int = 1, output_ch: int = 1, expand: int = 0) -> None  # 0=1, 1=10, 2=100
```

### 6.11 AUTO SET

```python
def auto_gain(self, channel: int = 1) -> None
def auto_reserve(self, channel: int = 1) -> None
def auto_phase(self, channel: int = 1) -> None
```

---

## 7. ODMRRecorder

**文件**: `data/recorder.py`

### 7.1 构造函数

```python
class ODMRRecorder:
    def __init__(self, output_dir: Optional[Path | str] = None) -> None
```

### 7.2 属性

```python
is_recording: bool
output_dir: Optional[Path]
```

### 7.3 方法

```python
def set_output_dir(self, path: Path | str) -> None
def start_recording(self) -> None
def stop_recording(self) -> None
def write_batch(
    self,
    rall_data: Dict[str, np.ndarray],
    smb_freq_hz: float = 0.0,
    smb_power_dbm: float = 0.0,
    smb_rf_on: bool = False,
) -> None
```

---

## 8. TimestampSyncHub

**文件**: `core/timestamp_sync.py`

### 8.1 数据类

```python
@dataclass
class TimestampedSample:
    host_timestamp_s: float
    device_timestamp_s: float
    source: str
    data: Dict[str, Any]
```

### 8.2 方法

```python
class TimestampSyncHub:
    def register_source(self, source_id: str, poll_interval_ms: int) -> None
    def ingest(self, sample: TimestampedSample) -> None
    def query_aligned(self, timestamp_s: float, tolerance_ms: float = 50) -> Dict[str, Any]
    def get_sources(self) -> Dict[str, Dict[str, Any]]
```

---

## 附录 A: CommandType 完整列表

### SMB100A

| 枚举值 | 参数 | 说明 |
|--------|------|------|
| SMB_CONNECT | `{address, timeout_ms}` | 连接 SMB100A |
| SMB_DISCONNECT | `{}` | 断开 SMB100A |
| SMB_SET_FREQUENCY | `{freq_hz}` | 设置 CW 频率 |
| SMB_SET_POWER | `{power_dbm}` | 设置功率 |
| SMB_SET_OUTPUT | `{enabled}` | 开关 RF 输出 |
| SMB_SET_LF_OUTPUT | `{enabled, freq_hz, amplitude_v}` | 设置 LF 输出 |
| SMB_SET_LF_FREQ | `{freq_hz}` | 设置 LF 频率 |
| SMB_SET_LF_VOLTAGE | `{mv}` | 设置 LF 幅度 (mV) |
| SMB_SET_LF_SHAPE | `{shape}` | 设置 LF 波形 |
| SMB_SET_MODULATION | `{enabled}` | 开关调制 |
| SMB_SET_FM_DEVIATION | `{hz}` | 设置 FM 偏差 |
| SMB_SET_SWEEP | `{start_hz, stop_hz, step_hz, dwell_ms, power_dbm}` | 配置扫频 |
| SMB_START_SWEEP | `{}` | 启动扫频 |
| SMB_STOP_SWEEP | `{}` | 停止扫频 |
| SMB_QUERY_STATE | `{}` | 查询完整状态 |
| SMB_EMERGENCY_STOP | `{}` | 急停 |

### OE1022D

| 枚举值 | 参数 | 说明 |
|--------|------|------|
| LOCKIN_CONNECT | `{port, baudrate, ...}` | 连接 OE1022D |
| LOCKIN_DISCONNECT | `{}` | 断开 OE1022D |
| LOCKIN_SET_INPUT | `{channel, source, gain, ground, coupling, notch}` | INPUT/FILTERS |
| LOCKIN_SET_REF_PHASE | `{channel, phase_deg, source, slope, freq_hz, harmonic}` | REF/PHASE |
| LOCKIN_SET_GAIN_TC | `{channel, sensitivity, reserve, time_const, filter_db, sync}` | GAIN/TC |
| LOCKIN_SET_OUTPUT | `{channel, output_ch, source, offset, expand}` | CHANNEL OUTPUT |
| LOCKIN_SET_SAMPLE | `{step_time_ms, length, trigger_mode, sample_mode}` | 采样配置 |
| LOCKIN_SET_TIME_CONSTANT | `{channel, index}` | 时间常数 |
| LOCKIN_SET_FILTER_SLOPE | `{channel, index}` | 滤波器滚降 |
| LOCKIN_SET_SYNC_FILTER | `{channel, on}` | 同步滤波器 |
| LOCKIN_SET_LINE_NOTCH | `{channel, mode}` | 陷波器 |
| LOCKIN_AUTO_GAIN | `{channel}` | 自动增益 |
| LOCKIN_AUTO_RESERVE | `{channel}` | 自动动态储备 |
| LOCKIN_AUTO_PHASE | `{channel}` | 自动相位 |
| LOCKIN_QUERY_SNAPD | `{channel}` | 查询 X/Y/R/θ |
| LOCKIN_QUERY_STATUS | `{channel}` | 查询过载/PLL |
| LOCKIN_START_ACQUIRE | `{recorder_id}` | 启动 RALL? 采集 |
| LOCKIN_STOP_ACQUIRE | `{}` | 停止 RALL? 采集 |

### 系统

| 枚举值 | 参数 | 说明 |
|--------|------|------|
| SYS_EMERGENCY_STOP | `{}` | 系统急停 |
| SYS_QUERY_ALL_STATUS | `{}` | 查询所有设备状态 |
