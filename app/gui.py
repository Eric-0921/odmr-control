"""ODMR Control GUI

遵循 mag-field-control-v2 的 Siemens 工业风设计：
- 左侧导航树 + 右侧堆叠页面
- 严格的安全按钮状态同步
- pyqtgraph 实时波形（解决刷新率问题）
"""

from __future__ import annotations

import datetime
import logging
import queue
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtGui import QDoubleValidator, QFont, QIntValidator
from PyQt5.QtWidgets import (
    QAction, QApplication, QCheckBox, QComboBox, QFileDialog, QFrame,
    QGridLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QMainWindow,
    QMenu, QMenuBar, QMessageBox, QProgressBar, QPushButton, QScrollArea,
    QSizePolicy, QSplitter, QStackedWidget, QStatusBar, QTabWidget,
    QTextEdit, QToolBar, QTreeWidget, QTreeWidgetItem, QVBoxLayout,
    QWidget,
)

from app.config_io import load_config, save_config
from core.command_service import CommandService
from core.commands import Command, CommandType
from core.instrument_controller import InstrumentController
from core.sweep_engine import SweepEngine, SweepSequence, SweepStep
from data.circular_buffer import CircularBuffer
from data.recorder import ODMRRecorder

try:
    import pyqtgraph as pg
    _HAS_PYG = True
except ImportError:
    _HAS_PYG = False


# ---------------------------------------------------------------------------
# Siemens-style industrial stylesheet
# ---------------------------------------------------------------------------

SIEMENS_STYLE = """
QMainWindow, QWidget {
    background-color: #f0f0f0;
    color: #1a1a1a;
    font-family: "Segoe UI", "Microsoft YaHei", "PingFang SC", sans-serif;
    font-size: 12px;
}
QMenuBar {
    background-color: #e8e8e8; border-bottom: 1px solid #c0c0c0;
    padding: 2px; color: #1a1a1a;
}
QMenuBar::item:selected { background-color: #cce4f7; }
QMenu { background-color: #ffffff; border: 1px solid #c0c0c0; }
QMenu::item:selected { background-color: #cce4f7; }
QToolBar {
    background-color: #e8e8e8; border-bottom: 1px solid #c0c0c0;
    padding: 3px; spacing: 6px;
}
QTreeWidget {
    background-color: #ffffff; border: 1px solid #c0c0c0;
    outline: none; font-size: 13px;
}
QTreeWidget::item {
    padding: 8px 6px; border-bottom: 1px solid #e8e8e8;
}
QTreeWidget::item:selected { background-color: #cce4f7; color: #1a1a1a; }
QTreeWidget::item:hover { background-color: #e5f0fb; }
QGroupBox {
    font-weight: 600; border: 1px solid #c0c0c0; border-radius: 3px;
    margin-top: 14px; padding: 16px 12px 12px 12px;
    background-color: #ffffff; color: #1a1a1a;
}
QGroupBox::title {
    subcontrol-origin: margin; left: 12px; padding: 0 8px;
    color: #005c8a; background-color: #f0f0f0;
}
QLabel { color: #1a1a1a; }
QLabel#sectionTitle {
    font-size: 15px; font-weight: 700; color: #005c8a;
    padding: 4px 0; border-bottom: 2px solid #0080c8;
}
QLabel#bigData {
    font-family: Consolas, "Courier New", monospace;
    font-size: 22px; font-weight: 700; color: #005c8a;
    padding: 10px; border: 1px solid #c0c0c0; border-radius: 3px;
    background-color: #f8f8f8;
}
QLabel#smallData {
    font-family: Consolas, "Courier New", monospace;
    font-size: 16px; font-weight: 700; color: #005c8a;
    padding: 8px; border: 1px solid #c0c0c0; border-radius: 3px;
    background-color: #f8f8f8;
}
QLabel#statusLed {
    min-width: 14px; min-height: 14px; max-width: 14px; max-height: 14px;
    border-radius: 7px; border: 1px solid #999;
}
QLabel#statusLed[on="true"] { background-color: #00a651; border-color: #008a44; }
QLabel#statusLed[on="false"] { background-color: #e04040; border-color: #c03030; }
QPushButton {
    min-height: 28px; padding: 5px 14px; border-radius: 3px; font-weight: 600;
    border: 1px solid #b0b0b0; background-color: #e0e0e0; color: #1a1a1a;
}
QPushButton:hover { background-color: #d0d0d0; border-color: #999; }
QPushButton:pressed { background-color: #c0c0c0; }
QPushButton:disabled { background-color: #e8e8e8; color: #999; border-color: #d0d0d0; }
QPushButton#primaryBtn {
    background-color: #0080c8; color: #ffffff; border: 1px solid #006ba0;
}
QPushButton#primaryBtn:hover { background-color: #0070b0; }
QPushButton#primaryBtn:pressed { background-color: #005c8a; }
QPushButton#dangerBtn {
    background-color: #e04040; color: #ffffff; border: 1px solid #c03030;
}
QPushButton#dangerBtn:hover { background-color: #d03030; }
QPushButton#successBtn {
    background-color: #00a651; color: #ffffff; border: 1px solid #008a44;
}
QPushButton#successBtn:hover { background-color: #009040; }
QPushButton#successBtn[checkable="true"] {
    background-color: #b8b8b8; color: #2a2a2a; border: 1px solid #999;
}
QPushButton#successBtn[checkable="true"]:checked {
    background-color: #00a651; color: #ffffff; border: 1px solid #008a44;
}
QLineEdit, QComboBox {
    padding: 5px 7px; border: 1px solid #b0b0b0; border-radius: 3px;
    background-color: #ffffff; color: #1a1a1a;
    selection-background-color: #0080c8; selection-color: #ffffff;
}
QLineEdit:focus, QComboBox:focus { border: 1px solid #0080c8; }
QLineEdit:read-only { background-color: #f0f0f0; color: #555; }
QCheckBox { spacing: 8px; color: #1a1a1a; }
QCheckBox::indicator {
    width: 16px; height: 16px; border: 1px solid #888;
    border-radius: 3px; background-color: #ffffff;
}
QCheckBox::indicator:checked { background-color: #0080c8; border-color: #006ba0; }
QTextEdit {
    font-family: Consolas, "Courier New", monospace; font-size: 11px;
    background-color: #ffffff; border: 1px solid #c0c0c0; color: #1a1a1a;
}
QProgressBar {
    border: 1px solid #c0c0c0; border-radius: 3px; text-align: center;
    background-color: #e8e8e8; min-height: 22px; color: #1a1a1a;
}
QProgressBar::chunk { background-color: #0080c8; border-radius: 2px; }
QStatusBar {
    background-color: #e0e0e0; border-top: 1px solid #c0c0c0;
    color: #1a1a1a; font-size: 12px;
}
QStatusBar QLabel { padding: 0 12px; }
QFrame#globalStatusBar {
    background-color: #2a2a2a; border-bottom: 1px solid #444;
    padding: 2px 8px;
}
QFrame#globalStatusBar QLabel {
    color: #cccccc; font-size: 10px; padding: 0 2px;
}
QLabel#globalLed {
    min-width: 12px; min-height: 12px; max-width: 12px; max-height: 12px;
    border-radius: 6px; border: 1px solid #666;
}
QLabel#globalLed[on="true"] { background-color: #00a651; border-color: #008a44; }
QLabel#globalLed[on="false"] { background-color: #555; border-color: #444; }
QLabel#globalLed[on="warn"] { background-color: #e04040; border-color: #c03030; }
"""


# ---------------------------------------------------------------------------
# Main Window
# ---------------------------------------------------------------------------

