from __future__ import annotations

import datetime
import logging
import math
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Dict, Optional

from PyQt5.QtCore import Qt, QTimer, QPointF
from PyQt5.QtGui import QDoubleValidator, QFont, QPainter, QPen, QColor, QPolygonF
from PyQt5.QtWidgets import (
    QAction,
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMenuBar,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QSlider,
    QSpinBox,
    QSplitter,
    QStackedWidget,
    QStatusBar,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QToolBar,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

try:
    import serial
    import serial.tools.list_ports
except ImportError:
    serial = None  # type: ignore[assignment]

from core.field_controller import AXES, FieldController
from core.presets import FieldPreset, PresetManager
from core.sequence_engine import FieldSequence, FieldStep, SequenceEngine
from core.vector_field import CartesianField, SphericalField, cartesian_to_spherical, spherical_to_cartesian
from data.recorder import MagnetFieldRecorder

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.xml"


# ---------------------------------------------------------------------------
# Config I/O (para.xml compatible)
# ---------------------------------------------------------------------------

def load_config(path: Path = DEFAULT_CONFIG_PATH) -> dict:
    cfg: Dict[str, object] = {
        "ports": {"X": "", "Y": "", "Z": ""},
        "baudrate": 9600,
        "coil_constant": {"X": 143.26, "Y": 141.77, "Z": 156.15},
        "zero_offset": {"X": 0.0, "Y": 0.0, "Z": 0.0},
        "data_save_dir": "./data",
        "poll_interval_ms": 500,
    }
    try:
        tree = ET.parse(path)
        root = tree.getroot()
        ports = root.find("Commports")
        if ports is not None:
            for axis in AXES:
                node = ports.find(f"Port{axis}")
                if node is not None:
                    cfg["ports"][axis] = node.get("Port", cfg["ports"][axis])  # type: ignore[index]
                    cfg["baudrate"] = int(node.get("BaudRate", cfg["baudrate"]))  # type: ignore[arg-type]
        cc = root.find("CoilConstant")
        if cc is not None:
            for axis in AXES:
                val = cc.get(axis)
                if val is not None:
                    cfg["coil_constant"][axis] = float(val)  # type: ignore[index]
        zo = root.find("ZeroOffset")
        if zo is not None:
            for axis in AXES:
                val = zo.get(axis)
                if val is not None:
                    cfg["zero_offset"][axis] = float(val)  # type: ignore[index]
        dsd = root.find("DataSaveDir")
        if dsd is not None and dsd.text:
            cfg["data_save_dir"] = dsd.text
        poll = root.find("PollIntervalMs")
        if poll is not None and poll.text:
            cfg["poll_interval_ms"] = max(100, min(5000, int(float(poll.text))))
    except Exception:
        pass
    return cfg


def save_config(cfg: dict, path: Path = DEFAULT_CONFIG_PATH) -> None:
    root = ET.Element("Root")
    ports_el = ET.SubElement(root, "Commports")
    for axis in AXES:
        pe = ET.SubElement(ports_el, f"Port{axis}")
        pe.set("Port", cfg["ports"][axis])  # type: ignore[index]
        pe.set("BaudRate", str(cfg["baudrate"]))
    dsd = ET.SubElement(root, "DataSaveDir")
    dsd.text = str(cfg["data_save_dir"])
    poll = ET.SubElement(root, "PollIntervalMs")
    poll.text = str(int(cfg.get("poll_interval_ms", 500)))
    cc = ET.SubElement(root, "CoilConstant")
    for axis in AXES:
        cc.set(axis, f"{cfg['coil_constant'][axis]:.2f}")  # type: ignore[index]
    zo = ET.SubElement(root, "ZeroOffset")
    for axis in AXES:
        zo.set(axis, f"{cfg['zero_offset'][axis]:.5f}")  # type: ignore[index]
    tree = ET.ElementTree(root)
    ET.indent(tree, space="  ")
    tree.write(path, encoding="utf-8", xml_declaration=True)


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
    QToolBar QToolButton {
        padding: 4px 10px; border: 1px solid transparent; border-radius: 3px;
        color: #1a1a1a;
    }
    QToolBar QToolButton:hover { background-color: #cce4f7; border-color: #99c9e8; }
    QToolBar QToolButton:pressed { background-color: #99c9e8; }
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
    QLabel#axisData {
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
    QTableWidget {
        background-color: #ffffff; alternate-background-color: #f5f5f5;
        gridline-color: #d0d0d0; border: 1px solid #c0c0c0;
    }
    QHeaderView::section {
        background-color: #e0e0e0; color: #1a1a1a; padding: 6px;
        border: 1px solid #c0c0c0; font-weight: 600;
    }
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
"""


# ---------------------------------------------------------------------------
# Vector field visualization widget
# ---------------------------------------------------------------------------

class VectorFieldVizWidget(QWidget):
    """3-axis field projection for target and current-estimated vectors."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._target = {"X": 0.0, "Y": 0.0, "Z": 0.0}
        self._estimated: Optional[Dict[str, float]] = None
        self.setMinimumSize(360, 360)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

    def set_field(self, bx: float, by: float, bz: float) -> None:
        self.set_fields({"X": bx, "Y": by, "Z": bz}, None)

    def set_fields(self, target: Dict[str, float], estimated: Optional[Dict[str, float]] = None) -> None:
        self._target = {axis: float(target.get(axis, 0.0)) for axis in AXES}
        if estimated is None:
            self._estimated = None
        else:
            self._estimated = {axis: float(estimated.get(axis, 0.0)) for axis in AXES}
        self.update()

    def paintEvent(self, _event) -> None:  # type: ignore[override]
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        w = self.width()
        h = self.height()

        # background
        painter.fillRect(self.rect(), QColor("#f8f8f8"))

        left_margin = 44
        right_margin = 16
        top_margin = 34
        row_h = max(92, (h - top_margin - 40) // 3)
        cx = (left_margin + w - right_margin) // 2
        scale = min((w - left_margin - right_margin) * 0.34, row_h * 0.34)
        views = [
            ("XY 平面", top_margin + row_h * 0 + row_h // 2, "X", "Y"),
            ("XZ 平面", top_margin + row_h * 1 + row_h // 2, "X", "Z"),
            ("YZ 平面", top_margin + row_h * 2 + row_h // 2, "Y", "Z"),
        ]

        axis_pen = QPen(QColor("#c0c0c0"), 1)
        target_pen = QPen(QColor("#0080c8"), 2.5)
        estimated_pen = QPen(QColor("#00a651"), 2.5)
        components = [abs(v) for v in self._target.values()]
        if self._estimated is not None:
            components.extend(abs(v) for v in self._estimated.values())
        max_component = max(1.0, *components)

        painter.setFont(QFont("Segoe UI", 9))
        painter.setPen(QColor("#0080c8"))
        painter.drawText(12, 18, "目标场")
        painter.setPen(QColor("#00a651"))
        painter.drawText(76, 18, "回读估算")

        for label, vcy, ax_name, ay_name in views:
            # axes
            painter.setPen(axis_pen)
            painter.drawLine(cx - int(scale), vcy, cx + int(scale), vcy)
            painter.drawLine(cx, vcy - int(scale), cx, vcy + int(scale))

            # axis labels
            painter.setFont(QFont("Segoe UI", 9))
            painter.setPen(QColor("#555"))
            painter.drawText(cx + int(scale) + 4, vcy + 4, ax_name)
            painter.drawText(cx + 4, vcy - int(scale) - 4, ay_name)
            painter.drawText(8, vcy - int(scale) + 14, label)

            self._draw_projection_arrow(
                painter, cx, vcy, scale, max_component,
                self._target[ax_name], self._target[ay_name], target_pen, QColor("#0080c8")
            )
            if self._estimated is not None:
                self._draw_projection_arrow(
                    painter, cx, vcy, scale, max_component,
                    self._estimated[ax_name], self._estimated[ay_name], estimated_pen, QColor("#00a651")
                )

        painter.end()

    def _draw_projection_arrow(
        self,
        painter: QPainter,
        cx: int,
        cy: int,
        scale: float,
        max_component: float,
        ax_val: float,
        ay_val: float,
        pen: QPen,
        color: QColor,
    ) -> None:
        B = math.sqrt(ax_val ** 2 + ay_val ** 2)
        if B <= 0:
            return
        ex = cx + int((ax_val / max_component) * scale)
        ey = cy - int((ay_val / max_component) * scale)
        painter.setPen(pen)
        painter.drawLine(cx, cy, ex, ey)

        angle = math.atan2(-(ey - cy), ex - cx)
        hl = 9
        ha = 0.42
        x1 = ex - hl * math.cos(angle - ha)
        y1 = ey + hl * math.sin(angle - ha)
        x2 = ex - hl * math.cos(angle + ha)
        y2 = ey + hl * math.sin(angle + ha)
        painter.setBrush(color)
        painter.drawPolygon(QPolygonF([QPointF(ex, ey), QPointF(x1, y1), QPointF(x2, y2)]))


# ---------------------------------------------------------------------------
# Main Window
# ---------------------------------------------------------------------------

class MagnetFieldControlGUI(QMainWindow):

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("磁场控制系统 v2")
        self.setMinimumSize(1100, 720)

        # config
        self._cfg = load_config()

        # core
        self._fc = FieldController(self)
        self._fc.set_poll_interval_ms(int(self._cfg.get("poll_interval_ms", 500)))
        self._seq_engine = SequenceEngine(self._fc, self)
        self._recorder = MagnetFieldRecorder()
        self._preset_mgr = PresetManager()
        self._recording = False
        self._record_count = 0

        # error logger
        self._setup_error_logger()

        # apply config to controllers
        for a in AXES:
            self._fc.set_coil_constant(a, self._cfg["coil_constant"][a])  # type: ignore[index]
            self._fc.set_zero_offset(a, self._cfg["zero_offset"][a])  # type: ignore[index]

        # connect signals
        self._fc.current_changed.connect(self._on_current_changed)
        self._fc.connection_changed.connect(self._on_connection_changed)
        self._fc.error_occurred.connect(self._on_error)
        self._fc.log_requested.connect(self._log)
        self._seq_engine.step_started.connect(self._on_seq_step_started)
        self._seq_engine.sequence_finished.connect(self._on_seq_finished)
        self._seq_engine.progress.connect(self._on_seq_progress)
        self._seq_engine.error_occurred.connect(lambda m: self._log(f"[序列] 错误: {m}"))

        # build UI
        self.setStyleSheet(SIEMENS_STYLE)
        self._build_menu()
        self._build_toolbar()
        self._build_central()
        self._build_statusbar()

        # recording timer
        self._record_timer = QTimer(self)
        self._record_timer.timeout.connect(self._on_record_tick)

    # -----------------------------------------------------------------------
    # Menu bar
    # -----------------------------------------------------------------------

    def _build_menu(self) -> None:
        menubar = self.menuBar()

        file_menu = menubar.addMenu("文件(&F)")
        act_save_cfg = QAction("保存配置", self)
        act_save_cfg.triggered.connect(self._save_config)
        file_menu.addAction(act_save_cfg)
        file_menu.addSeparator()
        act_quit = QAction("退出(&Q)", self)
        act_quit.triggered.connect(self.close)
        file_menu.addAction(act_quit)

        conn_menu = menubar.addMenu("连接(&C)")
        act_scan = QAction("扫描串口", self)
        act_scan.triggered.connect(self._scan_ports)
        conn_menu.addAction(act_scan)
        conn_menu.addSeparator()
        act_conn_all = QAction("全部连接", self)
        act_conn_all.triggered.connect(self._connect_all)
        conn_menu.addAction(act_conn_all)
        act_disconn_all = QAction("全部断开", self)
        act_disconn_all.triggered.connect(self._disconnect_all)
        conn_menu.addAction(act_disconn_all)

        seq_menu = menubar.addMenu("序列(&S)")
        act_load_seq = QAction("加载序列...", self)
        act_load_seq.triggered.connect(self._load_sequence_file)
        seq_menu.addAction(act_load_seq)
        act_save_seq = QAction("保存序列...", self)
        act_save_seq.triggered.connect(self._save_sequence_file)
        seq_menu.addAction(act_save_seq)

    # -----------------------------------------------------------------------
    # Toolbar
    # -----------------------------------------------------------------------

    def _build_toolbar(self) -> None:
        tb = QToolBar("工具栏")
        tb.setMovable(False)
        self.addToolBar(tb)

        scan_btn = QPushButton("扫描串口")
        scan_btn.clicked.connect(self._scan_ports)
        tb.addWidget(scan_btn)

        conn_all_btn = QPushButton("全部连接")
        conn_all_btn.setObjectName("primaryBtn")
        conn_all_btn.clicked.connect(self._connect_all)
        tb.addWidget(conn_all_btn)

        disconn_all_btn = QPushButton("全部断开")
        disconn_all_btn.clicked.connect(self._disconnect_all)
        tb.addWidget(disconn_all_btn)

        tb.addSeparator()

        estop_btn = QPushButton("紧急停止")
        estop_btn.setObjectName("dangerBtn")
        estop_btn.clicked.connect(self._emergency_stop)
        tb.addWidget(estop_btn)

        tb.addSeparator()

        check_btn = QPushButton("硬件自检")
        check_btn.clicked.connect(self._check_hardware)
        tb.addWidget(check_btn)

    # -----------------------------------------------------------------------
    # Central: left nav tree + right stacked pages
    # -----------------------------------------------------------------------

    def _build_central(self) -> None:
        splitter = QSplitter(Qt.Horizontal)

        # left: navigation tree
        self._nav = QTreeWidget()
        self._nav.setHeaderHidden(True)
        self._nav.setFixedWidth(180)
        nav_items = [
            ("📡  连接配置", 0),
            ("🧲  手动控制", 1),
            ("🧭  向量场", 2),
            ("📋  序列自动化", 3),
            ("⚙  参数设置", 4),
            ("📊  数据记录", 5),
            ("📝  日志", 6),
        ]
        for label, idx in nav_items:
            item = QTreeWidgetItem([label])
            item.setData(0, Qt.UserRole, idx)
            self._nav.addTopLevelItem(item)
        self._nav.currentItemChanged.connect(self._on_nav_changed)
        splitter.addWidget(self._nav)

        # right: stacked pages
        self._stack = QStackedWidget()
        self._stack.addWidget(self._build_connection_page())
        self._stack.addWidget(self._build_manual_page())
        self._stack.addWidget(self._build_vector_field_page())
        self._stack.addWidget(self._build_sequence_page())
        self._stack.addWidget(self._build_settings_page())
        self._stack.addWidget(self._build_record_page())
        self._stack.addWidget(self._build_log_page())
        splitter.addWidget(self._stack)

        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        self.setCentralWidget(splitter)

        # select first item
        self._nav.setCurrentItem(self._nav.topLevelItem(0))

    def _on_nav_changed(self, current, _previous) -> None:
        if current is not None:
            idx = current.data(0, Qt.UserRole)
            self._stack.setCurrentIndex(idx)

    # -----------------------------------------------------------------------
    # Status bar
    # -----------------------------------------------------------------------

    def _build_statusbar(self) -> None:
        sb = QStatusBar()
        self.setStatusBar(sb)

        self._status_current: Dict[str, QLabel] = {}
        for axis in AXES:
            lbl = QLabel(f"{axis}: 总 --- mA / 复现 --- mA / B --- nT")
            lbl.setMinimumWidth(260)
            sb.addWidget(lbl)
            self._status_current[axis] = lbl

        self._status_conn = QLabel("连接: 0/3")
        sb.addPermanentWidget(self._status_conn)

        self._status_seq = QLabel("序列: 空闲")
        sb.addPermanentWidget(self._status_seq)

        self._status_rec = QLabel("记录: 关")
        sb.addPermanentWidget(self._status_rec)

    # -----------------------------------------------------------------------
    # Page 1: Connection
    # -----------------------------------------------------------------------

    def _build_connection_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)

        title = QLabel("连接配置")
        title.setObjectName("sectionTitle")
        layout.addWidget(title)

        group = QGroupBox("设备连接")
        grid = QGridLayout(group)

        headers = ["轴", "串口", "波特率", "状态", ""]
        for col, h in enumerate(headers):
            grid.addWidget(QLabel(h), 0, col)

        self._conn_widgets: Dict[str, dict] = {}
        for row, axis in enumerate(AXES, start=1):
            lbl = QLabel(axis)
            lbl.setAlignment(Qt.AlignCenter)
            lbl.setFont(QFont("Segoe UI", 14, QFont.Bold))
            grid.addWidget(lbl, row, 0)

            port_combo = QComboBox()
            port_combo.setEditable(True)
            port_combo.setMinimumWidth(160)
            grid.addWidget(port_combo, row, 1)

            baud_combo = QComboBox()
            baud_combo.addItems(["9600", "19200", "38400", "57600", "115200"])
            baud_combo.setCurrentText(str(self._cfg["baudrate"]))
            grid.addWidget(baud_combo, row, 2)

            led = QLabel()
            led.setObjectName("statusLed")
            led.setProperty("on", "false")
            grid.addWidget(led, row, 3, alignment=Qt.AlignCenter)

            btn = QPushButton("连接")
            btn.setObjectName("primaryBtn")
            btn.clicked.connect(lambda _, a=axis: self._toggle_connect(a))
            grid.addWidget(btn, row, 4)

            self._conn_widgets[axis] = {
                "port": port_combo, "baud": baud_combo, "led": led, "btn": btn,
            }

        layout.addWidget(group)

        scan_btn = QPushButton("扫描串口")
        scan_btn.clicked.connect(self._scan_ports)
        layout.addWidget(scan_btn)

        # status summary
        status_group = QGroupBox("状态总览")
        status_layout = QHBoxLayout(status_group)
        self._status_labels: Dict[str, QLabel] = {}
        for axis in AXES:
            pill = QLabel(f"{axis}: 未连接")
            pill.setAlignment(Qt.AlignCenter)
            pill.setStyleSheet(
                "padding: 10px; border-radius: 3px; font-weight: 700;"
                "background-color: #e8e8e8; border: 1px solid #c0c0c0;"
            )
            status_layout.addWidget(pill)
            self._status_labels[axis] = pill
        layout.addWidget(status_group)

        layout.addStretch()
        return page

    # -----------------------------------------------------------------------
    # Page 2: Manual control (workflow: zero → output → lock → set field)
    # -----------------------------------------------------------------------

    def _build_manual_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)

        title = QLabel("手动控制")
        title.setObjectName("sectionTitle")
        layout.addWidget(title)

        # -- Step 1: Zero calibration
        zero_group = QGroupBox("步骤1: 零场校准")
        zg = QGridLayout(zero_group)
        self._zero_widgets: Dict[str, dict] = {}
        for row, axis in enumerate(AXES):
            zg.addWidget(QLabel(f"{axis}轴  零偏电流:"), row, 0)
            zero_edit = QLineEdit(f"{self._cfg['zero_offset'][axis]:.5f}")  # type: ignore[index]
            zero_edit.setValidator(QDoubleValidator(-99999, 99999, 5))
            zero_edit.setMaximumWidth(140)
            zg.addWidget(zero_edit, row, 1)
            zg.addWidget(QLabel("mA"), row, 2)
            set_btn = QPushButton("设置")
            set_btn.clicked.connect(lambda _, a=axis: self._on_set_zero(a))
            zg.addWidget(set_btn, row, 3)
            self._zero_widgets[axis] = {"edit": zero_edit}

        ctrl_row = QHBoxLayout()
        out_all_btn = QPushButton("开启全部输出")
        out_all_btn.setObjectName("successBtn")
        out_all_btn.clicked.connect(self._all_output_on)
        ctrl_row.addWidget(out_all_btn)

        lock_all_btn = QPushButton("锁定全部零场")
        lock_all_btn.setObjectName("primaryBtn")
        lock_all_btn.setCheckable(True)
        lock_all_btn.toggled.connect(self._all_lock_zero)
        ctrl_row.addWidget(lock_all_btn)
        ctrl_row.addStretch()

        # per-axis LED
        for axis in AXES:
            ctrl_row.addWidget(QLabel(f"{axis}:"))
            led = QLabel()
            led.setObjectName("statusLed")
            led.setProperty("on", "false")
            ctrl_row.addWidget(led)
            if not hasattr(self, "_axis_leds"):
                self._axis_leds: Dict[str, QLabel] = {}
            self._axis_leds[axis] = led

        zg.addLayout(ctrl_row, len(AXES), 0, 1, 4)
        layout.addWidget(zero_group)

        # -- Step 2: Set field
        field_group = QGroupBox("步骤2: 复现磁场")
        fg = QGridLayout(field_group)
        fg.addWidget(QLabel(""), 0, 0)  # spacer
        fg.addWidget(QLabel("目标磁场 (nT)"), 0, 1)
        fg.addWidget(QLabel("目标复现电流 (mA)"), 0, 2)
        fg.addWidget(QLabel("回读总电流 (mA)"), 0, 3)
        fg.addWidget(QLabel("回读估算磁场 (nT)"), 0, 4)
        fg.addWidget(QLabel(""), 0, 5)

        self._field_widgets: Dict[str, dict] = {}
        for row, axis in enumerate(AXES, start=1):
            fg.addWidget(QLabel(f"{axis}轴:"), row, 0)

            mag_edit = QLineEdit("0.00")
            mag_edit.setValidator(QDoubleValidator(-9999999, 9999999, 2))
            mag_edit.setMaximumWidth(140)
            mag_edit.textChanged.connect(self._update_negative_field_warning)
            fg.addWidget(mag_edit, row, 1)

            curr_edit = QLineEdit("0.00000")
            curr_edit.setValidator(QDoubleValidator(-99999, 99999, 5))
            curr_edit.setMaximumWidth(140)
            fg.addWidget(curr_edit, row, 2)

            meas_lbl = QLabel("---")
            meas_lbl.setObjectName("axisData")
            meas_lbl.setMinimumWidth(140)
            fg.addWidget(meas_lbl, row, 3)

            est_lbl = QLabel("---")
            est_lbl.setObjectName("axisData")
            est_lbl.setMinimumWidth(140)
            fg.addWidget(est_lbl, row, 4)

            set_btn = QPushButton("设置")
            set_btn.setObjectName("primaryBtn")
            set_btn.clicked.connect(lambda _, a=axis: self._on_set_field(a))
            fg.addWidget(set_btn, row, 5)

            self._field_widgets[axis] = {
                "mag": mag_edit, "curr": curr_edit, "meas": meas_lbl, "est": est_lbl,
            }

        # coil constants display
        cc_row = QHBoxLayout()
        cc_row.addWidget(QLabel("线圈常数:  "))
        for axis in AXES:
            cc = self._cfg["coil_constant"][axis]  # type: ignore[index]
            cc_row.addWidget(QLabel(f"{axis}={cc:.2f} nT/mA"))
        cc_row.addStretch()
        fg.addLayout(cc_row, len(AXES) + 1, 0, 1, 6)

        self._negative_field_warning = QLabel("")
        self._negative_field_warning.setStyleSheet("color: #b00020; font-weight: 700; padding: 4px 0;")
        self._negative_field_warning.setWordWrap(True)
        fg.addWidget(self._negative_field_warning, len(AXES) + 2, 0, 1, 6)

        layout.addWidget(field_group)

        # -- Presets
        preset_group = QGroupBox("快捷操作 / 预设")
        pg = QHBoxLayout(preset_group)
        pg.addWidget(QLabel("预设:"))
        self._preset_combo = QComboBox()
        self._preset_combo.setMinimumWidth(180)
        self._refresh_presets()
        pg.addWidget(self._preset_combo)

        load_preset_btn = QPushButton("加载")
        load_preset_btn.setObjectName("primaryBtn")
        load_preset_btn.clicked.connect(self._load_preset)
        pg.addWidget(load_preset_btn)

        save_preset_btn = QPushButton("保存当前")
        save_preset_btn.clicked.connect(self._save_preset)
        pg.addWidget(save_preset_btn)

        del_preset_btn = QPushButton("删除")
        del_preset_btn.clicked.connect(self._delete_preset)
        pg.addWidget(del_preset_btn)

        pg.addStretch()

        zero_field_btn = QPushButton("设为零场")
        zero_field_btn.clicked.connect(self._set_all_zero_field)
        pg.addWidget(zero_field_btn)
        layout.addWidget(preset_group)

        layout.addStretch()
        return page

    # -----------------------------------------------------------------------
    # Page 3: Sequence automation
    # -----------------------------------------------------------------------

    def _build_sequence_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)

        title = QLabel("序列自动化")
        title.setObjectName("sectionTitle")
        layout.addWidget(title)

        # sequence table
        self._seq_table = QTableWidget(0, 8)
        self._seq_table.setHorizontalHeaderLabels([
            "步骤名", "X (nT)", "Y (nT)", "Z (nT)", "保持 (s)", "建立 (s)", "触发", "延时 (s)"
        ])
        self._seq_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        layout.addWidget(self._seq_table)

        # edit buttons
        edit_row = QHBoxLayout()
        add_btn = QPushButton("添加步骤")
        add_btn.clicked.connect(self._seq_add_step)
        edit_row.addWidget(add_btn)
        del_btn = QPushButton("删除步骤")
        del_btn.clicked.connect(self._seq_del_step)
        edit_row.addWidget(del_btn)
        up_btn = QPushButton("上移")
        up_btn.clicked.connect(self._seq_move_up)
        edit_row.addWidget(up_btn)
        down_btn = QPushButton("下移")
        down_btn.clicked.connect(self._seq_move_down)
        edit_row.addWidget(down_btn)
        edit_row.addStretch()

        # loop settings
        edit_row.addWidget(QLabel("循环次数:"))
        self._seq_loop_count = QComboBox()
        self._seq_loop_count.addItems(["1", "2", "3", "5", "10", "无限"])
        self._seq_loop_count.setCurrentText("1")
        self._seq_loop_count.setMaximumWidth(80)
        edit_row.addWidget(self._seq_loop_count)

        edit_row.addWidget(QLabel("循环间隔 (s):"))
        self._seq_loop_delay = QLineEdit("0")
        self._seq_loop_delay.setMaximumWidth(60)
        edit_row.addWidget(self._seq_loop_delay)

        self._seq_return_zero = QCheckBox("完成后归零")
        self._seq_return_zero.setChecked(True)
        edit_row.addWidget(self._seq_return_zero)

        layout.addLayout(edit_row)

        # file operations
        file_row = QHBoxLayout()
        load_btn = QPushButton("加载序列")
        load_btn.clicked.connect(self._load_sequence_file)
        file_row.addWidget(load_btn)
        save_btn = QPushButton("保存序列")
        save_btn.clicked.connect(self._save_sequence_file)
        file_row.addWidget(save_btn)
        file_row.addStretch()

        self._seq_name_edit = QLineEdit("Untitled")
        self._seq_name_edit.setMaximumWidth(200)
        file_row.addWidget(QLabel("序列名:"))
        file_row.addWidget(self._seq_name_edit)
        layout.addLayout(file_row)

        # execution controls
        exec_group = QGroupBox("执行控制")
        eg = QHBoxLayout(exec_group)

        self._seq_start_btn = QPushButton("开始")
        self._seq_start_btn.setObjectName("successBtn")
        self._seq_start_btn.clicked.connect(self._seq_start)
        eg.addWidget(self._seq_start_btn)

        self._seq_pause_btn = QPushButton("暂停")
        self._seq_pause_btn.clicked.connect(self._seq_pause)
        eg.addWidget(self._seq_pause_btn)

        self._seq_stop_btn = QPushButton("停止")
        self._seq_stop_btn.setObjectName("dangerBtn")
        self._seq_stop_btn.clicked.connect(self._seq_stop)
        eg.addWidget(self._seq_stop_btn)

        self._seq_advance_btn = QPushButton("单步")
        self._seq_advance_btn.clicked.connect(self._seq_advance)
        eg.addWidget(self._seq_advance_btn)

        eg.addStretch()

        self._seq_progress = QProgressBar()
        self._seq_progress.setFormat("步骤 %v / %m")
        self._seq_progress.setMaximumWidth(300)
        eg.addWidget(self._seq_progress)

        self._seq_status = QLabel("空闲")
        self._seq_status.setStyleSheet("font-weight: 700; color: #555;")
        eg.addWidget(self._seq_status)

        layout.addWidget(exec_group)
        layout.addStretch()
        return page

    # -----------------------------------------------------------------------
    # Page 4: Settings
    # -----------------------------------------------------------------------

    def _build_settings_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)

        title = QLabel("参数设置")
        title.setObjectName("sectionTitle")
        layout.addWidget(title)

        group = QGroupBox("系统参数")
        grid = QGridLayout(group)
        row = 0

        self._setting_widgets: Dict[str, dict] = {}
        for axis in AXES:
            grid.addWidget(QLabel(f"{axis}轴 线圈常数 (nT/mA):"), row, 0)
            cc_edit = QLineEdit(f"{self._cfg['coil_constant'][axis]:.2f}")  # type: ignore[index]
            cc_edit.setValidator(QDoubleValidator(0, 99999, 2))
            grid.addWidget(cc_edit, row, 1)
            row += 1

            grid.addWidget(QLabel(f"{axis}轴 零偏电流 (mA):"), row, 0)
            zo_edit = QLineEdit(f"{self._cfg['zero_offset'][axis]:.5f}")  # type: ignore[index]
            zo_edit.setValidator(QDoubleValidator(-99999, 99999, 5))
            grid.addWidget(zo_edit, row, 1)
            row += 1

            grid.addWidget(QLabel(f"{axis}轴 电压 (V):"), row, 0)
            volt_edit = QLineEdit("75")
            volt_edit.setValidator(QDoubleValidator(0, 999, 0))
            grid.addWidget(volt_edit, row, 1)
            row += 1

            self._setting_widgets[axis] = {"cc": cc_edit, "zo": zo_edit, "volt": volt_edit}

        grid.addWidget(QLabel("数据保存目录:"), row, 0)
        dir_row = QHBoxLayout()
        self._dir_edit = QLineEdit(str(self._cfg["data_save_dir"]))
        dir_row.addWidget(self._dir_edit)
        browse_btn = QPushButton("浏览...")
        browse_btn.clicked.connect(self._browse_save_dir)
        dir_row.addWidget(browse_btn)
        grid.addLayout(dir_row, row, 1)
        row += 1

        grid.addWidget(QLabel("轮询间隔 (ms):"), row, 0)
        self._poll_interval_spin = QSpinBox()
        self._poll_interval_spin.setRange(100, 5000)
        self._poll_interval_spin.setValue(int(self._cfg.get("poll_interval_ms", 500)))
        self._poll_interval_spin.setSingleStep(100)
        self._poll_interval_spin.setSuffix(" ms")
        grid.addWidget(self._poll_interval_spin, row, 1)
        row += 1

        save_btn = QPushButton("保存配置")
        save_btn.setObjectName("primaryBtn")
        save_btn.clicked.connect(self._save_config)
        grid.addWidget(save_btn, row, 0, 1, 2)

        layout.addWidget(group)
        layout.addStretch()
        return page

    # -----------------------------------------------------------------------
    # Page 5: Data recording
    # -----------------------------------------------------------------------

    def _build_record_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)

        title = QLabel("数据记录")
        title.setObjectName("sectionTitle")
        layout.addWidget(title)

        group = QGroupBox("记录控制")
        grid = QGridLayout(group)

        grid.addWidget(QLabel("文件名:"), 0, 0)
        self._record_file_edit = QLineEdit(MagnetFieldRecorder.timestamp_filename())
        grid.addWidget(self._record_file_edit, 0, 1)

        grid.addWidget(QLabel("记录间隔 (ms):"), 1, 0)
        self._record_interval_combo = QComboBox()
        self._record_interval_combo.addItems(["500", "1000", "2000", "5000"])
        self._record_interval_combo.setCurrentText("1000")
        grid.addWidget(self._record_interval_combo, 1, 1)

        self._record_btn = QPushButton("开始记录")
        self._record_btn.setObjectName("successBtn")
        self._record_btn.clicked.connect(self._toggle_recording)
        grid.addWidget(self._record_btn, 2, 0, 1, 2)

        self._record_status = QLabel("未在记录")
        grid.addWidget(self._record_status, 3, 0, 1, 2)

        self._record_progress = QProgressBar()
        self._record_progress.setFormat("已记录 %v 行")
        self._record_progress.setValue(0)
        self._record_progress.setMaximum(0)
        grid.addWidget(self._record_progress, 4, 0, 1, 2)

        layout.addWidget(group)
        layout.addStretch()
        return page

    # -----------------------------------------------------------------------
    # Page 6: Log
    # -----------------------------------------------------------------------

    def _build_log_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)

        title = QLabel("系统日志")
        title.setObjectName("sectionTitle")
        layout.addWidget(title)

        self._log_text = QTextEdit()
        self._log_text.setReadOnly(True)
        layout.addWidget(self._log_text)

        clear_btn = QPushButton("清空日志")
        clear_btn.clicked.connect(self._log_text.clear)
        layout.addWidget(clear_btn)

        return page

    # -----------------------------------------------------------------------
    # Page 3 (new): Vector field calculator
    # -----------------------------------------------------------------------

    def _build_vector_field_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)

        title = QLabel("向量场计算器")
        title.setObjectName("sectionTitle")
        layout.addWidget(title)

        desc = QLabel("球坐标 (B, θ, φ) ↔ 笛卡尔 (Bx, By, Bz) 转换。物理学约定: θ 从 Z 轴量起。")
        desc.setStyleSheet("color: #555; padding: 4px 0;")
        layout.addWidget(desc)

        main_row = QHBoxLayout()

        # --- Left: input controls ---
        left_col = QVBoxLayout()

        # Spherical input
        sph_group = QGroupBox("球坐标输入")
        sg = QGridLayout(sph_group)

        sg.addWidget(QLabel("场强 B (nT):"), 0, 0)
        self._vf_mag_spin = QDoubleSpinBox()
        self._vf_mag_spin.setRange(0, 9999999)
        self._vf_mag_spin.setDecimals(2)
        self._vf_mag_spin.setSingleStep(1000)
        self._vf_mag_spin.setValue(0)
        sg.addWidget(self._vf_mag_spin, 0, 1)
        self._vf_mag_slider = QSlider(Qt.Horizontal)
        self._vf_mag_slider.setRange(0, 100000)
        self._vf_mag_slider.setValue(0)
        sg.addWidget(self._vf_mag_slider, 0, 2)

        sg.addWidget(QLabel("极角 θ (°):"), 1, 0)
        self._vf_theta_spin = QDoubleSpinBox()
        self._vf_theta_spin.setRange(0, 180)
        self._vf_theta_spin.setDecimals(1)
        self._vf_theta_spin.setSingleStep(5)
        self._vf_theta_spin.setValue(0)
        sg.addWidget(self._vf_theta_spin, 1, 1)
        self._vf_theta_slider = QSlider(Qt.Horizontal)
        self._vf_theta_slider.setRange(0, 1800)  # 0.1° resolution
        self._vf_theta_slider.setValue(0)
        sg.addWidget(self._vf_theta_slider, 1, 2)

        sg.addWidget(QLabel("方位角 φ (°):"), 2, 0)
        self._vf_phi_spin = QDoubleSpinBox()
        self._vf_phi_spin.setRange(0, 360)
        self._vf_phi_spin.setDecimals(1)
        self._vf_phi_spin.setSingleStep(5)
        self._vf_phi_spin.setValue(0)
        sg.addWidget(self._vf_phi_spin, 2, 1)
        self._vf_phi_slider = QSlider(Qt.Horizontal)
        self._vf_phi_slider.setRange(0, 3600)  # 0.1° resolution
        self._vf_phi_slider.setValue(0)
        sg.addWidget(self._vf_phi_slider, 2, 2)

        # sync spin ↔ slider
        self._vf_mag_spin.valueChanged.connect(lambda v: self._vf_mag_slider.setValue(int(v)))
        self._vf_mag_slider.valueChanged.connect(lambda v: self._vf_mag_spin.setValue(v))
        self._vf_theta_spin.valueChanged.connect(lambda v: self._vf_theta_slider.setValue(int(v * 10)))
        self._vf_theta_slider.valueChanged.connect(lambda v: self._vf_theta_spin.setValue(v / 10.0))
        self._vf_phi_spin.valueChanged.connect(lambda v: self._vf_phi_slider.setValue(int(v * 10)))
        self._vf_phi_slider.valueChanged.connect(lambda v: self._vf_phi_spin.setValue(v / 10.0))

        # recalculate on any change
        self._vf_mag_spin.valueChanged.connect(self._vf_recalculate)
        self._vf_theta_spin.valueChanged.connect(self._vf_recalculate)
        self._vf_phi_spin.valueChanged.connect(self._vf_recalculate)

        left_col.addWidget(sph_group)

        # Cartesian output
        cart_group = QGroupBox("笛卡尔输出")
        cg = QGridLayout(cart_group)

        self._vf_cart_labels: Dict[str, QLabel] = {}
        for i, (axis, name) in enumerate([("X", "Bx"), ("Y", "By"), ("Z", "Bz")]):
            cg.addWidget(QLabel(f"{name} (nT):"), i, 0)
            lbl = QLabel("0.00")
            lbl.setObjectName("axisData")
            lbl.setMinimumWidth(120)
            cg.addWidget(lbl, i, 1)
            cg.addWidget(QLabel(f"{name} (μT):"), i, 2)
            lbl_ut = QLabel("0.000")
            lbl_ut.setObjectName("axisData")
            lbl_ut.setMinimumWidth(100)
            cg.addWidget(lbl_ut, i, 3)
            self._vf_cart_labels[axis] = lbl
            self._vf_cart_labels[f"{axis}_ut"] = lbl_ut

        fill_btn = QPushButton("填入控制面板")
        fill_btn.setObjectName("primaryBtn")
        fill_btn.clicked.connect(self._vf_fill_to_manual)
        cg.addWidget(fill_btn, 3, 0, 1, 4)

        left_col.addWidget(cart_group)

        # Reverse: Cartesian → Spherical
        rev_group = QGroupBox("反向计算 (笛卡尔 → 球坐标)")
        rg = QGridLayout(rev_group)
        for i, axis in enumerate(AXES):
            rg.addWidget(QLabel(f"{axis} (nT):"), 0, i * 2)
            rev_edit = QLineEdit("0.00")
            rev_edit.setValidator(QDoubleValidator(-9999999, 9999999, 2))
            rev_edit.setMaximumWidth(120)
            rg.addWidget(rev_edit, 0, i * 2 + 1)
            if not hasattr(self, "_vf_rev_edits"):
                self._vf_rev_edits: Dict[str, QLineEdit] = {}
            self._vf_rev_edits[axis] = rev_edit

        rev_btn = QPushButton("计算球坐标")
        rev_btn.clicked.connect(self._vf_reverse_calc)
        rg.addWidget(rev_btn, 1, 0, 1, 6)

        self._vf_rev_result = QLabel("B = 0 nT, θ = 0°, φ = 0°")
        self._vf_rev_result.setStyleSheet("font-weight: 600; color: #005c8a; padding: 4px;")
        rg.addWidget(self._vf_rev_result, 2, 0, 1, 6)

        left_col.addWidget(rev_group)
        left_col.addStretch()

        main_row.addLayout(left_col)

        # --- Right: visualization ---
        viz_group = QGroupBox("联动磁场可视化")
        viz_layout = QVBoxLayout(viz_group)
        self._vf_viz = VectorFieldVizWidget()
        viz_layout.addWidget(self._vf_viz)

        self._vf_summary = QLabel("目标场 |B|=0.00 nT, θ=0.0°, φ=0.0°    回读估算: 不可用")
        self._vf_summary.setStyleSheet("font-weight: 600; color: #005c8a; padding: 4px;")
        viz_layout.addWidget(self._vf_summary)

        param_group = QGroupBox("计算参数 (只读)")
        param_layout = QVBoxLayout(param_group)
        self._field_param_table = QTableWidget(3, 9)
        self._field_param_table.setHorizontalHeaderLabels([
            "轴", "C nT/mA", "零偏 mA", "目标复现 mA", "回读总 mA",
            "估算复现 mA", "目标 nT", "估算 nT", "余量 mA",
        ])
        self._field_param_table.verticalHeader().setVisible(False)
        self._field_param_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self._field_param_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._field_param_table.setMaximumHeight(150)
        param_layout.addWidget(self._field_param_table)
        viz_layout.addWidget(param_group)

        main_row.addWidget(viz_group, stretch=1)

        layout.addLayout(main_row)
        self._refresh_field_visualization()
        return page

    # -----------------------------------------------------------------------
    # Connection actions
    # -----------------------------------------------------------------------

    def _scan_ports(self) -> None:
        if serial is None:
            return
        ports = [p.device for p in serial.tools.list_ports.comports()]
        for axis in AXES:
            combo = self._conn_widgets[axis]["port"]
            current = combo.currentText()
            combo.clear()
            combo.addItems(ports)
            if current in ports:
                combo.setCurrentText(current)
            elif ports:
                combo.setCurrentText(ports[0])
        self._log(f"扫描到 {len(ports)} 个串口: {', '.join(ports) if ports else '无'}")

    def _toggle_connect(self, axis: str) -> None:
        if self._fc.is_connected(axis):
            self._disconnect_axis(axis)
        else:
            self._connect_axis(axis)

    def _connect_axis(self, axis: str) -> None:
        w = self._conn_widgets[axis]
        port = w["port"].currentText()
        baud = int(w["baud"].currentText())
        try:
            idn = self._fc.connect_axis(axis, port, baud)
            self._log(f"[{axis}] 已连接 {port}: {idn}")
        except Exception as exc:
            QMessageBox.critical(self, "连接失败", f"[{axis}] 无法连接 {port}: {exc}")
            self._log(f"[{axis}] 连接失败: {exc}")

    def _disconnect_axis(self, axis: str) -> None:
        self._fc.disconnect_axis(axis)
        self._log(f"[{axis}] 已断开")

    def _connect_all(self) -> None:
        ports = {}
        for axis in AXES:
            p = self._conn_widgets[axis]["port"].currentText()
            if p:
                ports[axis] = p
        baud = int(self._conn_widgets["X"]["baud"].currentText())
        results = self._fc.connect_all(ports, baud)
        for axis, result in results.items():
            if result.startswith("ERROR"):
                self._log(f"[{axis}] 连接失败: {result}")
            else:
                self._log(f"[{axis}] 已连接: {result}")

    def _disconnect_all(self) -> None:
        self._fc.disconnect_all()
        self._log("全部断开")

    def _emergency_stop(self) -> None:
        self._fc.all_output_off()
        if self._seq_engine.is_running:
            self._seq_engine.stop()
        for axis in AXES:
            if axis in self._axis_leds:
                self._axis_leds[axis].setProperty("on", "false")
                self._axis_leds[axis].style().unpolish(self._axis_leds[axis])
                self._axis_leds[axis].style().polish(self._axis_leds[axis])
        self._log("紧急停止: 所有输出已关闭")

    # -----------------------------------------------------------------------
    # Signal callbacks
    # -----------------------------------------------------------------------

    def _on_current_changed(self, axis: str, ma: float) -> None:
        status = self._fc.get_status(axis)
        est_recur = status["estimated_recur_current_mA"]
        est_field = status["estimated_field_nT"]
        # status bar
        self._status_current[axis].setText(
            f"{axis}: 总 {ma:.3f} mA / 复现 {self._format_optional(est_recur, 3)} mA / "
            f"B {self._format_optional(est_field, 1)} nT"
        )
        # manual page meas display
        if axis in self._field_widgets:
            self._field_widgets[axis]["meas"].setText(f"{ma:.5f}")
            self._field_widgets[axis]["est"].setText(self._format_optional(est_field, 2))
        self._refresh_field_visualization()

    def _on_connection_changed(self, axis: str, connected: bool) -> None:
        w = self._conn_widgets[axis]
        led = w["led"]
        btn = w["btn"]
        led.setProperty("on", "true" if connected else "false")
        led.style().unpolish(led)
        led.style().polish(led)
        btn.setText("断开" if connected else "连接")
        btn.setObjectName("dangerBtn" if connected else "primaryBtn")
        btn.style().unpolish(btn)
        btn.style().polish(btn)
        # status pill
        if connected:
            self._status_labels[axis].setText(f"{axis}: 已连接")
            self._status_labels[axis].setStyleSheet(
                "padding: 10px; border-radius: 3px; font-weight: 700;"
                "background-color: #c8f0d8; border: 1px solid #00a651; color: #005c8a;"
            )
        else:
            self._status_labels[axis].setText(f"{axis}: 未连接")
            self._status_labels[axis].setStyleSheet(
                "padding: 10px; border-radius: 3px; font-weight: 700;"
                "background-color: #e8e8e8; border: 1px solid #c0c0c0;"
            )
        self._update_status_conn()
        self._refresh_field_visualization()

    def _on_error(self, axis: str, msg: str) -> None:
        self._log(f"[{axis}] {msg}")

    def _update_status_conn(self) -> None:
        n = len(self._fc.connected_axes())
        self._status_conn.setText(f"连接: {n}/3")

    def _manual_target_field_values(self) -> Dict[str, float]:
        target: Dict[str, float] = {}
        if not hasattr(self, "_field_widgets"):
            return {axis: 0.0 for axis in AXES}
        for axis in AXES:
            try:
                target[axis] = float(self._field_widgets[axis]["mag"].text())
            except (KeyError, ValueError):
                target[axis] = 0.0
        return target

    def _update_negative_field_warning(self, *_args) -> None:
        if not hasattr(self, "_negative_field_warning"):
            return
        negative_axes = [
            axis
            for axis, value in self._manual_target_field_values().items()
            if value < 0
        ]
        if negative_axes:
            axes = "/".join(negative_axes)
            self._negative_field_warning.setText(
                f"提示: {axes} 轴目标磁场为负。当前单极性输出策略会阻止下发，仍可用于预览和计算。"
            )
        else:
            self._negative_field_warning.setText("")

    def _format_optional(self, value: Optional[float], digits: int = 2) -> str:
        if value is None:
            return "---"
        return f"{value:.{digits}f}"

    def _refresh_field_visualization(self, target_override: Optional[Dict[str, float]] = None) -> None:
        if not hasattr(self, "_vf_viz"):
            return

        snapshot = self._fc.get_field_snapshot()
        target = target_override if target_override is not None else self._manual_target_field_values()
        estimated = snapshot["estimated_field_nT"]
        self._vf_viz.set_fields(target, estimated)

        target_s = cartesian_to_spherical(CartesianField(
            x_nT=target["X"],
            y_nT=target["Y"],
            z_nT=target["Z"],
        ))
        if estimated is None:
            estimated_text = "回读估算: 不可用"
        else:
            est_s = cartesian_to_spherical(CartesianField(
                x_nT=estimated["X"],
                y_nT=estimated["Y"],
                z_nT=estimated["Z"],
            ))
            estimated_text = (
                f"回读估算 |B|={est_s.magnitude_nT:.2f} nT, "
                f"θ={est_s.theta_deg:.1f}°, φ={est_s.phi_deg:.1f}°"
            )
        self._vf_summary.setText(
            f"目标场 |B|={target_s.magnitude_nT:.2f} nT, "
            f"θ={target_s.theta_deg:.1f}°, φ={target_s.phi_deg:.1f}°    {estimated_text}"
        )

        if hasattr(self, "_field_param_table"):
            axes = snapshot["axes"]
            self._field_param_table.setRowCount(len(AXES))
            for row, axis in enumerate(AXES):
                status = axes[axis]
                cc = status["coil_constant"]
                target_field = target[axis]
                target_recur = target_field / cc if cc else 0.0
                values = [
                    axis,
                    f"{cc:.2f}",
                    f"{status['zero_offset_mA']:.5f}",
                    f"{target_recur:.5f}",
                    f"{status['total_current_mA']:.5f}",
                    self._format_optional(status["estimated_recur_current_mA"], 5),
                    f"{target_field:.2f}",
                    self._format_optional(status["estimated_field_nT"], 2),
                    f"{status['current_margin_mA']:.2f}",
                ]
                for col, value in enumerate(values):
                    item = QTableWidgetItem(value)
                    item.setTextAlignment(Qt.AlignCenter)
                    self._field_param_table.setItem(row, col, item)

    # -----------------------------------------------------------------------
    # Manual control actions
    # -----------------------------------------------------------------------

    def _on_set_zero(self, axis: str) -> None:
        try:
            ma = float(self._zero_widgets[axis]["edit"].text())
        except ValueError:
            QMessageBox.warning(self, "输入错误", "请输入有效的零偏电流值")
            return
        if not self._fc.set_zero_offset(axis, ma):
            return
        self._cfg["zero_offset"][axis] = ma  # type: ignore[index]
        self._log(f"[{axis}] 设置零偏电流: {ma:.5f} mA")
        self._refresh_field_visualization()

    def _on_set_field(self, axis: str) -> None:
        w = self._field_widgets[axis]
        try:
            nT = float(w["mag"].text())
        except ValueError:
            QMessageBox.warning(self, "输入错误", "请输入有效的磁场值 (nT)")
            return
        cc = self._fc.get_coil_constant(axis)
        ma = nT / cc
        w["curr"].setText(f"{ma:.5f}")
        if not self._fc.set_field(axis, nT):
            return
        self._log(f"[{axis}] 设置磁场: {nT:.2f} nT → {ma:.5f} mA")
        self._refresh_field_visualization()

    def _all_output_on(self) -> None:
        for axis in AXES:
            if self._fc.is_connected(axis):
                self._fc.set_output(axis, True)
                self._axis_leds[axis].setProperty("on", "true")
                self._axis_leds[axis].style().unpolish(self._axis_leds[axis])
                self._axis_leds[axis].style().polish(self._axis_leds[axis])
        self._log("全部输出已开启")
        self._refresh_field_visualization()

    def _all_lock_zero(self, checked: bool) -> None:
        for axis in AXES:
            self._fc.lock_zero(axis, checked)
        self._log(f"全部零场{'锁定' if checked else '解锁'}")
        self._refresh_field_visualization()

    def _set_all_zero_field(self) -> None:
        for axis in AXES:
            if self._fc.is_connected(axis):
                self._fc.set_field(axis, 0.0)
                self._field_widgets[axis]["mag"].setText("0.00")
                self._field_widgets[axis]["curr"].setText("0.00000")
        self._log("全部设为零场")
        self._refresh_field_visualization()

    # -----------------------------------------------------------------------
    # Preset actions
    # -----------------------------------------------------------------------

    def _refresh_presets(self) -> None:
        if not hasattr(self, "_preset_combo"):
            return
        self._preset_combo.clear()
        self._preset_combo.addItems(self._preset_mgr.list_names())

    def _load_preset(self) -> None:
        name = self._preset_combo.currentText()
        if not name:
            return
        preset = self._preset_mgr.get_preset(name)
        if preset is None:
            QMessageBox.warning(self, "错误", f"预设 '{name}' 不存在")
            return
        for axis, val in zip(AXES, (preset.x_nT, preset.y_nT, preset.z_nT)):
            self._field_widgets[axis]["mag"].setText(f"{val:.2f}")
            cc = self._fc.get_coil_constant(axis)
            self._field_widgets[axis]["curr"].setText(f"{val / cc:.5f}" if cc else "0.00000")
        self._log(f"已加载预设: {name} ({preset.x_nT:.0f}, {preset.y_nT:.0f}, {preset.z_nT:.0f} nT)")
        if any(v < 0 for v in (preset.x_nT, preset.y_nT, preset.z_nT)):
            self._log("提示: 当前硬件兼容模式会阻止负磁场下发，该预设仅用于预览/填入。")
        self._refresh_field_visualization()

    def _save_preset(self) -> None:
        name, ok = QInputDialog.getText(
            self, "保存预设", "预设名称:"
        )
        if not ok or not name.strip():
            return
        vals = {}
        for axis in AXES:
            try:
                vals[axis] = float(self._field_widgets[axis]["mag"].text())
            except ValueError:
                vals[axis] = 0.0
        preset = FieldPreset(
            name=name.strip(),
            x_nT=vals["X"],
            y_nT=vals["Y"],
            z_nT=vals["Z"],
        )
        self._preset_mgr.save_preset(preset)
        self._refresh_presets()
        self._preset_combo.setCurrentText(name.strip())
        self._log(f"已保存预设: {name.strip()}")

    def _delete_preset(self) -> None:
        name = self._preset_combo.currentText()
        if not name:
            return
        if self._preset_mgr.delete_preset(name):
            self._refresh_presets()
            self._log(f"已删除预设: {name}")
        else:
            QMessageBox.information(self, "提示", f"预设 '{name}' 是内置预设，无法删除")

    # -----------------------------------------------------------------------
    # Vector field actions
    # -----------------------------------------------------------------------

    def _vf_recalculate(self) -> None:
        """Spherical → Cartesian, update labels and viz."""
        s = SphericalField(
            magnitude_nT=self._vf_mag_spin.value(),
            theta_deg=self._vf_theta_spin.value(),
            phi_deg=self._vf_phi_spin.value(),
        )
        c = spherical_to_cartesian(s)
        self._vf_cart_labels["X"].setText(f"{c.x_nT:.2f}")
        self._vf_cart_labels["Y"].setText(f"{c.y_nT:.2f}")
        self._vf_cart_labels["Z"].setText(f"{c.z_nT:.2f}")
        self._vf_cart_labels["X_ut"].setText(f"{c.x_nT / 1000:.3f}")
        self._vf_cart_labels["Y_ut"].setText(f"{c.y_nT / 1000:.3f}")
        self._vf_cart_labels["Z_ut"].setText(f"{c.z_nT / 1000:.3f}")
        self._refresh_field_visualization({"X": c.x_nT, "Y": c.y_nT, "Z": c.z_nT})

    def _vf_fill_to_manual(self) -> None:
        """Copy calculated Cartesian values to the manual control page."""
        for axis in AXES:
            nT_text = self._vf_cart_labels[axis].text()
            try:
                nT = float(nT_text)
            except ValueError:
                nT = 0.0
            self._field_widgets[axis]["mag"].setText(f"{nT:.2f}")
            cc = self._fc.get_coil_constant(axis)
            self._field_widgets[axis]["curr"].setText(f"{nT / cc:.5f}" if cc else "0.00000")
        self._log(f"向量场值已填入控制面板")
        self._refresh_field_visualization()

    def _vf_reverse_calc(self) -> None:
        """Cartesian → Spherical."""
        vals = {}
        for axis in AXES:
            try:
                vals[axis] = float(self._vf_rev_edits[axis].text())
            except ValueError:
                vals[axis] = 0.0
        c = CartesianField(x_nT=vals["X"], y_nT=vals["Y"], z_nT=vals["Z"])
        s = cartesian_to_spherical(c)
        self._vf_rev_result.setText(
            f"B = {s.magnitude_nT:.2f} nT ({s.magnitude_nT / 1000:.3f} μT),  "
            f"θ = {s.theta_deg:.1f}°,  φ = {s.phi_deg:.1f}°"
        )

    # -----------------------------------------------------------------------
    # Hardware check
    # -----------------------------------------------------------------------

    def _check_hardware(self) -> None:
        results = self._fc.check_all_axes()
        self._log("=== 硬件自检 ===")
        for axis, result in results.items():
            self._log(f"  [{axis}] {result}")
        self._log("=== 自检完成 ===")

    # -----------------------------------------------------------------------
    # Sequence actions
    # -----------------------------------------------------------------------

    def _seq_add_step(self) -> None:
        row = self._seq_table.rowCount()
        self._seq_table.insertRow(row)
        self._seq_table.setItem(row, 0, QTableWidgetItem(f"Step {row + 1}"))
        for col in range(1, 4):
            self._seq_table.setItem(row, col, QTableWidgetItem("0.00"))
        self._seq_table.setItem(row, 4, QTableWidgetItem("0"))
        self._seq_table.setItem(row, 5, QTableWidgetItem("0.5"))
        self._seq_table.setItem(row, 6, QTableWidgetItem("immediate"))
        self._seq_table.setItem(row, 7, QTableWidgetItem("0"))

    def _seq_del_step(self) -> None:
        row = self._seq_table.currentRow()
        if row >= 0:
            self._seq_table.removeRow(row)

    def _seq_move_up(self) -> None:
        row = self._seq_table.currentRow()
        if row > 0:
            self._seq_table.insertRow(row - 1)
            for col in range(self._seq_table.columnCount()):
                item = self._seq_table.takeItem(row + 1, col)
                self._seq_table.setItem(row - 1, col, item)
            self._seq_table.removeRow(row + 1)
            self._seq_table.setCurrentCell(row - 1, 0)

    def _seq_move_down(self) -> None:
        row = self._seq_table.currentRow()
        if 0 <= row < self._seq_table.rowCount() - 1:
            self._seq_table.insertRow(row + 2)
            for col in range(self._seq_table.columnCount()):
                item = self._seq_table.takeItem(row, col)
                self._seq_table.setItem(row + 2, col, item)
            self._seq_table.removeRow(row)
            self._seq_table.setCurrentCell(row + 1, 0)

    def _collect_sequence(self) -> FieldSequence:
        steps = []
        for row in range(self._seq_table.rowCount()):
            def _item(r, c):
                item = self._seq_table.item(r, c)
                return item.text() if item else "0"
            steps.append(FieldStep(
                name=_item(row, 0),
                field_x_nT=float(_item(row, 1) or "0"),
                field_y_nT=float(_item(row, 2) or "0"),
                field_z_nT=float(_item(row, 3) or "0"),
                hold_seconds=float(_item(row, 4) or "0"),
                settle_seconds=float(_item(row, 5) or "0.5"),
                trigger=_item(row, 6) or "immediate",
                delay_seconds=float(_item(row, 7) or "0"),
            ))
        loop_text = self._seq_loop_count.currentText()
        loop_count = 0 if loop_text == "无限" else int(loop_text)
        return FieldSequence(
            name=self._seq_name_edit.text() or "Untitled",
            steps=steps,
            loop_count=loop_count,
            loop_delay=float(self._seq_loop_delay.text() or "0"),
            return_to_zero=self._seq_return_zero.isChecked(),
        )

    def _populate_seq_table(self, seq: FieldSequence) -> None:
        self._seq_table.setRowCount(0)
        self._seq_name_edit.setText(seq.name)
        for step in seq.steps:
            row = self._seq_table.rowCount()
            self._seq_table.insertRow(row)
            self._seq_table.setItem(row, 0, QTableWidgetItem(step.name))
            self._seq_table.setItem(row, 1, QTableWidgetItem(f"{step.field_x_nT:.2f}"))
            self._seq_table.setItem(row, 2, QTableWidgetItem(f"{step.field_y_nT:.2f}"))
            self._seq_table.setItem(row, 3, QTableWidgetItem(f"{step.field_z_nT:.2f}"))
            self._seq_table.setItem(row, 4, QTableWidgetItem(f"{step.hold_seconds:.1f}"))
            self._seq_table.setItem(row, 5, QTableWidgetItem(f"{step.settle_seconds:.1f}"))
            self._seq_table.setItem(row, 6, QTableWidgetItem(step.trigger))
            self._seq_table.setItem(row, 7, QTableWidgetItem(f"{step.delay_seconds:.1f}"))
        self._seq_loop_count.setCurrentText(
            "无限" if seq.loop_count == 0 else str(seq.loop_count)
        )
        self._seq_loop_delay.setText(str(seq.loop_delay))
        self._seq_return_zero.setChecked(seq.return_to_zero)

    def _load_sequence_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "加载序列", "", "JSON 文件 (*.json);;所有文件 (*)"
        )
        if path:
            try:
                seq = SequenceEngine.load_sequence_file(path)
                self._populate_seq_table(seq)
                self._log(f"已加载序列: {path}")
            except Exception as exc:
                QMessageBox.critical(self, "加载失败", str(exc))

    def _save_sequence_file(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self, "保存序列", "", "JSON 文件 (*.json);;所有文件 (*)"
        )
        if path:
            try:
                seq = self._collect_sequence()
                SequenceEngine.save_sequence(seq, path)
                self._log(f"已保存序列: {path}")
            except Exception as exc:
                QMessageBox.critical(self, "保存失败", str(exc))

    def _seq_start(self) -> None:
        if self._seq_engine.is_running:
            return
        seq = self._collect_sequence()
        if not seq.steps:
            QMessageBox.warning(self, "提示", "请先添加步骤")
            return
        self._seq_engine.load_sequence(seq)
        # auto-generate record path
        save_dir = self._cfg["data_save_dir"]
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        record_path = str(Path(save_dir) / f"seq_{seq.name}_{ts}.csv")
        self._seq_engine.start(record_path=record_path)
        self._seq_status.setText("运行中")
        self._seq_status.setStyleSheet("font-weight: 700; color: #00a651;")
        self._status_seq.setText("序列: 运行中")
        self._log(f"序列 [{seq.name}] 开始执行, 数据记录: {record_path}")

    def _seq_pause(self) -> None:
        if self._seq_engine.is_paused:
            self._seq_engine.resume()
            self._seq_pause_btn.setText("暂停")
            self._seq_status.setText("运行中")
            self._seq_status.setStyleSheet("font-weight: 700; color: #00a651;")
        elif self._seq_engine.is_running:
            self._seq_engine.pause()
            self._seq_pause_btn.setText("继续")
            self._seq_status.setText("已暂停")
            self._seq_status.setStyleSheet("font-weight: 700; color: #f0a030;")

    def _seq_stop(self) -> None:
        self._seq_engine.stop()

    def _seq_advance(self) -> None:
        self._seq_engine.advance()

    def _on_seq_step_started(self, idx: int, name: str) -> None:
        self._seq_progress.setValue(idx)
        self._seq_status.setText(f"步骤 {idx + 1}: {name}")
        # highlight current row
        self._seq_table.selectRow(idx)

    def _on_seq_finished(self, name: str) -> None:
        self._seq_status.setText("已完成")
        self._seq_status.setStyleSheet("font-weight: 700; color: #555;")
        self._status_seq.setText("序列: 空闲")
        self._seq_pause_btn.setText("暂停")
        self._log(f"序列 [{name}] 执行完毕")

    def _on_seq_progress(self, loop: int, step: int, total: int) -> None:
        self._seq_progress.setMaximum(total)
        self._seq_progress.setValue(step)

    # -----------------------------------------------------------------------
    # Settings actions
    # -----------------------------------------------------------------------

    def _browse_save_dir(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "选择数据保存目录")
        if d:
            self._dir_edit.setText(d)

    def _save_config(self) -> None:
        for axis in AXES:
            sw = self._setting_widgets[axis]
            try:
                cc = float(sw["cc"].text())
                zo = float(sw["zo"].text())
                v = float(sw["volt"].text())
            except ValueError:
                QMessageBox.warning(self, "输入错误", f"{axis}轴参数格式不正确")
                return
            self._cfg["coil_constant"][axis] = cc  # type: ignore[index]
            self._cfg["zero_offset"][axis] = zo  # type: ignore[index]
            self._fc.set_coil_constant(axis, cc)
            self._fc.set_zero_offset(axis, zo)
            if self._fc.is_connected(axis):
                self._fc.set_voltage(axis, v)
            # update manual page zero display
            if axis in self._zero_widgets:
                self._zero_widgets[axis]["edit"].setText(f"{zo:.5f}")
        self._cfg["data_save_dir"] = self._dir_edit.text()
        poll_interval = self._poll_interval_spin.value()
        self._cfg["poll_interval_ms"] = poll_interval
        try:
            self._fc.set_poll_interval_ms(poll_interval)
        except ValueError as exc:
            QMessageBox.warning(self, "输入错误", str(exc))
            return
        save_config(self._cfg)
        self._log(f"配置已保存，轮询间隔 {poll_interval} ms")
        self._refresh_field_visualization()

    # -----------------------------------------------------------------------
    # Recording
    # -----------------------------------------------------------------------

    def _toggle_recording(self) -> None:
        if self._recording:
            self._stop_recording()
        else:
            self._start_recording()

    def _start_recording(self) -> None:
        filename = self._record_file_edit.text()
        if not filename:
            filename = MagnetFieldRecorder.timestamp_filename()
        save_dir = self._cfg["data_save_dir"]
        filepath = str(Path(save_dir) / filename)
        try:
            self._recorder.init_file(filepath)
        except Exception as exc:
            QMessageBox.critical(self, "文件错误", f"无法创建记录文件: {exc}")
            return
        self._recording = True
        self._record_count = 0
        self._record_progress.setValue(0)
        interval = int(self._record_interval_combo.currentText())
        self._record_timer.start(interval)
        self._record_btn.setText("停止记录")
        self._record_btn.setObjectName("dangerBtn")
        self._record_btn.style().unpolish(self._record_btn)
        self._record_btn.style().polish(self._record_btn)
        self._record_status.setText(f"正在记录 → {filepath}")
        self._status_rec.setText("记录: 开")
        self._log(f"开始数据记录: {filepath} (间隔 {interval} ms)")

    def _stop_recording(self) -> None:
        self._record_timer.stop()
        self._recorder.close()
        self._recording = False
        self._record_btn.setText("开始记录")
        self._record_btn.setObjectName("successBtn")
        self._record_btn.style().unpolish(self._record_btn)
        self._record_btn.style().polish(self._record_btn)
        self._record_status.setText(f"记录已停止 (共 {self._record_count} 行)")
        self._status_rec.setText("记录: 关")
        self._log(f"数据记录已停止 (共 {self._record_count} 行)")

    def _on_record_tick(self) -> None:
        row = {}
        for a in AXES:
            status = self._fc.get_status(a)
            row[f"{a}_target_field_nT"] = status["target_field_nT"]
            row[f"{a}_total_current_mA"] = status["total_current_mA"]
            row[f"{a}_estimated_recur_current_mA"] = status["estimated_recur_current_mA"]
            row[f"{a}_estimated_field_nT"] = status["estimated_field_nT"]
        self._recorder.append_row(row)
        self._record_count += 1
        self._record_progress.setValue(self._record_count)

    # -----------------------------------------------------------------------
    # Logging
    # -----------------------------------------------------------------------

    def _setup_error_logger(self) -> None:
        errors_dir = Path(__file__).resolve().parent.parent / "Errors"
        errors_dir.mkdir(exist_ok=True)
        today = datetime.datetime.now().strftime("%Y%m%d")
        log_file = errors_dir / f"{today}.Log"
        self._error_logger = logging.getLogger("magnet_field_errors")
        self._error_logger.setLevel(logging.ERROR)
        if not self._error_logger.handlers:
            fh = logging.FileHandler(log_file, encoding="utf-8")
            fh.setFormatter(logging.Formatter("%(asctime)s  %(message)s", datefmt="%H:%M:%S"))
            self._error_logger.addHandler(fh)

    def _log(self, msg: str) -> None:
        ts = datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]
        self._log_text.append(f"[{ts}] {msg}")
        if "失败" in msg or "错误" in msg or "超时" in msg or "异常" in msg:
            self._error_logger.error(msg)

    # -----------------------------------------------------------------------
    # Close
    # -----------------------------------------------------------------------

    def closeEvent(self, event) -> None:
        if self._recording:
            self._stop_recording()
        if self._seq_engine.is_running:
            self._seq_engine.stop()
        self._fc.all_output_off()
        self._fc.disconnect_all()
        save_config(self._cfg)
        event.accept()