class ODMRControlGUI(QMainWindow):

    def __init__(self, cmd_service: CommandService | None = None) -> None:
        super().__init__()
        self.setWindowTitle("ODMR 控制系统 / ODMR Control")
        self.setMinimumSize(1200, 800)

        # config
        self._cfg = load_config()

        # core — 复用 CommandService 持有的 InstrumentController，避免重复实例
        self._cmd_service = cmd_service
        if self._cmd_service is not None:
            self._ctrl = self._cmd_service._ctrl
            # 订阅 CommandService 的统一广播
            self._cmd_service.smb_state_broadcast.connect(self._on_smb_state_changed)
            self._cmd_service.lockin_data_broadcast.connect(self._on_lockin_data_ready)
            self._cmd_service.lockin_status_broadcast.connect(self._on_lockin_status_changed)
            self._cmd_service.lockin_batch_broadcast.connect(self._on_lockin_batch_ready)
            self._cmd_service.laser_state_broadcast.connect(self._on_laser_state_changed)
            self._cmd_service.error_occurred.connect(self._on_error)
            self._cmd_service.log_requested.connect(self._on_log)
        else:
            # 向后兼容：直接订阅 InstrumentController
            self._ctrl = InstrumentController(self)
            self._ctrl.smb_state_changed.connect(self._on_smb_state_changed)
            self._ctrl.lockin_data_ready.connect(self._on_lockin_data_ready)
            self._ctrl.lockin_batch_ready.connect(self._on_lockin_batch_ready)
            self._ctrl.error_occurred.connect(self._on_error)
            self._ctrl.log_requested.connect(self._on_log)
            self._ctrl.lockin_status_changed.connect(self._on_lockin_status_changed)
            self._ctrl.laser_state_changed.connect(self._on_laser_state_changed)

        self._sweep_engine = SweepEngine(self._ctrl, self)
        self._sweep_engine.step_started.connect(self._on_sweep_step_started)
        self._sweep_engine.sweep_finished.connect(self._on_sweep_finished)
        self._sweep_engine.error_occurred.connect(self._on_error)
        self._sweep_engine.log_requested.connect(self._on_log)

        # data buffers
        self._buffer = CircularBuffer(
            channels=["X", "Y", "R", "theta", "smb_freq_hz"],
            capacity=2000,
        )

        # recording
        self._recorder: Optional[ODMRRecorder] = None
        self._recording = False

        # async command tracking (for CommandService migration)
        self._pending_cmds: Dict[str, tuple] = {}
        if self._cmd_service is not None:
            self._cmd_service.command_completed.connect(self._on_command_completed)
            self._cmd_service.command_error.connect(self._on_command_error)

        # logger
        self._setup_error_logger()

        # UI
        self.setStyleSheet(SIEMENS_STYLE)
        self._build_menu()
        self._build_toolbar()
        self._build_central()
        self._build_statusbar()

        # display update timer (50ms, decoupled from data rate)
        self._display_timer = QTimer(self)
        self._display_timer.timeout.connect(self._on_display_tick)
        self._display_timer.start(50)

    def _setup_error_logger(self) -> None:
        log_dir = Path(__file__).parent.parent / "errors"
        log_dir.mkdir(exist_ok=True)
        date_str = datetime.datetime.now().strftime("%Y%m%d")
        handler = logging.FileHandler(log_dir / f"{date_str}.Log", encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
        logger = logging.getLogger("odmr")
        logger.addHandler(handler)
        logger.setLevel(logging.WARNING)

    # -----------------------------------------------------------------------
    # Menu bar
    # -----------------------------------------------------------------------

    def _build_menu(self) -> None:
        menubar = self.menuBar()

        file_menu = menubar.addMenu("文件 / File (&F)")
        act_save_cfg = QAction("保存配置 / Save Config", self)
        act_save_cfg.triggered.connect(self._save_config)
        file_menu.addAction(act_save_cfg)
        file_menu.addSeparator()
        act_quit = QAction("退出 / Quit (&Q)", self)
        act_quit.triggered.connect(self.close)
        file_menu.addAction(act_quit)

        conn_menu = menubar.addMenu("连接 / Connection (&C)")
        act_conn_all = QAction("全部连接 / Connect All", self)
        act_conn_all.triggered.connect(self._connect_all)
        conn_menu.addAction(act_conn_all)
        act_disconn_all = QAction("全部断开 / Disconnect All", self)
        act_disconn_all.triggered.connect(self._disconnect_all)
        conn_menu.addAction(act_disconn_all)

        settings_menu = menubar.addMenu("设置 / Settings (&S)")
        act_monitor_interval = QAction("监控间隔 / Monitor Interval", self)
        act_monitor_interval.triggered.connect(self._change_monitor_interval)
        settings_menu.addAction(act_monitor_interval)

    # -----------------------------------------------------------------------
    # Toolbar
    # -----------------------------------------------------------------------

    def _build_toolbar(self) -> None:
        tb = QToolBar("工具栏 / Toolbar")
        tb.setMovable(False)
        self.addToolBar(tb)

        conn_all_btn = QPushButton("全部连接 / Connect All")
        conn_all_btn.setObjectName("primaryBtn")
        conn_all_btn.clicked.connect(self._connect_all)
        tb.addWidget(conn_all_btn)

        disconn_btn = QPushButton("全部断开 / Disconnect All")
        disconn_btn.clicked.connect(self._disconnect_all)
        tb.addWidget(disconn_btn)

        # 弹簧将急停推到最右侧
        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        tb.addWidget(spacer)

        estop_btn = QPushButton("急停 / E-Stop")
        estop_btn.setObjectName("dangerBtn")
        estop_btn.setMinimumHeight(32)
        estop_btn.setMinimumWidth(120)
        estop_btn.clicked.connect(self._emergency_stop)
        tb.addWidget(estop_btn)

    # -----------------------------------------------------------------------
    # Status bar
    # -----------------------------------------------------------------------

    def _build_statusbar(self) -> None:
        sb = QStatusBar()
        self.setStatusBar(sb)

        self._status_smb = QLabel("SMB: 未连接")
        self._status_smb.setMinimumWidth(200)
        sb.addWidget(self._status_smb)

        self._status_lockin = QLabel("Lockin: 未连接")
        self._status_lockin.setMinimumWidth(200)
        sb.addWidget(self._status_lockin)

        self._status_laser = QLabel("Laser: 未连接")
        self._status_laser.setMinimumWidth(200)
        sb.addWidget(self._status_laser)

        self._status_sweep = QLabel("扫频: 空闲")
        sb.addPermanentWidget(self._status_sweep)

        self._status_rec = QLabel("记录: 关")
        sb.addPermanentWidget(self._status_rec)

    # -----------------------------------------------------------------------
    # Central: left nav tree + right stacked pages
    # -----------------------------------------------------------------------

    def _build_central(self) -> None:
        splitter = QSplitter(Qt.Horizontal)

        # left: navigation tree
        self._nav = QTreeWidget()
        self._nav.setHeaderHidden(True)
        self._nav.setMinimumWidth(200)
        self._nav.setMaximumWidth(260)
        nav_items = [
            ("设备连接 / Connection", 0),
            ("参数配置 / Parameters", 1),
            ("扫频实验 / Sweep", 2),
            ("实时波形 / Waveform", 3),
            ("状态监控 / Monitor", 4),
            ("日志 / Log", 5),
        ]
        for label, idx in nav_items:
            item = QTreeWidgetItem([label])
            item.setData(0, Qt.UserRole, idx)
            self._nav.addTopLevelItem(item)
        self._nav.currentItemChanged.connect(self._on_nav_changed)
        splitter.addWidget(self._nav)

        # right: global status bar + stacked pages
        right_widget = QWidget()
        right_layout = QVBoxLayout(right_widget)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(0)

        # Global status bar (always visible)
        self._global_status_bar = self._build_global_status_bar()
        right_layout.addWidget(self._global_status_bar)

        # Stacked pages (6 pages)
        self._stack = QStackedWidget()
        self._stack.addWidget(self._build_connection_page())       # 0: 设备连接
        self._stack.addWidget(self._build_param_config_page())     # 1: 参数配置
        self._stack.addWidget(self._build_sweep_experiment_page()) # 2: 扫频实验
        self._stack.addWidget(self._build_waveform_page())         # 3: 实时波形
        self._stack.addWidget(self._build_monitor_page())          # 4: 状态监控
        self._stack.addWidget(self._build_log_page())              # 5: 日志
        right_layout.addWidget(self._stack, 1)

        splitter.addWidget(right_widget)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        self.setCentralWidget(splitter)
        self._nav.setCurrentItem(self._nav.topLevelItem(0))

    def _on_nav_changed(self, current, _previous) -> None:
        if current is not None:
            idx = current.data(0, Qt.UserRole)
            self._stack.setCurrentIndex(idx)
            # RALL? lifecycle: waveform page (idx=3) needs RALL? data
            if idx == 3:
                self._start_waveform_acquire()
            else:
                self._stop_waveform_acquire()

    # -----------------------------------------------------------------------
    # Global Status Bar
    # -----------------------------------------------------------------------

    def _build_global_status_bar(self) -> QFrame:
        """全局状态栏：始终可见，显示所有设备关键状态。"""
        bar = QFrame()
        bar.setObjectName("globalStatusBar")
        bar.setFixedHeight(32)
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(8, 2, 8, 2)
        layout.setSpacing(4)

        def _led() -> QLabel:
            led = QLabel()
            led.setObjectName("globalLed")
            led.setProperty("on", "false")
            return led

        def _sep() -> QFrame:
            sep = QFrame()
            sep.setFrameShape(QFrame.VLine)
            sep.setStyleSheet("color: #555;")
            return sep

        def _txt(text: str) -> QLabel:
            return QLabel(text)

        # SMB100A
        layout.addWidget(_txt("SMB:"))
        self._gs_smb_rf = _led()
        layout.addWidget(self._gs_smb_rf)
        layout.addWidget(_txt("RF"))
        self._gs_smb_lf = _led()
        layout.addWidget(self._gs_smb_lf)
        layout.addWidget(_txt("LF"))
        self._gs_smb_mod = _led()
        layout.addWidget(self._gs_smb_mod)
        layout.addWidget(_txt("Mod"))

        layout.addWidget(_sep())

        # OE1022D CH-A
        layout.addWidget(_txt("A:"))
        self._gs_lockin_a_ov = _led()
        layout.addWidget(self._gs_lockin_a_ov)
        layout.addWidget(_txt("OV"))
        self._gs_lockin_a_pll = _led()
        layout.addWidget(self._gs_lockin_a_pll)
        layout.addWidget(_txt("PLL"))

        # OE1022D CH-B
        layout.addWidget(_txt("B:"))
        self._gs_lockin_b_ov = _led()
        layout.addWidget(self._gs_lockin_b_ov)
        layout.addWidget(_txt("OV"))
        self._gs_lockin_b_pll = _led()
        layout.addWidget(self._gs_lockin_b_pll)
        layout.addWidget(_txt("PLL"))

        layout.addWidget(_sep())

        # Laser
        layout.addWidget(_txt("Laser:"))
        self._gs_laser_out = _led()
        layout.addWidget(self._gs_laser_out)
        layout.addWidget(_txt("Out"))

        layout.addWidget(_sep())

        # Sweep / Recording status
        self._gs_sweep = _txt("扫频: 空闲")
        layout.addWidget(self._gs_sweep)
        layout.addWidget(_sep())
        self._gs_rec = _txt("记录: 关")
        layout.addWidget(self._gs_rec)

        layout.addStretch()
        return bar


    # -----------------------------------------------------------------------
    # Page 0: Connection
    # -----------------------------------------------------------------------

    def _build_connection_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)

        title = QLabel("设备连接 / Device Connection")
        title.setObjectName("sectionTitle")
        layout.addWidget(title)

        # SMB100A
        smb_group = QGroupBox("SMB100A 信号源 / Signal Generator")
        smb_layout = QGridLayout(smb_group)

        # Row 0: protocol + address + scan + connect + LED
        smb_layout.addWidget(QLabel("协议 / Protocol:"), 0, 0)
        self._smb_protocol = QComboBox()
        self._smb_protocol.addItems(["USB", "GPIB", "LAN", "Serial"])
        self._smb_protocol.currentTextChanged.connect(self._on_smb_protocol_changed)
        smb_layout.addWidget(self._smb_protocol, 0, 1)

        self._smb_addr_input = QLineEdit(self._cfg["smb"]["visa_address"])
        self._smb_addr_input.setPlaceholderText("USB0::0x0AAD::0x0054::106789::INSTR")
        smb_layout.addWidget(self._smb_addr_input, 0, 2)

        smb_scan_btn = QPushButton("自动扫描 / Auto Scan")
        smb_scan_btn.setToolTip("扫描所有 VISA 资源并识别罗德施瓦茨设备")
        smb_scan_btn.clicked.connect(self._scan_rs_devices)
        smb_layout.addWidget(smb_scan_btn, 0, 3)

        self._smb_conn_btn = QPushButton("连接 / Connect")
        self._smb_conn_btn.setObjectName("primaryBtn")
        self._smb_conn_btn.clicked.connect(self._toggle_smb_connection)
        smb_layout.addWidget(self._smb_conn_btn, 0, 4)

        self._smb_led = QLabel()
        self._smb_led.setObjectName("statusLed")
        self._smb_led.setProperty("on", "false")
        smb_layout.addWidget(self._smb_led, 0, 5, alignment=Qt.AlignCenter)

        # Row 1: protocol hint
        self._smb_protocol_hint = QLabel(
            "USB: USB0::0x0AAD::0x0054::SN::INSTR | GPIB: GPIB0::28::INSTR | LAN: TCPIP0::IP::inst0::INSTR"
        )
        self._smb_protocol_hint.setStyleSheet("color: #666; font-size: 11px;")
        smb_layout.addWidget(self._smb_protocol_hint, 1, 0, 1, 5)

        # Row 2: status label (embedded in device area)
        self._smb_status_label = QLabel("未连接 / Disconnected")
        self._smb_status_label.setStyleSheet("color: #999; font-weight: 600;")
        smb_layout.addWidget(self._smb_status_label, 2, 0, 1, 5)

        layout.addWidget(smb_group)

        # OE1022D
        lockin_group = QGroupBox("OE1022D 锁相放大器 / Lock-in Amplifier")
        lockin_layout = QGridLayout(lockin_group)

        lockin_layout.addWidget(QLabel("串口 / Port:"), 0, 0)
        self._lockin_port_combo = QComboBox()
        self._lockin_port_combo.setEditable(True)
        self._lockin_port_combo.addItems([f"COM{i}" for i in range(1, 21)])
        try:
            from serial.tools import list_ports
            for port in list_ports.comports():
                if self._lockin_port_combo.findText(port.device) < 0:
                    self._lockin_port_combo.addItem(port.device, port.device)
        except Exception:
            pass
        self._lockin_port_combo.setCurrentText(self._cfg["lockin"]["port"])
        lockin_layout.addWidget(self._lockin_port_combo, 0, 1)

        lockin_layout.addWidget(QLabel("波特率 / Baud:"), 0, 2)
        self._lockin_baud_combo = QComboBox()
        self._lockin_baud_combo.addItems(["9600", "19200", "38400", "57600", "115200", "921600"])
        self._lockin_baud_combo.setCurrentText(str(self._cfg["lockin"]["baudrate"]))
        lockin_layout.addWidget(self._lockin_baud_combo, 0, 3)

        lockin_scan_btn = QPushButton("扫描 / Scan")
        lockin_scan_btn.setToolTip("扫描串口并自动识别 OE1022D (IDN验证)")
        lockin_scan_btn.clicked.connect(self._scan_lockin_ports)
        lockin_layout.addWidget(lockin_scan_btn, 0, 4)

        self._lockin_conn_btn = QPushButton("连接 / Connect")
        self._lockin_conn_btn.setObjectName("primaryBtn")
        self._lockin_conn_btn.clicked.connect(self._toggle_lockin_connection)
        lockin_layout.addWidget(self._lockin_conn_btn, 0, 5)

        self._lockin_led = QLabel()
        self._lockin_led.setObjectName("statusLed")
        self._lockin_led.setProperty("on", "false")
        lockin_layout.addWidget(self._lockin_led, 0, 6, alignment=Qt.AlignCenter)

        # Row 1: status label (embedded in device area)
        self._lockin_status_label = QLabel("未连接 / Disconnected")
        self._lockin_status_label.setStyleSheet("color: #999; font-weight: 600;")
        lockin_layout.addWidget(self._lockin_status_label, 1, 0, 1, 6)

        layout.addWidget(lockin_group)

        # Laser
        laser_group = QGroupBox("MSL-U 激光器 / Laser")
        laser_layout = QGridLayout(laser_group)

        laser_layout.addWidget(QLabel("串口 / Port:"), 0, 0)
        self._laser_port_combo = QComboBox()
        self._laser_port_combo.setEditable(True)
        self._laser_port_combo.addItems([f"COM{i}" for i in range(1, 21)])
        self._laser_port_combo.setCurrentText(self._cfg["laser"]["port"])
        laser_layout.addWidget(self._laser_port_combo, 0, 1)

        laser_layout.addWidget(QLabel("波特率 / Baud:"), 0, 2)
        self._laser_baud_combo = QComboBox()
        self._laser_baud_combo.addItems(["9600", "19200", "38400", "57600", "115200"])
        self._laser_baud_combo.setCurrentText(str(self._cfg["laser"]["baudrate"]))
        laser_layout.addWidget(self._laser_baud_combo, 0, 3)

        laser_scan_btn = QPushButton("扫描 / Scan")
        laser_scan_btn.setToolTip("扫描串口查找可用激光器端口")
        laser_scan_btn.clicked.connect(self._scan_laser_ports)
        laser_layout.addWidget(laser_scan_btn, 0, 4)

        self._laser_conn_btn = QPushButton("连接 / Connect")
        self._laser_conn_btn.setObjectName("primaryBtn")
        self._laser_conn_btn.clicked.connect(self._toggle_laser_connection)
        laser_layout.addWidget(self._laser_conn_btn, 0, 5)

        self._laser_led = QLabel()
        self._laser_led.setObjectName("statusLed")
        self._laser_led.setProperty("on", "false")
        laser_layout.addWidget(self._laser_led, 0, 6, alignment=Qt.AlignCenter)

        # Row 1: status label
        self._laser_status_label = QLabel("未连接 / Disconnected")
        self._laser_status_label.setStyleSheet("color: #999; font-weight: 600;")
        laser_layout.addWidget(self._laser_status_label, 1, 0, 1, 6)

        layout.addWidget(laser_group)

        # Laser Control
        laser_ctrl_group = QGroupBox("激光器控制 / Laser Control")
        laser_ctrl_layout = QGridLayout(laser_ctrl_group)
        laser_ctrl_layout.addWidget(QLabel("功率 (mW):"), 0, 0)
        self._laser_power_edit = QLineEdit("0")
        self._laser_power_edit.setValidator(QIntValidator(0, 999))
        self._laser_power_edit.setFixedWidth(80)
        laser_ctrl_layout.addWidget(self._laser_power_edit, 0, 1)
        laser_power_btn = QPushButton("设 / Set")
        laser_power_btn.setObjectName("primaryBtn")
        laser_power_btn.clicked.connect(self._set_laser_power)
        laser_ctrl_layout.addWidget(laser_power_btn, 0, 2)
        self._laser_max_label = QLabel(f"Max: {self._cfg['laser'].get('max_power_mw', 150)} mW")
        self._laser_max_label.setStyleSheet("color: #666; font-size: 11px;")
        laser_ctrl_layout.addWidget(self._laser_max_label, 0, 3)
        self._laser_output_toggle = QCheckBox("激光输出 / Laser Output")
        self._laser_output_toggle.stateChanged.connect(self._toggle_laser_output)
        laser_ctrl_layout.addWidget(self._laser_output_toggle, 1, 0, 1, 2)
        laser_ctrl_layout.setColumnStretch(4, 1)
        layout.addWidget(laser_ctrl_group)

        layout.addStretch()
        return page

    # -----------------------------------------------------------------------
    # Page 1: Real-time Monitor
    # -----------------------------------------------------------------------

    def _build_monitor_page(self) -> QWidget:
        page = QScrollArea()
        page.setWidgetResizable(True)
        inner = QWidget()
        layout = QVBoxLayout(inner)

        title = QLabel("实时监控 / Real-time Monitor")
        title.setObjectName("sectionTitle")
        layout.addWidget(title)

        # -- SMB100A Parameters ------------------------------------------------
        smb_params_group = QGroupBox("SMB100A 参数 / SMB100A Parameters")
        smb_params_layout = QGridLayout(smb_params_group)
        self._smb_param_displays: Dict[str, QLabel] = {}
        smb_params = [
            ("频率 / Freq", "freq", "GHz", "%.6f"),
            ("功率 / Power", "power", "dBm", "%.2f"),
            ("RF 输出 / RF Out", "rf_out", "", "%s"),
            ("模式 / Mode", "mode", "", "%s"),
            ("LF 输出 / LF Out", "lf_out", "", "%s"),
            ("LF 频率 / LF Freq", "lf_freq", "Hz", "%.1f"),
            ("调制 / Mod", "mod", "", "%s"),
        ]
        for idx, (label_text, key, unit, fmt) in enumerate(smb_params):
            row = idx // 4
            col = (idx % 4) * 2
            smb_params_layout.addWidget(QLabel(label_text + ":"), row, col)
            disp = QLabel("---")
            disp.setStyleSheet("font-weight: 600; color: #005c8a;")
            smb_params_layout.addWidget(disp, row, col + 1)
            self._smb_param_displays[key] = (disp, unit, fmt)
        layout.addWidget(smb_params_group)

        # -- Laser Parameters --------------------------------------------------
        laser_params_group = QGroupBox("激光器参数 / Laser Parameters")
        laser_params_layout = QGridLayout(laser_params_group)
        self._laser_param_displays: Dict[str, QLabel] = {}
        laser_params = [
            ("功率 / Power", "power", "mW", "%.1f"),
            ("输出 / Output", "output", "", "%s"),
            ("波长 / Wavelength", "wavelength", "nm", "%.5f"),
        ]
        for idx, (label_text, key, unit, fmt) in enumerate(laser_params):
            row = idx // 3
            col = (idx % 3) * 2
            laser_params_layout.addWidget(QLabel(label_text + ":"), row, col)
            disp = QLabel("---")
            disp.setStyleSheet("font-weight: 600; color: #005c8a;")
            laser_params_layout.addWidget(disp, row, col + 1)
            self._laser_param_displays[key] = (disp, unit, fmt)
        layout.addWidget(laser_params_group)

        # -- Lock-in Amplifier (Channel A/B tabs) ------------------------------
        lockin_tabs = QTabWidget()
        self._lockin_ch_displays: Dict[int, Dict[str, QLabel]] = {}
        self._lockin_ch_buffers: Dict[int, CircularBuffer] = {}
        for ch_num in [1, 2]:
            ch_page = QWidget()
            ch_layout = QHBoxLayout(ch_page)
            displays: Dict[str, QLabel] = {}
            for label_text, key in [("X (mV)", "X"), ("Y (mV)", "Y"), ("R (mV)", "R"), ("theta (deg)", "theta")]:
                w = QWidget()
                vl = QVBoxLayout(w)
                vl.addWidget(QLabel(label_text))
                disp = QLabel("0.0000")
                disp.setObjectName("smallData")
                disp.setAlignment(Qt.AlignCenter)
                vl.addWidget(disp)
                ch_layout.addWidget(w)
                displays[key] = disp
            self._lockin_ch_displays[ch_num] = displays
            self._lockin_ch_buffers[ch_num] = CircularBuffer(
                channels=["X", "Y", "R", "theta", "smb_freq_hz"],
                capacity=2000,
            )
            lockin_tabs.addTab(ch_page, f"Channel {'A' if ch_num == 1 else 'B'}")
        layout.addWidget(lockin_tabs)

        layout.addStretch()
        page.setWidget(inner)
        return page

    def _change_monitor_interval(self) -> None:
        from PyQt5.QtWidgets import QInputDialog
        current_ms = 94
        ms, ok = QInputDialog.getInt(
            self, "监控间隔 / Monitor Interval",
            "间隔 (ms):", current_ms, 50, 5000, 10,
        )
        if ok:
            self._ctrl.set_lockin_monitor_interval(ms)
            self._on_log(f"监控间隔设为 {ms} ms")

    # -----------------------------------------------------------------------
    # Page 1: Parameter Config (Source + Lock-in tabs)
    # -----------------------------------------------------------------------

    def _build_param_config_page(self) -> QWidget:
        """参数配置页面：微波源 + 锁相 CH-A + 锁相 CH-B 三个 Tab。"""
        page = QWidget()
        layout = QVBoxLayout(page)
        tabs = QTabWidget()
        tabs.addTab(self._build_source_page(), "微波源 / Microwave")
        tabs.addTab(self._build_lockin_control_page(), "锁相控制 / Lock-in")
        layout.addWidget(tabs)
        return page

    # -----------------------------------------------------------------------
    # Microwave Source Control Tab
    # -----------------------------------------------------------------------

    def _build_source_page(self) -> QWidget:
        page = QScrollArea()
        page.setWidgetResizable(True)
        inner = QWidget()
        layout = QVBoxLayout(inner)

        title = QLabel("微波源控制 / Microwave Source Control")
        title.setObjectName("sectionTitle")
        layout.addWidget(title)

        cfg_sweep = self._cfg["sweep"]
        amp_installed = self._cfg["smb"].get("amplifier_installed", True)
        self._max_power_dbm = 10.0 if amp_installed else 25.0

        # -- Output Toggles ----------------------------------------------------
        out_group = QGroupBox("输出控制 / Output Control")
        out_layout = QHBoxLayout(out_group)
        self._rf_toggle = QCheckBox("RF 输出 / RF Output")
        self._rf_toggle.stateChanged.connect(self._toggle_rf_output)
        out_layout.addWidget(self._rf_toggle)
        self._lf_toggle = QCheckBox("LF 输出 / LF Output")
        self._lf_toggle.stateChanged.connect(self._toggle_lf_output)
        out_layout.addWidget(self._lf_toggle)
        self._fm_toggle = QCheckBox("FM 调制 / FM Modulation")
        self._fm_toggle.stateChanged.connect(self._toggle_fm_mod)
        out_layout.addWidget(self._fm_toggle)
        out_layout.addStretch()
        layout.addWidget(out_group)

        # -- RF Frequency ------------------------------------------------------
        freq_group = QGroupBox("RF 频率 / Frequency")
        freq_layout = QGridLayout(freq_group)
        freq_layout.addWidget(QLabel("CW 频率 (Hz):"), 0, 0)
        self._cw_freq_edit = QLineEdit(str(cfg_sweep["cw_freq_hz"]))
        freq_layout.addWidget(self._cw_freq_edit, 0, 1)
        btn = QPushButton("设 / Set")
        btn.setObjectName("primaryBtn")
        btn.clicked.connect(lambda: self._set_cw_freq(self._cw_freq_edit.text()))
        freq_layout.addWidget(btn, 0, 2)
        freq_layout.setColumnStretch(3, 1)
        layout.addWidget(freq_group)

        # -- RF Power (with safety limit display) ------------------------------
        power_group = QGroupBox("RF 功率 / Level")
        power_layout = QGridLayout(power_group)
        power_layout.addWidget(QLabel("功率 (dBm):"), 0, 0)
        self._power_edit = QLineEdit(str(cfg_sweep["power_dbm"]))
        self._power_edit.textChanged.connect(self._on_power_text_changed)
        power_layout.addWidget(self._power_edit, 0, 1)
        self._power_limit_label = QLabel(f"Max: {self._max_power_dbm:.0f} dBm")
        self._power_limit_label.setStyleSheet("color: #666; font-size: 11px;")
        power_layout.addWidget(self._power_limit_label, 0, 2)
        btn = QPushButton("设 / Set")
        btn.setObjectName("primaryBtn")
        btn.clicked.connect(lambda: self._set_power(self._power_edit.text()))
        power_layout.addWidget(btn, 0, 3)
        # Amplifier note
        amp_note = QLabel(
            f"{'已接放大器 / Amplifier installed' if amp_installed else '未接放大器 / No amplifier'} — "
            f"安全限制 {self._max_power_dbm:.0f} dBm"
        )
        amp_note.setStyleSheet("color: #e04040; font-size: 11px; font-weight: 600;")
        power_layout.addWidget(amp_note, 1, 0, 1, 4)
        power_layout.setColumnStretch(4, 1)
        layout.addWidget(power_group)

        # -- Modulation --------------------------------------------------------
        mod_group = QGroupBox("调制 / Modulation")
        mod_layout = QGridLayout(mod_group)
        mod_layout.addWidget(QLabel("FM 偏差 (Hz):"), 0, 0)
        self._fm_dev_edit = QLineEdit(str(cfg_sweep["fm_dev_hz"]))
        mod_layout.addWidget(self._fm_dev_edit, 0, 1)
        btn = QPushButton("设 / Set")
        btn.setObjectName("primaryBtn")
        btn.clicked.connect(lambda: self._set_fm_dev(self._fm_dev_edit.text()))
        mod_layout.addWidget(btn, 0, 2)
        mod_layout.setColumnStretch(3, 1)
        layout.addWidget(mod_group)

        # -- LF Generator ------------------------------------------------------
        lf_group = QGroupBox("LF 发生器 / LF Generator")
        lf_layout = QGridLayout(lf_group)
        lf_layout.addWidget(QLabel("LF 频率 (Hz):"), 0, 0)
        self._lf_freq_edit = QLineEdit(str(cfg_sweep["lf_freq_hz"]))
        lf_layout.addWidget(self._lf_freq_edit, 0, 1)
        btn = QPushButton("设 / Set")
        btn.setObjectName("primaryBtn")
        btn.clicked.connect(lambda: self._set_lf_freq(self._lf_freq_edit.text()))
        lf_layout.addWidget(btn, 0, 2)
        lf_layout.addWidget(QLabel("LF 幅度 (mV):"), 1, 0)
        self._lf_amp_edit = QLineEdit(str(cfg_sweep["lf_amp_mv"]))
        lf_layout.addWidget(self._lf_amp_edit, 1, 1)
        btn2 = QPushButton("设 / Set")
        btn2.setObjectName("primaryBtn")
        btn2.clicked.connect(lambda: self._set_lf_amp(self._lf_amp_edit.text()))
        lf_layout.addWidget(btn2, 1, 2)
        lf_layout.addWidget(QLabel("LF 波形:"), 2, 0)
        self._lf_shape_combo = QComboBox()
        self._lf_shape_combo.addItems(["SINE", "SQUARE", "TRIANGLE", "SAWTOOTH", "ISAWTOOTH"])
        self._lf_shape_combo.setCurrentText(cfg_sweep["lf_shape"])
        lf_layout.addWidget(self._lf_shape_combo, 2, 1)
        btn3 = QPushButton("设 / Set")
        btn3.setObjectName("primaryBtn")
        btn3.clicked.connect(self._set_lf_shape)
        lf_layout.addWidget(btn3, 2, 2)
        lf_layout.setColumnStretch(3, 1)
        layout.addWidget(lf_group)

        # -- Apply All ---------------------------------------------------------
        apply_all_btn = QPushButton("应用所有参数 / Apply All")
        apply_all_btn.setObjectName("primaryBtn")
        apply_all_btn.clicked.connect(self._apply_all_params)
        layout.addWidget(apply_all_btn)

        layout.addStretch()
        page.setWidget(inner)
        return page

    # -----------------------------------------------------------------------
    # Page 3: Lock-in Control
    # -----------------------------------------------------------------------

    def _build_lockin_control_page(self) -> QWidget:
        page = QScrollArea()
        page.setWidgetResizable(True)
        inner = QWidget()
        layout = QVBoxLayout(inner)

        title = QLabel("锁相控制 / Lock-in Control")
        title.setObjectName("sectionTitle")
        layout.addWidget(title)

        # 全局通道选择
        ch_layout = QHBoxLayout()
        ch_layout.addWidget(QLabel("目标通道 / Target Channel:"))
        self._lockin_ctrl_channel = QComboBox()
        self._lockin_ctrl_channel.addItems(["Channel A", "Channel B"])
        ch_layout.addWidget(self._lockin_ctrl_channel)
        ch_layout.addStretch()
        layout.addLayout(ch_layout)

        def add_param_group(parent_layout, title, params):
            """辅助函数：创建参数分组（中英双语标签）。"""
            group = QGroupBox(title)
            gl = QGridLayout(group)
            gl.setColumnMinimumWidth(0, 240)
            widgets = {}
            for row, (label, key, default, wtype, *args) in enumerate(params):
                lbl = QLabel(label)
                lbl.setWordWrap(True)
                gl.addWidget(lbl, row, 0)
                if wtype == "combo":
                    w = QComboBox()
                    w.addItems(args[0])
                    w.setCurrentIndex(default)
                else:
                    w = QLineEdit(str(default))
                gl.addWidget(w, row, 1)
                btn = QPushButton("Set")
                btn.setObjectName("primaryBtn")
                gl.addWidget(btn, row, 2)
                widgets[key] = (w, btn)
            gl.setColumnStretch(3, 1)
            parent_layout.addWidget(group)
            return widgets

        # 输入与滤波器 / INPUT / FILTERS
        self._lockin_input_widgets = add_param_group(
            layout, "输入与滤波器 / INPUT / FILTERS",
            [
                ("输入模式：A=单端, AB=差分, I=电流\nSource (0=A,1=AB,2=I6,3=I8)", "source", 0, "combo", ["A", "AB", "I(10^6)", "I(10^8)"]),
                ("电流增益（放大倍数）\nCurrent Gain (0=1,1=10,2=100)", "gain", 0, "combo", ["1", "10", "100"]),
                ("接地方式：浮地 / 接地\nGrounding (0=Float,1=Ground)", "ground", 0, "combo", ["Float", "Ground"]),
                ("耦合方式：AC=交流(0.16Hz高通), DC=直流\nCoupling (0=AC,1=DC)", "coupling", 0, "combo", ["AC", "DC"]),
                ("工频陷波器：关 / 50Hz / 50+100Hz / 100Hz\nLine Notch (0=Off,1=50Hz,2=50+100,3=100Hz)", "notch", 1, "combo", ["Off", "50Hz", "50+100Hz", "100Hz"]),
            ]
        )
        for key, (w, btn) in self._lockin_input_widgets.items():
            btn.clicked.connect(lambda _k=key, _w=w: self._set_lockin_input(_k, _w))

        # 参考与相位 / REF / PHASE
        self._lockin_ref_widgets = add_param_group(
            layout, "参考与相位 / REF / PHASE",
            [
                ("参考相位（-180°~+180°, 精度0.01°）\nPhase (deg)", "phase", "0", "line"),
                ("参考信号源：外部 / 内部\nSource (0=Ext,1=Int)", "ref_source", 0, "combo", ["External", "Internal"]),
                ("外部参考类型：正弦 / 上升沿 / 下降沿\nSlope (0=Sine,1=PosTTL,2=NegTTL)", "slope", 0, "combo", ["Sine", "Pos TTL", "Neg TTL"]),
                ("内部参考频率（1mHz~102kHz）\nFrequency (Hz)", "freq", "1000", "line"),
                ("谐波检测次数（限制: 次数×频率<102kHz）\nHarmonic", "harmonic", "1", "line"),
            ]
        )
        for key, (w, btn) in self._lockin_ref_widgets.items():
            btn.clicked.connect(lambda _k=key, _w=w: self._set_lockin_ref(_k, _w))

        # 增益与时间常数 / GAIN / TC
        self._lockin_gain_widgets = add_param_group(
            layout, "增益与时间常数 / GAIN / TC",
            [
                ("满偏灵敏度（1nV~1V, 1-2-5步进）\nSensitivity (index)", "sens", "10", "line"),
                ("动态储备：低 / 普通 / 高\nReserve (0=Low,1=Normal,2=High)", "reserve", 1, "combo", ["低 Low", "普通 Normal", "高 High"]),
                ("时间常数（10μs~3000s）\nTime Constant (index)", "tc", "6", "line"),
                ("低通滤波陡降（6/12/18/24 dB/oct）\nFilter dB/oct (0=6,1=12,2=18,3=24)", "filter", 2, "combo", ["6dB", "12dB", "18dB", "24dB"]),
                ("同步滤波器（≤200Hz时有效）\nSync Filter (0=Off,1=On)", "sync", 1, "combo", ["Off", "On"]),
            ]
        )
        for key, (w, btn) in self._lockin_gain_widgets.items():
            btn.clicked.connect(lambda _k=key, _w=w: self._set_lockin_gain(_k, _w))

        # 通道输出 / CHANNEL OUTPUT
        self._lockin_output_widgets = add_param_group(
            layout, "通道输出 / CHANNEL OUTPUT",
            [
                ("输出通道号\nOutput CH (1 or 2)", "out_ch", "1", "line"),
                ("输出源（X/Y/R/θ/各谐波/Noise/AUXOUT）\nSource (index)", "out_source", "0", "line"),
                ("偏置（-100%~+100%）\nOffset (-10000~10000)", "out_offset", "0", "line"),
                ("扩展倍数\nExpand (0=1,1=10,2=100)", "out_expand", 0, "combo", ["1", "10", "100"]),
            ]
        )
        for key, (w, btn) in self._lockin_output_widgets.items():
            btn.clicked.connect(lambda _k=key, _w=w: self._set_lockin_output(_k, _w))

        # 自动设置 / AUTO SET
        auto_group = QGroupBox("自动设置 / AUTO SET")
        auto_layout = QHBoxLayout(auto_group)
        for label, tip, cmd_type in [
            ("Auto Gain\n自动灵敏度", "根据R值自动调整灵敏度，约5秒", CommandType.LOCKIN_AUTO_GAIN),
            ("Auto Reserve\n自动储备", "选取当前信号的最小动态储备", CommandType.LOCKIN_AUTO_RESERVE),
            ("Auto Phase\n自动移相", "使输入信号相位为0°，约5秒", CommandType.LOCKIN_AUTO_PHASE),
        ]:
            btn = QPushButton(label)
            btn.setObjectName("primaryBtn")
            btn.setToolTip(tip)
            btn.setMinimumHeight(48)
            btn.clicked.connect(lambda _ct=cmd_type: self._send_lockin_cmd(_ct))
            auto_layout.addWidget(btn)
        auto_layout.addStretch()
        layout.addWidget(auto_group)

        layout.addStretch()
        page.setWidget(inner)
        return page

    def _get_lockin_ctrl_channel(self) -> int:
        return self._lockin_ctrl_channel.currentIndex() + 1

    def _set_lockin_input(self, key: str, widget) -> None:
        ch = self._get_lockin_ctrl_channel()
        params: Dict[str, Any] = {"channel": ch}
        for k, (w, _btn) in self._lockin_input_widgets.items():
            if isinstance(w, QComboBox):
                params[k] = w.currentIndex()
            else:
                try:
                    params[k] = int(w.text())
                except ValueError:
                    params[k] = 0
        self._send_lockin_cmd(CommandType.LOCKIN_SET_INPUT, params)

    def _set_lockin_ref(self, key: str, widget) -> None:
        ch = self._get_lockin_ctrl_channel()
        params: Dict[str, Any] = {"channel": ch}
        w_phase = self._lockin_ref_widgets["phase"][0]
        params["phase_deg"] = float(w_phase.text()) if isinstance(w_phase, QLineEdit) else 0.0
        w_source = self._lockin_ref_widgets["ref_source"][0]
        params["source"] = w_source.currentIndex() if isinstance(w_source, QComboBox) else 0
        w_slope = self._lockin_ref_widgets["slope"][0]
        params["slope"] = w_slope.currentIndex() if isinstance(w_slope, QComboBox) else 0
        w_freq = self._lockin_ref_widgets["freq"][0]
        params["freq_hz"] = float(w_freq.text()) if isinstance(w_freq, QLineEdit) else 1000.0
        w_harm = self._lockin_ref_widgets["harmonic"][0]
        params["harmonic"] = int(w_harm.text()) if isinstance(w_harm, QLineEdit) else 1
        self._send_lockin_cmd(CommandType.LOCKIN_SET_REF_PHASE, params)

    def _set_lockin_gain(self, key: str, widget) -> None:
        ch = self._get_lockin_ctrl_channel()
        params: Dict[str, Any] = {"channel": ch}
        w_sens = self._lockin_gain_widgets["sens"][0]
        params["sensitivity"] = int(w_sens.text()) if isinstance(w_sens, QLineEdit) else 10
        w_reserve = self._lockin_gain_widgets["reserve"][0]
        params["reserve"] = w_reserve.currentIndex() if isinstance(w_reserve, QComboBox) else 1
        w_tc = self._lockin_gain_widgets["tc"][0]
        params["time_const"] = int(w_tc.text()) if isinstance(w_tc, QLineEdit) else 6
        w_filter = self._lockin_gain_widgets["filter"][0]
        params["filter_db"] = w_filter.currentIndex() if isinstance(w_filter, QComboBox) else 2
        w_sync = self._lockin_gain_widgets["sync"][0]
        params["sync"] = w_sync.currentIndex() == 1 if isinstance(w_sync, QComboBox) else True
        self._send_lockin_cmd(CommandType.LOCKIN_SET_GAIN_TC, params)

    def _set_lockin_output(self, key: str, widget) -> None:
        ch = self._get_lockin_ctrl_channel()
        params: Dict[str, Any] = {"channel": ch}
        w_ch = self._lockin_output_widgets["out_ch"][0]
        params["output_ch"] = int(w_ch.text()) if isinstance(w_ch, QLineEdit) else 1
        w_src = self._lockin_output_widgets["out_source"][0]
        params["source"] = int(w_src.text()) if isinstance(w_src, QLineEdit) else 0
        w_off = self._lockin_output_widgets["out_offset"][0]
        params["offset"] = int(w_off.text()) if isinstance(w_off, QLineEdit) else 0
        w_exp = self._lockin_output_widgets["out_expand"][0]
        params["expand"] = w_exp.currentIndex() if isinstance(w_exp, QComboBox) else 0
        self._send_lockin_cmd(CommandType.LOCKIN_SET_OUTPUT, params)

    def _send_lockin_cmd(self, cmd_type: CommandType, params: Dict[str, Any] | None = None) -> None:
        if params is None:
            params = {"channel": self._get_lockin_ctrl_channel()}
        try:
            if self._cmd_service is not None:
                self._cmd_service.submit(Command(cmd_type, params, source="gui"))
            else:
                # 向后兼容：直接调用 driver
                self._ctrl.lockin._exchange_ascii(f"AGAND {params.get('channel', 1)}")
            self._on_log(f"Lockin command {cmd_type.name} sent", "lockin")
        except Exception as exc:
            self._on_error(f"Lockin command failed: {exc}")

    def _browse_save_dir(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "选择保存目录", self._save_dir_input.text())
        if d:
            self._save_dir_input.setText(d)

    # -----------------------------------------------------------------------
    # Page 3: Real-time Waveform
    # -----------------------------------------------------------------------

    # 波形参数定义：(显示名, buffer key, 颜色, 默认勾选, Y轴组)
    _WAVE_PARAMS = [
        ("X",   "X",      "#0080c8", True,  "amp"),
        ("Y",   "Y",      "#00a651", True,  "amp"),
        ("R",   "R",      "#e04040", True,  "amp"),
        ("θ",    "theta",    "#ff8c00", True,  "amp"),
        ("freq", "freq",     "#808080", False, "other"),
        ("noise","noise",    "#c0c0c0", False, "other"),
        ("Xh1",  "Xh1",     "#4080c8", True,  "amp"),
        ("Yh1",  "Yh1",     "#40a651", True,  "amp"),
        ("Rh1",  "Rh1",     "#e08080", False, "amp"),
        ("θh1",  "theta_h1","#c08000", False, "amp"),
        ("Xh2",  "Xh2",     "#8080c8", False, "amp"),
        ("Yh2",  "Yh2",     "#80a651", False, "amp"),
        ("Rh2",  "Rh2",     "#e0a0a0", False, "amp"),
        ("θh2",  "theta_h2","#806000", False, "amp"),
    ]

    # CircularBuffer 通道列表（波形专用）
    _WAVE_BUFFER_CHANNELS = [p[1] for p in _WAVE_PARAMS]

    def _build_waveform_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(4, 4, 4, 4)

        title = QLabel("实时波形 / Real-time Waveform")
        title.setObjectName("sectionTitle")
        layout.addWidget(title)

        # -- Control bar -------------------------------------------------------
        ctrl_bar = QHBoxLayout()
        ctrl_bar.addWidget(QLabel("通道:"))
        self._wave_ch_select = QComboBox()
        self._wave_ch_select.addItems(["Channel A", "Channel B"])
        self._wave_ch_select.currentIndexChanged.connect(self._on_wave_channel_changed)
        ctrl_bar.addWidget(self._wave_ch_select)

        self._wave_pause_btn = QPushButton("暂停 ⏸")
        self._wave_pause_btn.setCheckable(True)
        self._wave_pause_btn.clicked.connect(self._on_wave_pause_toggle)
        ctrl_bar.addWidget(self._wave_pause_btn)

        clear_btn = QPushButton("清空 ✕")
        clear_btn.clicked.connect(self._on_wave_clear)
        ctrl_bar.addWidget(clear_btn)

        self._wave_auto_y = QCheckBox("Auto Y")
        self._wave_auto_y.setChecked(True)
        ctrl_bar.addWidget(self._wave_auto_y)

        ctrl_bar.addStretch()

        self._wave_status_label = QLabel("等待数据...")
        self._wave_status_label.setStyleSheet("color: #888; font-size: 11px;")
        ctrl_bar.addWidget(self._wave_status_label)

        layout.addLayout(ctrl_bar)

        # -- Parameter checkboxes ----------------------------------------------
        param_group = QGroupBox("显示参数 / Display Parameters")
        param_grid = QGridLayout(param_group)
        param_grid.setContentsMargins(8, 12, 8, 8)
        param_grid.setSpacing(4)
        self._wave_checkboxes: Dict[str, QCheckBox] = {}

        # Row 0: 基波
        param_grid.addWidget(QLabel("<b>基波:</b>"), 0, 0)
        for i, (label, key, color, default, _axis) in enumerate(self._WAVE_PARAMS[:6]):
            cb = QCheckBox(label)
            cb.setChecked(default)
            cb.setStyleSheet(f"QCheckBox {{ color: {color}; }}")
            cb.toggled.connect(lambda checked, _k=key: self._on_wave_param_toggled(_k, checked))
            param_grid.addWidget(cb, 0, 1 + i)
            self._wave_checkboxes[key] = cb

        # Row 1: 谐波1
        param_grid.addWidget(QLabel("<b>谐波1:</b>"), 1, 0)
        for i, (label, key, color, default, _axis) in enumerate(self._WAVE_PARAMS[6:10]):
            cb = QCheckBox(label)
            cb.setChecked(default)
            cb.setStyleSheet(f"QCheckBox {{ color: {color}; }}")
            cb.toggled.connect(lambda checked, _k=key: self._on_wave_param_toggled(_k, checked))
            param_grid.addWidget(cb, 1, 1 + i)
            self._wave_checkboxes[key] = cb

        # Row 2: 谐波2
        param_grid.addWidget(QLabel("<b>谐波2:</b>"), 2, 0)
        for i, (label, key, color, default, _axis) in enumerate(self._WAVE_PARAMS[10:]):
            cb = QCheckBox(label)
            cb.setChecked(default)
            cb.setStyleSheet(f"QCheckBox {{ color: {color}; }}")
            cb.toggled.connect(lambda checked, _k=key: self._on_wave_param_toggled(_k, checked))
            param_grid.addWidget(cb, 2, 1 + i)
            self._wave_checkboxes[key] = cb

        layout.addWidget(param_group)

        # -- Plot widget -------------------------------------------------------
        if _HAS_PYG:
            self._wave_plot = pg.PlotWidget()
            self._wave_plot.setLabel("left", "幅度", units="mV")
            self._wave_plot.setLabel("bottom", "时间", units="s")
            self._wave_plot.addLegend()
            self._wave_plot.showGrid(x=True, y=True, alpha=0.3)
            # 零线
            self._wave_zero_line = pg.InfiniteLine(
                pos=0, angle=0, pen=pg.mkPen("#999", width=1, style=Qt.DotLine)
            )
            self._wave_plot.addItem(self._wave_zero_line)

            self._wave_curves: Dict[str, pg.PlotDataItem] = {}
            for label, key, color, _default, _axis in self._WAVE_PARAMS:
                curve = self._wave_plot.plot(pen=pg.mkPen(color, width=1.5), name=label)
                self._wave_curves[key] = curve

            layout.addWidget(self._wave_plot, 1)
        else:
            layout.addWidget(QLabel("pyqtgraph 未安装，波形显示不可用"))
            self._wave_plot = None
            self._wave_curves = {}

        # -- Waveform buffers --------------------------------------------------
        self._waveform_buffers: Dict[int, CircularBuffer] = {
            1: CircularBuffer(channels=self._WAVE_BUFFER_CHANNELS, capacity=5000),
            2: CircularBuffer(channels=self._WAVE_BUFFER_CHANNELS, capacity=5000),
        }
        self._waveform_paused = False
        self._waveform_page_active = False
        self._waveform_rall_running = False
        self._waveform_data_count = 0

        # -- Recording control -------------------------------------------------
        rec_group = QGroupBox("数据记录 / Data Recording")
        rec_layout = QVBoxLayout(rec_group)

        rec_dir_layout = QHBoxLayout()
        rec_dir_layout.addWidget(QLabel("保存目录:"))
        self._save_dir_input = QLineEdit(self._cfg["acquisition"]["save_dir"])
        rec_dir_layout.addWidget(self._save_dir_input, 1)
        browse_btn = QPushButton("浏览 / Browse")
        browse_btn.clicked.connect(self._browse_save_dir)
        rec_dir_layout.addWidget(browse_btn)
        rec_layout.addLayout(rec_dir_layout)

        rec_btn_layout = QHBoxLayout()
        self._rec_toggle_btn = QPushButton("开始记录 / Start Recording")
        self._rec_toggle_btn.setObjectName("primaryBtn")
        self._rec_toggle_btn.setCheckable(True)
        self._rec_toggle_btn.clicked.connect(self._toggle_recording)
        rec_btn_layout.addWidget(self._rec_toggle_btn)
        self._rec_status_label = QLabel("未记录 / Not recording")
        self._rec_status_label.setAlignment(Qt.AlignCenter)
        self._rec_status_label.setStyleSheet(
            "padding: 8px; border-radius: 3px; font-weight: 700; background-color: #e8e8e8;"
        )
        rec_btn_layout.addWidget(self._rec_status_label)
        rec_btn_layout.addStretch()
        rec_layout.addLayout(rec_btn_layout)

        self._rec_file_label = QLabel("---")
        self._rec_file_label.setWordWrap(True)
        self._rec_file_label.setStyleSheet("color: #666; font-size: 11px;")
        rec_layout.addWidget(self._rec_file_label)

        layout.addWidget(rec_group)

        return page

    def _on_wave_pause_toggle(self, checked: bool) -> None:
        self._waveform_paused = checked
        self._wave_pause_btn.setText("继续 ▶" if checked else "暂停 ⏸")

    def _on_wave_clear(self) -> None:
        for buf in self._waveform_buffers.values():
            buf.clear()
        self._waveform_data_count = 0
        for curve in self._wave_curves.values():
            curve.setData([], [])

    def _on_wave_param_toggled(self, key: str, checked: bool) -> None:
        if key in self._wave_curves:
            if not checked:
                self._wave_curves[key].setData([], [])

    def _on_wave_channel_changed(self, idx: int) -> None:
        """通道切换：清空曲线，等待新数据。"""
        for curve in self._wave_curves.values():
            curve.setData([], [])

    def _start_waveform_acquire(self) -> None:
        """启动 RALL? 波形采集（页面切换到波形时调用）。"""
        self._waveform_page_active = True
        if not self._waveform_rall_running and self._ctrl.is_lockin_connected:
            try:
                self._ctrl.start_lockin_acquire(None)
                self._waveform_rall_running = True
                self._on_log("[Waveform] RALL? 启动", "lockin")
            except Exception as exc:
                self._waveform_rall_running = False
                self._on_error(f"[Waveform] RALL? 启动失败: {exc}")

    def _stop_waveform_acquire(self) -> None:
        """停止 RALL? 波形采集（除非正在记录数据）。"""
        self._waveform_page_active = False
        # 如果正在记录数据，不停止 RALL?
        if self._recording:
            return
        if self._waveform_rall_running:
            try:
                self._ctrl.stop_lockin_acquire()
                self._waveform_rall_running = False
                self._on_log("[Waveform] RALL? 停止", "lockin")
            except Exception:
                pass

    # -----------------------------------------------------------------------
    # Page 2: Sweep Experiment (Sweep Config + Acquisition + Controls)
    # -----------------------------------------------------------------------

    def _build_sweep_experiment_page(self) -> QWidget:
        page = QScrollArea()
        page.setWidgetResizable(True)
        inner = QWidget()
        layout = QVBoxLayout(inner)

        title = QLabel("扫频实验 / Sweep Experiment")
        title.setObjectName("sectionTitle")
        layout.addWidget(title)

        cfg_sweep = self._cfg["sweep"]

        # -- Sweep parameters -------------------------------------------------
        sweep_group = QGroupBox("扫频参数 / Sweep Parameters")
        sweep_layout = QGridLayout(sweep_group)
        sweep_layout.addWidget(QLabel("起始频率 (Hz):"), 0, 0)
        self._start_freq_edit = QLineEdit(str(cfg_sweep["start_freq_hz"]))
        sweep_layout.addWidget(self._start_freq_edit, 0, 1)
        sweep_layout.addWidget(QLabel("终止频率 (Hz):"), 0, 2)
        self._stop_freq_edit = QLineEdit(str(cfg_sweep["stop_freq_hz"]))
        sweep_layout.addWidget(self._stop_freq_edit, 0, 3)
        sweep_layout.addWidget(QLabel("步进 (Hz):"), 1, 0)
        self._step_edit = QLineEdit(str(cfg_sweep["step_hz"]))
        sweep_layout.addWidget(self._step_edit, 1, 1)
        sweep_layout.addWidget(QLabel("驻留 (ms):"), 1, 2)
        self._dwell_edit = QLineEdit(str(cfg_sweep["dwell_ms"]))
        sweep_layout.addWidget(self._dwell_edit, 1, 3)
        sweep_layout.addWidget(QLabel("功率 (dBm):"), 2, 0)
        self._sweep_power_edit = QLineEdit(str(cfg_sweep["power_dbm"]))
        sweep_layout.addWidget(self._sweep_power_edit, 2, 1)
        sweep_layout.setColumnStretch(4, 1)
        layout.addWidget(sweep_group)

        # -- Cycle settings ---------------------------------------------------
        cycle_group = QGroupBox("循环设置 / Cycle Settings")
        cycle_layout = QGridLayout(cycle_group)
        cycle_layout.addWidget(QLabel("循环次数 / Cycles:"), 0, 0)
        self._cycle_count_input = QLineEdit("1")
        self._cycle_count_input.setValidator(QIntValidator(1, 9999))
        self._cycle_count_input.setFixedWidth(80)
        cycle_layout.addWidget(self._cycle_count_input, 0, 1)
        cycle_layout.addWidget(QLabel("循环间隔 (ms):"), 0, 2)
        self._cycle_interval_input = QLineEdit("200")
        self._cycle_interval_input.setValidator(QIntValidator(0, 3600000))
        self._cycle_interval_input.setFixedWidth(80)
        cycle_layout.addWidget(self._cycle_interval_input, 0, 3)
        layout.addWidget(cycle_group)

        # -- Control buttons --------------------------------------------------
        btn_layout = QHBoxLayout()
        self._start_sweep_btn = QPushButton("开始扫频 / Start Sweep")
        self._start_sweep_btn.setObjectName("primaryBtn")
        self._start_sweep_btn.clicked.connect(self._start_sweep)
        btn_layout.addWidget(self._start_sweep_btn)
        self._stop_sweep_btn = QPushButton("停止扫频 / Stop Sweep")
        self._stop_sweep_btn.setObjectName("dangerBtn")
        self._stop_sweep_btn.clicked.connect(self._stop_sweep)
        self._stop_sweep_btn.setEnabled(False)
        btn_layout.addWidget(self._stop_sweep_btn)
        btn_layout.addStretch()
        layout.addLayout(btn_layout)

        # Progress
        self._sweep_progress = QProgressBar()
        self._sweep_progress.setRange(0, 100)
        self._sweep_progress.setValue(0)
        layout.addWidget(self._sweep_progress)

        # -- Acquisition settings ---------------------------------------------
        acq_group = QGroupBox("采集设置 / Acquisition Settings")
        acq_layout = QVBoxLayout(acq_group)
        self._auto_save_check = QCheckBox("扫频时自动保存 / Auto Save on Sweep")
        self._auto_save_check.setChecked(self._cfg["acquisition"]["auto_save"])
        acq_layout.addWidget(self._auto_save_check)
        layout.addWidget(acq_group)

        layout.addStretch()
        page.setWidget(inner)
        return page

    def _toggle_recording(self, checked: bool) -> None:
        """独立于扫频的数据记录控制。"""
        if checked:
            if not self._ctrl.is_lockin_connected:
                QMessageBox.warning(self, "警告", "请先连接锁相放大器")
                self._rec_toggle_btn.setChecked(False)
                return
            out_dir = self._save_dir_input.text()
            if self._cmd_service is not None:
                req = self._cmd_service.submit(Command(
                    CommandType.ACQ_START_RECORDING,
                    {"output_dir": out_dir},
                    source="gui",
                ))
                self._pending_cmds[req] = ("record_start", out_dir)
                self._rec_status_label.setText("正在启动 / Starting...")
                self._rec_toggle_btn.setEnabled(False)
            else:
                self._recorder = ODMRRecorder(out_dir)
                self._recorder.start_recording()
                self._ctrl.start_lockin_acquire(self._recorder)
                self._apply_recording_started(str(self._recorder.output_dir))
        else:
            if self._cmd_service is not None:
                req = self._cmd_service.submit(Command(
                    CommandType.ACQ_STOP_RECORDING,
                    {"stop_acquire": not self._waveform_page_active},
                    source="gui",
                ))
                self._pending_cmds[req] = ("record_stop", None)
                self._rec_status_label.setText("正在停止 / Stopping...")
                self._rec_toggle_btn.setEnabled(False)
            else:
                if self._recording and not self._waveform_page_active:
                    self._ctrl.stop_lockin_acquire()
                elif self._recorder is not None:
                    self._recorder.stop_recording()
                self._apply_recording_stopped()

    def _apply_recording_started(self, output_dir: str) -> None:
        self._rec_file_label.setText(output_dir)
        self._rec_status_label.setText("记录中 / Recording")
        self._rec_status_label.setStyleSheet(
            "padding: 8px; border-radius: 3px; font-weight: 700; background-color: #cce4f7;"
        )
        self._rec_toggle_btn.setText("停止记录 / Stop Recording")
        self._rec_toggle_btn.setChecked(True)
        self._rec_toggle_btn.setEnabled(True)
        self._status_rec.setText("Record: ON")
        self._recording = True

    def _apply_recording_stopped(self) -> None:
        self._rec_status_label.setText("未记录 / Not recording")
        self._rec_status_label.setStyleSheet(
            "padding: 8px; border-radius: 3px; font-weight: 700; background-color: #e8e8e8;"
        )
        self._rec_toggle_btn.setText("开始记录 / Start Recording")
        self._rec_toggle_btn.setChecked(False)
        self._rec_toggle_btn.setEnabled(True)
        self._status_rec.setText("Record: OFF")
        self._recording = False

    # -----------------------------------------------------------------------
    # Page 6: Log
    # -----------------------------------------------------------------------

    def _build_log_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)

        title = QLabel("日志 / Log")
        title.setObjectName("sectionTitle")
        layout.addWidget(title)

        self._log_tabs = QTabWidget()
        self._smb_log = QTextEdit()
        self._smb_log.setReadOnly(True)
        self._lockin_log = QTextEdit()
        self._lockin_log.setReadOnly(True)
        self._serial_log = QTextEdit()
        self._serial_log.setReadOnly(True)

        self._log_tabs.addTab(self._smb_log, "SMB100A")
        self._log_tabs.addTab(self._lockin_log, "OE1022D")
        self._log_tabs.addTab(self._serial_log, "串口原始 / Serial Raw")
        layout.addWidget(self._log_tabs, 1)

        btn_layout = QHBoxLayout()
        btn_layout.addStretch()
        clear_smb = QPushButton("清空 SMB")
        clear_smb.clicked.connect(self._smb_log.clear)
        btn_layout.addWidget(clear_smb)
        clear_lockin = QPushButton("清空 Lockin")
        clear_lockin.clicked.connect(self._lockin_log.clear)
        btn_layout.addWidget(clear_lockin)
        clear_serial = QPushButton("清空 Serial")
        clear_serial.clicked.connect(self._serial_log.clear)
        btn_layout.addWidget(clear_serial)
        layout.addLayout(btn_layout)

        return page


    def _connect_all(self):
        if not self._ctrl.is_smb_connected:
            self._toggle_smb_connection()
        if not self._ctrl.is_lockin_connected:
            self._toggle_lockin_connection()
        if not self._ctrl.is_laser_connected:
            self._toggle_laser_connection()

    def _disconnect_all(self):
        if self._ctrl.is_smb_connected:
            self._toggle_smb_connection()
        if self._ctrl.is_lockin_connected:
            self._toggle_lockin_connection()
        if self._ctrl.is_laser_connected:
            self._toggle_laser_connection()

    def _toggle_smb_connection(self):
        if self._ctrl.is_smb_connected:
            # Disconnect
            if self._cmd_service is not None:
                cmd = Command(CommandType.SMB_DISCONNECT, source="gui")
                req = self._cmd_service.submit(cmd)
                self._pending_cmds[req] = ("smb_disconnect", None)
            else:
                self._ctrl.disconnect_smb()
                self._smb_conn_btn.setText("连接 / Connect")
                self._smb_led.setProperty("on", "false")
                self._smb_led.style().unpolish(self._smb_led)
                self._smb_led.style().polish(self._smb_led)
                self._smb_status_label.setText("未连接 / Disconnected")
                self._smb_status_label.setStyleSheet("color: #999;")
                self._status_smb.setText("SMB: 未连接")
                self._on_log("SMB100A disconnected", "smb")
        else:
            addr = self._smb_addr_input.text().strip()
            if not addr:
                QMessageBox.warning(self, "Warning", "Please enter VISA address")
                return
            if self._cmd_service is not None:
                cmd = Command(
                    CommandType.SMB_CONNECT,
                    {"address": addr, "timeout_ms": 10000},
                    source="gui",
                )
                req = self._cmd_service.submit(cmd)
                self._pending_cmds[req] = ("smb_connect", addr)
            else:
                try:
                    idn = self._ctrl.connect_smb(addr)
                    self._smb_conn_btn.setText("断开 / Disconnect")
                    self._smb_led.setProperty("on", "true")
                    self._smb_led.style().unpolish(self._smb_led)
                    self._smb_led.style().polish(self._smb_led)
                    self._smb_status_label.setText(f"已连接 / Connected: {idn[:30]}")
                    self._smb_status_label.setStyleSheet("color: #00a651; font-weight: 600;")
                    self._status_smb.setText("SMB: " + idn[:30])
                    self._on_log("SMB100A connected: " + idn, "smb")
                except Exception as exc:
                    QMessageBox.critical(self, "Connection Error", str(exc))
                    self._on_log("SMB100A connect failed: " + str(exc), "smb")

    def _toggle_lockin_connection(self):
        if self._ctrl.is_lockin_connected:
            # Disconnect
            if self._cmd_service is not None:
                cmd = Command(CommandType.LOCKIN_DISCONNECT, source="gui")
                req = self._cmd_service.submit(cmd)
                self._pending_cmds[req] = ("lockin_disconnect", None)
            else:
                self._ctrl.disconnect_lockin()
                self._lockin_conn_btn.setText("连接 / Connect")
                self._lockin_led.setProperty("on", "false")
                self._lockin_led.style().unpolish(self._lockin_led)
                self._lockin_led.style().polish(self._lockin_led)
                self._lockin_status_label.setText("未连接 / Disconnected")
                self._lockin_status_label.setStyleSheet("color: #999;")
                self._status_lockin.setText("Lockin: 未连接")
                self._on_log("OE1022D disconnected", "lockin")
        else:
            port = self._lockin_port_combo.currentData() or self._lockin_port_combo.currentText()
            baud = int(self._lockin_baud_combo.currentText())
            if self._cmd_service is not None:
                cmd = Command(
                    CommandType.LOCKIN_CONNECT,
                    {"port": port, "baudrate": baud},
                    source="gui",
                )
                req = self._cmd_service.submit(cmd)
                self._pending_cmds[req] = ("lockin_connect", port)
            else:
                try:
                    idn = self._ctrl.connect_lockin(port, baud)
                    self._lockin_conn_btn.setText("断开 / Disconnect")
                    self._lockin_led.setProperty("on", "true")
                    self._lockin_led.style().unpolish(self._lockin_led)
                    self._lockin_led.style().polish(self._lockin_led)
                    self._lockin_status_label.setText(f"已连接 / Connected: {idn[:30]}")
                    self._lockin_status_label.setStyleSheet("color: #00a651; font-weight: 600;")
                    self._status_lockin.setText("Lockin: " + idn[:30])
                    self._on_log("OE1022D connected: " + idn, "lockin")
                except Exception as exc:
                    QMessageBox.critical(self, "Connection Error", str(exc))
                    self._on_log("OE1022D connect failed: " + str(exc), "lockin")

    def _toggle_laser_connection(self):
        if self._ctrl.is_laser_connected:
            # Disconnect
            if self._cmd_service is not None:
                cmd = Command(CommandType.LASER_DISCONNECT, source="gui")
                req = self._cmd_service.submit(cmd)
                self._pending_cmds[req] = ("laser_disconnect", None)
            else:
                self._ctrl.disconnect_laser()
                self._laser_conn_btn.setText("连接 / Connect")
                self._laser_led.setProperty("on", "false")
                self._laser_led.style().unpolish(self._laser_led)
                self._laser_led.style().polish(self._laser_led)
                self._laser_status_label.setText("未连接 / Disconnected")
                self._laser_status_label.setStyleSheet("color: #999;")
                self._status_laser.setText("Laser: 未连接")
                self._on_log("Laser disconnected", "laser")
        else:
            port = self._laser_port_combo.currentData() or self._laser_port_combo.currentText()
            baud = int(self._laser_baud_combo.currentText())
            if self._cmd_service is not None:
                cmd = Command(
                    CommandType.LASER_CONNECT,
                    {"port": port, "baudrate": baud},
                    source="gui",
                )
                req = self._cmd_service.submit(cmd)
                self._pending_cmds[req] = ("laser_connect", port)
            else:
                try:
                    idn = self._ctrl.connect_laser(port, baud)
                    self._laser_conn_btn.setText("断开 / Disconnect")
                    self._laser_led.setProperty("on", "true")
                    self._laser_led.style().unpolish(self._laser_led)
                    self._laser_led.style().polish(self._laser_led)
                    self._laser_status_label.setText(f"已连接 / Connected: {idn[:30]}")
                    self._laser_status_label.setStyleSheet("color: #00a651; font-weight: 600;")
                    self._status_laser.setText("Laser: " + idn[:30])
                    self._on_log("Laser connected: " + idn, "laser")
                except Exception as exc:
                    QMessageBox.critical(self, "Connection Error", str(exc))
                    self._on_log("Laser connect failed: " + str(exc), "laser")

    @staticmethod
    def _extract_smb_serial(idn: str) -> str:
        """从 SMB100A IDN 字符串中提取序列号。

        IDN 格式: Rohde&Schwarz,<type>,<partno>/<serial>,<firmware>
        """
        try:
            parts = idn.split(",")
            if len(parts) >= 3:
                serial_part = parts[2].split("/")[-1].strip()
                return serial_part
        except Exception:
            pass
        return ""

    def _scan_rs_devices(self):
        """扫描所有 VISA 资源并识别罗德施瓦茨设备。"""
        try:
            from instruments.smb100a import SMB100ADriver
            devices = SMB100ADriver.scan_rs_devices(timeout_ms=500)
            if not devices:
                QMessageBox.information(self, "Scan Result", "未发现罗德施瓦茨设备")
                return

            # 检查 smb_bindings，自动选中已知设备
            bindings = self._cfg.get("smb_bindings", {})
            auto_select_idx = -1
            items = []
            for i, (addr, idn) in enumerate(devices):
                serial = self._extract_smb_serial(idn)
                label = f"{addr}  |  {idn[:50]}"
                if serial and serial in bindings:
                    label += "  [已知]"
                items.append(label)
                if serial and serial in bindings and bindings[serial] == addr:
                    auto_select_idx = i

            from PyQt5.QtWidgets import QInputDialog
            if auto_select_idx >= 0:
                default_idx = auto_select_idx
                self._on_log(f"Auto-matched SMB100A: {devices[auto_select_idx][0]}", "smb")
            else:
                default_idx = 0

            item, ok = QInputDialog.getItem(
                self, "选择设备 / Select Device", "发现的设备 / Found devices:", items, default_idx, False
            )
            if ok and item:
                idx = items.index(item)
                addr, idn = devices[idx]
                self._smb_addr_input.setText(addr)
                self._on_log(f"Selected SMB100A: {addr} ({idn[:40]})", "smb")
        except Exception as exc:
            QMessageBox.warning(self, "Scan Failed", str(exc))
            self._on_log("SMB scan failed: " + str(exc), "smb")

    def _scan_lockin_ports(self):
        """扫描串口并自动识别 OE1022D（IDN 验证）。"""
        try:
            from instruments.oe1022d import OE1022DDriver
            results = OE1022DDriver.scan_ports_with_idn(
                baudrate=int(self._lockin_baud_combo.currentText()),
                timeout=0.5,
            )
            # 分离 OE1022D 和其他设备
            oe1022d_ports = [(port, idn) for port, idn in results if idn]
            other_ports = [port for port, idn in results if not idn]

            # 更新下拉框：OE1022D 优先，带 IDN 标注
            self._lockin_port_combo.clear()
            for port, idn in oe1022d_ports:
                self._lockin_port_combo.addItem(f"{port}  ({idn[:30]})", port)
            for port in other_ports:
                self._lockin_port_combo.addItem(port, port)

            # 如果有绑定配置，尝试自动匹配
            bindings = self._cfg.get("lockin_bindings", {})
            for port, idn in oe1022d_ports:
                if idn in bindings:
                    # 已知设备，优先选中
                    idx = self._lockin_port_combo.findData(port)
                    if idx >= 0:
                        self._lockin_port_combo.setCurrentIndex(idx)
                        self._on_log(f"Auto-matched known OE1022D: {port} ({idn[:30]})", "lockin")
                        break

            if oe1022d_ports:
                self._on_log(f"Found {len(oe1022d_ports)} OE1022D, {len(other_ports)} other ports", "lockin")
            else:
                self._on_log(f"No OE1022D found. Scanned {len(other_ports)} ports", "lockin")
        except Exception as exc:
            QMessageBox.warning(self, "Scan Failed", str(exc))
            self._on_log("Lockin scan failed: " + str(exc), "lockin")

    def _scan_laser_ports(self):
        """扫描串口查找可用激光器端口。"""
        try:
            from instruments.laser_msl import LaserMSLDriver
            results = LaserMSLDriver.scan_ports(
                baudrate=int(self._laser_baud_combo.currentText()),
                timeout=0.5,
            )
            self._laser_port_combo.clear()
            if results:
                for port, desc in results:
                    self._laser_port_combo.addItem(f"{port}  ({desc})", port)
                self._on_log(f"Found {len(results)} available port(s)", "laser")
            else:
                self._laser_port_combo.addItems([f"COM{i}" for i in range(1, 21)])
                self._on_log("No available ports found", "laser")
        except Exception as exc:
            QMessageBox.warning(self, "Scan Failed", str(exc))
            self._on_log("Laser scan failed: " + str(exc), "laser")

    def _on_smb_protocol_changed(self, protocol: str):
        """根据协议更新地址输入框的 placeholder 和提示。"""
        hints = {
            "USB": "USB0::0x0AAD::0x0054::SN::INSTR",
            "GPIB": "GPIB0::28::INSTR",
            "LAN": "TCPIP0::192.168.1.100::inst0::INSTR",
            "Serial": "ASRL1::INSTR",
        }
        self._smb_addr_input.setPlaceholderText(hints.get(protocol, ""))
        protocol_desc = {
            "USB": "USB: 通过 USBTMC 连接，地址格式 USB0::VID::PID::SN::INSTR",
            "GPIB": "GPIB: 通过 GPIB 接口连接，地址格式 GPIB0::地址::INSTR",
            "LAN": "LAN: 通过 VXI-11/HiSLIP/Raw Socket 连接，地址格式 TCPIP0::IP::inst0::INSTR",
            "Serial": "Serial: 通过 RS-232 串口连接，地址格式 ASRLn::INSTR",
        }
        self._smb_protocol_hint.setText(protocol_desc.get(protocol, ""))

    def _emergency_stop(self):
        """急停：通过 CommandService 统一执行（若可用），确保日志和安全策略完整。"""
        if self._cmd_service is not None:
            self._cmd_service.submit(Command(CommandType.SYS_EMERGENCY_STOP, source="gui"))
        else:
            self._ctrl.emergency_stop()
        self._stop_sweep()
        self._rf_toggle.setChecked(False)
        self._lf_toggle.setChecked(False)
        self._fm_toggle.setChecked(False)
        self._on_log("[E-Stop] Executed", "smb")
        QTimer.singleShot(500, self._verify_estop)

    def _verify_estop(self):
        result = self._ctrl.verify_emergency_stop()
        smb_ok = result.get("smb", {}).get("ok", False)
        if smb_ok:
            self._on_log("[E-Stop] Verification passed", "smb")
        else:
            self._on_log("[E-Stop] Verification failed: " + str(result), "smb")

    def _on_power_text_changed(self, text: str) -> None:
        """功率输入框颜色警告。"""
        try:
            p = float(text)
        except ValueError:
            self._power_edit.setStyleSheet("")
            return
        ratio = p / self._max_power_dbm if self._max_power_dbm != 0 else 0
        if p > self._max_power_dbm:
            self._power_edit.setStyleSheet("color: #e04040; font-weight: bold; border: 2px solid #e04040;")
        elif ratio > 0.95:
            self._power_edit.setStyleSheet("color: #e04040; font-weight: bold;")
        elif ratio > 0.80:
            self._power_edit.setStyleSheet("color: #ff8c00;")
        else:
            self._power_edit.setStyleSheet("")

    def _set_power(self, val):
        try:
            p = float(val)
            if p > self._max_power_dbm:
                QMessageBox.warning(
                    self, "功率限制 / Power Limit",
                    f"功率 {p} dBm 超过安全限制 {self._max_power_dbm} dBm\n"
                    f"Power {p} dBm exceeds safety limit {self._max_power_dbm} dBm"
                )
                return
            if self._cmd_service is not None:
                cmd = Command(CommandType.SMB_SET_POWER, {"power_dbm": p}, source="gui")
                self._cmd_service.submit(cmd)
            else:
                self._ctrl.smb.set_power(p)
            self._on_log("Power set to " + val + " dBm", "smb")
        except Exception as exc:
            self._on_error("Set power failed: " + str(exc))

    def _set_cw_freq(self, val):
        try:
            if self._cmd_service is not None:
                self._cmd_service.submit(Command(CommandType.SMB_SET_FREQUENCY, {"freq_hz": float(val)}, source="gui"))
            else:
                self._ctrl.smb.set_freq_cw(float(val))
            self._on_log("CW freq set to " + val + " Hz", "smb")
        except Exception as exc:
            self._on_error("Set CW freq failed: " + str(exc))

    def _set_lf_freq(self, val):
        try:
            if self._cmd_service is not None:
                self._cmd_service.submit(Command(CommandType.SMB_SET_LF_FREQ, {"freq_hz": float(val)}, source="gui"))
            else:
                self._ctrl.smb.set_lf_freq(float(val))
            self._on_log("LF freq set to " + val + " Hz", "smb")
        except Exception as exc:
            self._on_error("Set LF freq failed: " + str(exc))

    def _set_lf_amp(self, val):
        try:
            if self._cmd_service is not None:
                self._cmd_service.submit(Command(CommandType.SMB_SET_LF_VOLTAGE, {"mv": float(val)}, source="gui"))
            else:
                self._ctrl.smb.set_lf_voltage(float(val))
            self._on_log("LF amp set to " + val + " mV", "smb")
        except Exception as exc:
            self._on_error("Set LF amp failed: " + str(exc))

    def _set_lf_shape(self):
        try:
            shape = self._lf_shape_combo.currentText()
            if self._cmd_service is not None:
                self._cmd_service.submit(Command(CommandType.SMB_SET_LF_SHAPE, {"shape": shape}, source="gui"))
            else:
                self._ctrl.smb.set_lf_shape(shape)
            self._on_log("LF shape set to " + shape, "smb")
        except Exception as exc:
            self._on_error("Set LF shape failed: " + str(exc))

    def _set_fm_dev(self, val):
        try:
            if self._cmd_service is not None:
                self._cmd_service.submit(Command(CommandType.SMB_SET_FM_DEVIATION, {"hz": float(val)}, source="gui"))
            else:
                self._ctrl.smb.set_fm_deviation(float(val))
            self._on_log("FM deviation set to " + val + " Hz", "smb")
        except Exception as exc:
            self._on_error("Set FM dev failed: " + str(exc))

    def _apply_all_params(self):
        try:
            p = float(self._power_edit.text())
            if p > self._max_power_dbm:
                QMessageBox.warning(
                    self, "功率限制 / Power Limit",
                    f"功率 {p} dBm 超过安全限制 {self._max_power_dbm} dBm"
                )
                return
            params = [
                (CommandType.SMB_SET_POWER, {"power_dbm": p}),
                (CommandType.SMB_SET_LF_VOLTAGE, {"mv": float(self._lf_amp_edit.text())}),
                (CommandType.SMB_SET_LF_FREQ, {"freq_hz": float(self._lf_freq_edit.text())}),
                (CommandType.SMB_SET_LF_SHAPE, {"shape": self._lf_shape_combo.currentText()}),
                (CommandType.SMB_SET_FM_DEVIATION, {"hz": float(self._fm_dev_edit.text())}),
            ]
            if self._cmd_service is not None:
                for ct, pr in params:
                    self._cmd_service.submit(Command(ct, pr, source="gui"))
            else:
                self._ctrl.smb.set_power(p)
                self._ctrl.smb.set_lf_voltage(float(self._lf_amp_edit.text()))
                self._ctrl.smb.set_lf_freq(float(self._lf_freq_edit.text()))
                self._ctrl.smb.set_lf_shape(self._lf_shape_combo.currentText())
                self._ctrl.smb.set_fm_deviation(float(self._fm_dev_edit.text()))
            self._on_log("All parameters applied", "smb")
        except Exception as exc:
            self._on_error("Apply all failed: " + str(exc))

    def _toggle_rf_output(self, state):
        if not self._ctrl.is_smb_connected:
            return
        try:
            on = state == Qt.Checked
            if self._cmd_service is not None:
                self._cmd_service.submit(Command(CommandType.SMB_SET_OUTPUT, {"enabled": on}, source="gui"))
            else:
                self._ctrl.smb.set_output(on)
            self._on_log("RF output " + ("ON" if on else "OFF"), "smb")
        except Exception as exc:
            self._on_error("RF toggle failed: " + str(exc))

    def _toggle_lf_output(self, state):
        if not self._ctrl.is_smb_connected:
            return
        try:
            on = state == Qt.Checked
            if self._cmd_service is not None:
                self._cmd_service.submit(Command(CommandType.SMB_SET_LF_OUTPUT, {"enabled": on}, source="gui"))
            else:
                self._ctrl.smb.set_lf_output(on)
            self._on_log("LF output " + ("ON" if on else "OFF"), "smb")
        except Exception as exc:
            self._on_error("LF toggle failed: " + str(exc))

    def _toggle_fm_mod(self, state):
        if not self._ctrl.is_smb_connected:
            return
        try:
            on = state == Qt.Checked
            if self._cmd_service is not None:
                self._cmd_service.submit(Command(CommandType.SMB_SET_MODULATION, {"enabled": on}, source="gui"))
            else:
                self._ctrl.smb.set_fm_state(on)
            self._on_log("FM modulation " + ("ON" if on else "OFF"), "smb")
        except Exception as exc:
            self._on_error("FM toggle failed: " + str(exc))

    def _set_laser_power(self):
        if not self._ctrl.is_laser_connected:
            QMessageBox.warning(self, "警告", "请先连接激光器")
            return
        if self._cmd_service is None:
            QMessageBox.warning(self, "警告", "命令总线未就绪")
            return
        try:
            power = int(self._laser_power_edit.text())
            max_mw = self._cfg["laser"].get("max_power_mw", 150)
            if power < 0 or power > max_mw:
                QMessageBox.warning(self, "警告", f"功率超出范围 [0, {max_mw}] mW")
                return
            self._cmd_service.submit(Command(CommandType.LASER_SET_POWER, {"power_mw": power}, source="gui"))
            self._on_log(f"激光功率设为 {power} mW", "laser")
        except ValueError:
            QMessageBox.warning(self, "警告", "请输入有效的功率值")
        except Exception as exc:
            self._on_error("激光功率设置失败: " + str(exc))

    def _toggle_laser_output(self, state):
        if not self._ctrl.is_laser_connected:
            return
        if self._cmd_service is None:
            return
        try:
            on = state == Qt.Checked
            self._cmd_service.submit(Command(CommandType.LASER_SET_OUTPUT, {"enabled": on}, source="gui"))
            self._on_log("激光输出 " + ("ON" if on else "OFF"), "laser")
        except Exception as exc:
            self._on_error("激光输出切换失败: " + str(exc))

    def _start_sweep(self):
        if not self._ctrl.is_smb_connected:
            QMessageBox.warning(self, "Warning", "Please connect SMB100A first")
            return
        try:
            cycles = int(self._cycle_count_input.text())
            interval_ms = int(self._cycle_interval_input.text())
        except ValueError:
            cycles = 1
            interval_ms = 0
        step = SweepStep(
            name="sweep",
            start_freq_hz=float(self._start_freq_edit.text()),
            stop_freq_hz=float(self._stop_freq_edit.text()),
            step_hz=float(self._step_edit.text()),
            dwell_ms=float(self._dwell_edit.text()),
            power_dbm=float(self._sweep_power_edit.text()),
            lf_freq_hz=float(self._lf_freq_edit.text()),
            lf_amp_mv=float(self._lf_amp_edit.text()),
            lf_shape=self._lf_shape_combo.currentText(),
            fm_dev_hz=float(self._fm_dev_edit.text()),
            cycles=cycles,
            cycle_interval_ms=interval_ms,
        )
        seq = SweepSequence(name="manual", steps=[step])
        self._sweep_engine.set_sequence(seq)
        if self._auto_save_check.isChecked():
            out_dir = self._save_dir_input.text()
            self._recorder = ODMRRecorder(out_dir)
            self._recorder.start_recording()
            self._sweep_engine.set_recorder(self._recorder)
            self._rec_file_label.setText(str(self._recorder.output_dir))
            self._rec_status_label.setText("Recording...")
            self._rec_status_label.setStyleSheet(
                "padding: 10px; border-radius: 3px; font-weight: 700; background-color: #cce4f7;"
            )
            self._status_rec.setText("Record: ON")
        else:
            self._recorder = None
            self._sweep_engine.set_recorder(None)
        self._sweep_engine.start()
        self._start_sweep_btn.setEnabled(False)
        self._stop_sweep_btn.setEnabled(True)
        self._status_sweep.setText("Sweep: Running")

    def _stop_sweep(self):
        self._sweep_engine.stop()
        self._start_sweep_btn.setEnabled(False)
        self._stop_sweep_btn.setEnabled(False)
        self._status_sweep.setText("Sweep: Stopping...")

    def _on_sweep_step_started(self, name, idx):
        self._on_log("Sweep step started: " + name + " (" + str(idx) + ")", "smb")

    def _on_sweep_finished(self, ok):
        self._start_sweep_btn.setEnabled(True)
        self._stop_sweep_btn.setEnabled(False)
        self._status_sweep.setText("Sweep: Idle")
        self._rec_status_label.setText("Not recording")
        self._rec_status_label.setStyleSheet(
            "padding: 10px; border-radius: 3px; font-weight: 700; background-color: #e8e8e8;"
        )
        self._status_rec.setText("Record: OFF")
        self._on_log("Sweep finished: " + ("OK" if ok else "Cancelled"), "smb")

    def _on_smb_state_changed(self, *args):
        """兼容两种信号格式：
        - InstrumentController.smb_state_changed: (freq_hz, output_on, mode)
        - CommandService.smb_state_broadcast: dict
        """
        if len(args) == 1 and isinstance(args[0], dict):
            state = args[0]
            freq_hz = state.get("freq_hz", 0.0)
            output_on = state.get("output_on", False)
            mode = state.get("mode", "CW")
            power_dbm = state.get("power_dbm", 0.0)
            lf_on = state.get("lf_on", False)
            lf_freq_hz = state.get("lf_freq_hz", 0.0)
            mod_on = state.get("mod_on", False)
            lf_sweep = state.get("lf_sweep", False)
        elif len(args) >= 3:
            freq_hz, output_on, mode = args[0], args[1], args[2]
            power_dbm = 0.0
            lf_on = False
            lf_freq_hz = 0.0
            mod_on = False
            lf_sweep = False
        else:
            return

        # 频率大字体显示（兼容未创建的情况）
        if hasattr(self, '_freq_display'):
            self._freq_display.setText("%.6f GHz" % (freq_hz / 1e9))

        # 更新 SMB 参数面板
        params = {
            "freq": (freq_hz / 1e9, "GHz"),
            "power": (power_dbm, "dBm"),
            "rf_out": ("ON" if output_on else "OFF", ""),
            "mode": (mode, ""),
            "lf_out": ("ON" if lf_on else "OFF", ""),
            "lf_freq": (lf_freq_hz, "Hz"),
            "mod": ("ON" if mod_on else "OFF", ""),
        }
        for key, (value, unit) in params.items():
            if key in self._smb_param_displays:
                disp, _u, fmt = self._smb_param_displays[key]
                try:
                    text = fmt % value
                except Exception:
                    text = str(value)
                if unit:
                    text += f" {unit}"
                disp.setText(text)

        # 更新 SMB 指示灯
        indicators = {
            "rf": output_on,
            "lf_on": lf_on,
            "lf_sweep": lf_sweep,
            "mod_on": mod_on,
        }
        # 更新 RF toggle（微波源控制页面）
        if output_on != self._rf_toggle.isChecked():
            self._rf_toggle.blockSignals(True)
            self._rf_toggle.setChecked(output_on)
            self._rf_toggle.blockSignals(False)

        # 追加到所有通道的 buffer
        for buf in self._lockin_ch_buffers.values():
            buf.append({"smb_freq_hz": freq_hz}, datetime.datetime.now().timestamp())
        # 兼容旧 buffer
        self._buffer.append({"smb_freq_hz": freq_hz}, datetime.datetime.now().timestamp())

        # 全局状态栏
        if hasattr(self, '_gs_smb_rf'):
            for led, on in [(self._gs_smb_rf, output_on), (self._gs_smb_lf, lf_on), (self._gs_smb_mod, mod_on)]:
                led.setProperty("on", "true" if on else "false")
                led.style().unpolish(led)
                led.style().polish(led)

    def _on_laser_state_changed(self, state: dict):
        """处理激光器状态广播。"""
        power_mw = state.get("power_mw", 0.0)
        output_on = state.get("output_on", False)
        wavelength_nm = state.get("wavelength_nm", 0.0)

        # 更新参数面板
        params = {
            "power": (power_mw, "mW"),
            "output": ("ON" if output_on else "OFF", ""),
            "wavelength": (wavelength_nm, "nm"),
        }
        for key, (value, unit) in params.items():
            if key in self._laser_param_displays:
                disp, _u, fmt = self._laser_param_displays[key]
                try:
                    text = fmt % value
                except Exception:
                    text = str(value)
                if unit:
                    text += f" {unit}"
                disp.setText(text)

        # 同步输出开关状态
        if hasattr(self, "_laser_output_toggle"):
            if output_on != self._laser_output_toggle.isChecked():
                self._laser_output_toggle.blockSignals(True)
                self._laser_output_toggle.setChecked(output_on)
                self._laser_output_toggle.blockSignals(False)

        # 全局状态栏
        if hasattr(self, '_gs_laser_out'):
            self._gs_laser_out.setProperty("on", "true" if output_on else "false")
            self._gs_laser_out.style().unpolish(self._gs_laser_out)
            self._gs_laser_out.style().polish(self._gs_laser_out)

    def _on_lockin_data_ready(self, data):
        """处理 Lockin 数据，按 channel 路由到对应显示和 buffer。"""
        ch = data.get("channel", 1)
        # 更新对应通道的显示
        if ch in self._lockin_ch_displays:
            for key, disp in self._lockin_ch_displays[ch].items():
                val = data.get(key, 0.0)
                disp.setText(f"{val:.4f}")
        # 更新对应通道的 buffer
        if ch in self._lockin_ch_buffers:
            point = {k: data.get(k, 0.0) for k in ["X", "Y", "R", "theta"]}
            self._lockin_ch_buffers[ch].append(point, datetime.datetime.now().timestamp())
        # 兼容旧 buffer（CH-A 数据也写入主 buffer）
        if ch == 1:
            self._buffer.append(data, datetime.datetime.now().timestamp())

    def _on_lockin_status_changed(self, status):
        """更新全局状态栏 Lockin 指示灯（过载、PLL）。"""
        ch = status.get("channel", 1)
        # 全局状态栏
        if hasattr(self, '_gs_lockin_a_ov'):
            any_ov = status.get("input_overload", False) or status.get("gain_overload", False)
            locked = status.get("pll_locked", False)
            if ch == 1:
                self._gs_lockin_a_ov.setProperty("on", "warn" if any_ov else "false")
                self._gs_lockin_a_pll.setProperty("on", "true" if locked else "false")
            elif ch == 2:
                self._gs_lockin_b_ov.setProperty("on", "warn" if any_ov else "false")
                self._gs_lockin_b_pll.setProperty("on", "true" if locked else "false")
            for led in ([self._gs_lockin_a_ov, self._gs_lockin_a_pll] if ch == 1
                        else [self._gs_lockin_b_ov, self._gs_lockin_b_pll]):
                led.style().unpolish(led)
                led.style().polish(led)

    def _on_lockin_batch_ready(self, batch):
        """处理 RALL? 批次数据：喂给波形缓冲区和兼容旧缓冲区。"""
        n = len(batch.get("lockin_A_X_mv", []))
        if n == 0:
            return
        ts = datetime.datetime.now().timestamp()
        dt = 0.05 / n  # 50ms / n points

        for ch_label, ch_num in [("A", 1), ("B", 2)]:
            prefix = f"lockin_{ch_label}_"
            x = batch.get(f"{prefix}X_mv")
            y = batch.get(f"{prefix}Y_mv")
            if x is None or y is None:
                continue
            r = np.sqrt(x**2 + y**2)
            theta = np.degrees(np.arctan2(y, x))
            freq = batch.get(f"{prefix}freq_hz", np.zeros(n))
            noise = batch.get(f"{prefix}noise_mv", np.zeros(n))
            xh1 = batch.get(f"{prefix}Xh1_mv", np.zeros(n))
            yh1 = batch.get(f"{prefix}Yh1_mv", np.zeros(n))
            rh1 = np.sqrt(xh1**2 + yh1**2)
            theta_h1 = np.degrees(np.arctan2(yh1, xh1))
            xh2 = batch.get(f"{prefix}Xh2_mv", np.zeros(n))
            yh2 = batch.get(f"{prefix}Yh2_mv", np.zeros(n))
            rh2 = np.sqrt(xh2**2 + yh2**2)
            theta_h2 = np.degrees(np.arctan2(yh2, xh2))

            timestamps = [ts + i * dt for i in range(n)]

            # 写入波形缓冲区
            if ch_num in self._waveform_buffers:
                self._waveform_buffers[ch_num].extend({
                    "X": x.tolist(), "Y": y.tolist(), "R": r.tolist(), "theta": theta.tolist(),
                    "freq": freq.tolist(), "noise": noise.tolist(),
                    "Xh1": xh1.tolist(), "Yh1": yh1.tolist(), "Rh1": rh1.tolist(), "theta_h1": theta_h1.tolist(),
                    "Xh2": xh2.tolist(), "Yh2": yh2.tolist(), "Rh2": rh2.tolist(), "theta_h2": theta_h2.tolist(),
                }, timestamps)

        # 兼容旧缓冲区（CH-A 基波）
        if 1 in self._lockin_ch_buffers:
            x_a = batch.get("lockin_A_X_mv", np.zeros(n))
            y_a = batch.get("lockin_A_Y_mv", np.zeros(n))
            r_a = np.sqrt(x_a**2 + y_a**2)
            for i in range(n):
                point = {"X": float(x_a[i]), "Y": float(y_a[i]), "R": float(r_a[i])}
                self._lockin_ch_buffers[1].append(point, ts + i * dt)
                self._buffer.append(point, ts + i * dt)

        self._waveform_data_count += n

    def _on_display_tick(self):
        """定时刷新波形图。"""
        if not _HAS_PYG or not hasattr(self, '_wave_plot') or self._wave_plot is None:
            return
        if self._waveform_paused:
            return
        if (
            getattr(self, "_waveform_page_active", False)
            and self._ctrl.is_lockin_connected
            and not self._ctrl.is_acquiring
        ):
            self._waveform_rall_running = False
            self._start_waveform_acquire()

        # 选择当前通道 buffer
        ch_idx = self._wave_ch_select.currentIndex() if hasattr(self, '_wave_ch_select') else 0
        ch_num = ch_idx + 1
        buf = self._waveform_buffers.get(ch_num)
        if buf is None:
            return

        # 更新状态标签
        if hasattr(self, '_wave_status_label'):
            self._wave_status_label.setText(f"数据点: {self._waveform_data_count}")

        # 更新所有可见曲线
        for _label, key, _color, _default, axis_group in self._WAVE_PARAMS:
            cb = self._wave_checkboxes.get(key)
            curve = self._wave_curves.get(key)
            if cb is None or curve is None:
                continue
            if not cb.isChecked():
                continue
            ts_arr, vals = buf.get(key, max_points=2000, downsample=2)
            if len(ts_arr) > 0:
                ts_arr = ts_arr - ts_arr[-1]  # 相对时间，最新=0
                curve.setData(ts_arr, vals)

        # Auto Y 轴缩放
        if hasattr(self, '_wave_auto_y') and self._wave_auto_y.isChecked():
            amp_vals = []
            for _label, key, _color, _default, axis_group in self._WAVE_PARAMS:
                if axis_group != "amp":
                    continue
                cb = self._wave_checkboxes.get(key)
                if cb is None or not cb.isChecked():
                    continue
                _ts, vals = buf.get(key, max_points=500)
                if len(vals) > 0:
                    amp_vals.extend(vals.tolist())
            if amp_vals:
                y_max_abs = max(abs(min(amp_vals)), abs(max(amp_vals)))
                if y_max_abs > 0:
                    margin = y_max_abs * 0.1
                    self._wave_plot.setYRange(-y_max_abs - margin, y_max_abs + margin, padding=0)
                    # Y 轴标签：数据已由 RALL? 驱动转换为 mV

    def _on_command_completed(self, request_id: str, success: bool, message: str, result: dict) -> None:
        """处理 CommandService 异步命令结果。"""
        if request_id not in self._pending_cmds:
            return
        op, ctx = self._pending_cmds.pop(request_id)

        if op == "smb_connect":
            if success:
                self._smb_conn_btn.setText("断开 / Disconnect")
                self._smb_led.setProperty("on", "true")
                self._smb_led.style().unpolish(self._smb_led)
                self._smb_led.style().polish(self._smb_led)
                idn = result.get("idn", "Unknown")
                self._smb_status_label.setText(f"已连接 / Connected: {idn[:30]}")
                self._smb_status_label.setStyleSheet("color: #00a651; font-weight: 600;")
                self._on_log("SMB100A connected: " + idn, "smb")
                # 保存序列号→地址绑定
                serial = self._extract_smb_serial(idn)
                if serial:
                    self._cfg.setdefault("smb_bindings", {})[serial] = ctx
                    self._on_log(f"SMB binding saved: {serial} -> {ctx}", "smb")
            else:
                QMessageBox.critical(self, "Connection Error", message)
                self._on_log("SMB100A connect failed: " + message, "smb")

        elif op == "smb_disconnect":
            self._smb_conn_btn.setText("连接 / Connect")
            self._smb_led.setProperty("on", "false")
            self._smb_led.style().unpolish(self._smb_led)
            self._smb_led.style().polish(self._smb_led)
            self._smb_status_label.setText("未连接 / Disconnected")
            self._smb_status_label.setStyleSheet("color: #999;")
            self._on_log("SMB100A disconnected", "smb")

        elif op == "lockin_connect":
            if success:
                self._lockin_conn_btn.setText("断开 / Disconnect")
                self._lockin_led.setProperty("on", "true")
                self._lockin_led.style().unpolish(self._lockin_led)
                self._lockin_led.style().polish(self._lockin_led)
                idn = result.get("idn", "Unknown")
                self._lockin_status_label.setText(f"已连接 / Connected: {idn[:30]}")
                self._lockin_status_label.setStyleSheet("color: #00a651; font-weight: 600;")
                self._status_lockin.setText("Lockin: " + idn[:30])
                self._on_log("OE1022D connected: " + idn, "lockin")
                # 保存 IDN→端口绑定
                if idn and idn != "Unknown":
                    self._cfg.setdefault("lockin_bindings", {})[idn] = ctx
                    self._on_log(f"Lockin binding saved: {idn[:30]} -> {ctx}", "lockin")
                if getattr(self, "_waveform_page_active", False):
                    self._start_waveform_acquire()
            else:
                QMessageBox.critical(self, "Connection Error", message)
                self._on_log("OE1022D connect failed: " + message, "lockin")

        elif op == "lockin_disconnect":
            self._lockin_conn_btn.setText("连接 / Connect")
            self._lockin_led.setProperty("on", "false")
            self._lockin_led.style().unpolish(self._lockin_led)
            self._lockin_led.style().polish(self._lockin_led)
            self._lockin_status_label.setText("未连接 / Disconnected")
            self._lockin_status_label.setStyleSheet("color: #999;")
            self._status_lockin.setText("Lockin: 未连接")
            self._waveform_rall_running = False
            self._on_log("OE1022D disconnected", "lockin")

        elif op == "laser_connect":
            if success:
                self._laser_conn_btn.setText("断开 / Disconnect")
                self._laser_led.setProperty("on", "true")
                self._laser_led.style().unpolish(self._laser_led)
                self._laser_led.style().polish(self._laser_led)
                idn = result.get("idn", "Unknown")
                self._laser_status_label.setText(f"已连接 / Connected: {idn[:30]}")
                self._laser_status_label.setStyleSheet("color: #00a651; font-weight: 600;")
                self._status_laser.setText("Laser: " + idn[:30])
                self._on_log("Laser connected: " + idn, "laser")
            else:
                QMessageBox.critical(self, "Connection Error", message)
                self._on_log("Laser connect failed: " + message, "laser")

        elif op == "laser_disconnect":
            self._laser_conn_btn.setText("连接 / Connect")
            self._laser_led.setProperty("on", "false")
            self._laser_led.style().unpolish(self._laser_led)
            self._laser_led.style().polish(self._laser_led)
            self._laser_status_label.setText("未连接 / Disconnected")
            self._laser_status_label.setStyleSheet("color: #999;")
            self._status_laser.setText("Laser: 未连接")
            self._on_log("Laser disconnected", "laser")

        elif op == "record_start":
            if success:
                output_dir = result.get("output_dir", str(ctx))
                self._apply_recording_started(output_dir)
                self._waveform_rall_running = True
                self._on_log(f"Recording started: {output_dir}", "lockin")
            else:
                self._apply_recording_stopped()
                QMessageBox.critical(self, "Recording Error", message)
                self._on_log("Recording start failed: " + message, "lockin")

        elif op == "record_stop":
            self._apply_recording_stopped()
            if success:
                self._on_log("Recording stopped", "lockin")
            else:
                QMessageBox.warning(self, "Recording Error", message)
                self._on_log("Recording stop failed: " + message, "lockin")

    def _on_command_error(self, request_id: str, error_message: str) -> None:
        """处理 CommandService 命令错误。"""
        if request_id in self._pending_cmds:
            op, ctx = self._pending_cmds.pop(request_id)
            self._on_log(f"[Command Error] {op}: {error_message}", "smb")
            if op in ("record_start", "record_stop"):
                self._apply_recording_stopped()

    def _on_error(self, msg):
        self._on_log("[ERROR] " + msg, "smb")
        logging.getLogger("odmr").error(msg)

    def _on_log(self, msg, device="smb"):
        ts = datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]
        line = "[%s] %s" % (ts, msg)
        if device == "smb":
            self._smb_log.append(line)
            self._trim_log(self._smb_log)
        elif device == "lockin":
            self._lockin_log.append(line)
            self._trim_log(self._lockin_log)
        else:
            self._serial_log.append(line)
            self._trim_log(self._serial_log)

    def _trim_log(self, widget):
        text = widget.toPlainText()
        lines = text.split("\n")
        if len(lines) > 500:
            widget.setPlainText("\n".join(lines[-500:]))

    def _save_config(self):
        cfg = self._cfg.copy()
        cfg["smb"]["visa_address"] = self._smb_addr_input.text()
        cfg["lockin"]["port"] = self._lockin_port_combo.currentText()
        cfg["lockin"]["baudrate"] = int(self._lockin_baud_combo.currentText())
        cfg["acquisition"]["save_dir"] = self._save_dir_input.text()
        cfg["acquisition"]["auto_save"] = self._auto_save_check.isChecked()
        save_config(cfg)
        self._on_log("Config saved", "smb")

    def closeEvent(self, event):
        reply = QMessageBox.question(self, "Exit", "Confirm exit?", QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if reply == QMessageBox.Yes:
            self._sweep_engine.stop_and_wait()
            self._ctrl.emergency_stop()
            self._ctrl.disconnect_smb()
            self._ctrl.disconnect_lockin()
            self._ctrl.disconnect_laser()
            event.accept()
        else:
            event.ignore()


def main():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    window = ODMRControlGUI()
    window.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
