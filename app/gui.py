from __future__ import annotations

import datetime
import math
from pathlib import Path
from dataclasses import asdict
from typing import Any

from PyQt5.QtCore import QThread, QTimer, Qt
from PyQt5.QtGui import QIntValidator
from PyQt5.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

import serial

from config.presets import PresetStore
from data.recorder import DataRecorder
from instruments.oe1022d import LockinSample, OE1022DController
from instruments.smb100a import (
    SMB100AController,
    SMBParameters,
    format_float,
    format_frequency,
    parse_frequency_to_hz,
    parse_power_dbm,
    parse_time_to_ms,
    parse_voltage_to_mv,
)
from workers.experiment_worker import ExperimentConfig, ExperimentWorker
from workers.lockin_worker import LockinWorker

try:
    import pyqtgraph as pg
except ImportError:  # Plot widgets are replaced with a clear placeholder until dependencies are installed.
    pg = None


FIELD_LABELS = {
    "power_dbm": "功率(dBm)",
    "cw_hz": "当前频率",
    "start_hz": "起始频率",
    "stop_hz": "终止频率",
    "step_hz": "扫频步进",
    "dwell_ms": "驻留(ms)",
    "lf_freq_hz": "LF频率",
    "lf_amp_mv": "LF幅度(mV)",
    "lf_shape": "LF波形",
    "fm_dev_hz": "FM偏差",
    "rf_output": "RF输出",
    "lf_output": "LF输出",
    "fm_state": "FM调制",
}


class ParameterReviewDialog(QDialog):
    APPLY_AND_START = 1
    SYNC_AND_CANCEL = 2
    CANCEL = 0

    def __init__(self, differences: list[tuple[str, str, str, str]], parent=None) -> None:
        super().__init__(parent)
        self.choice = self.CANCEL
        self.setWindowTitle("扫频前参数校对")
        self.resize(720, 420)

        layout = QVBoxLayout(self)
        label = QLabel("设备参数与界面参数不一致。请确认下一步操作。")
        layout.addWidget(label)

        table = QTableWidget(len(differences), 4)
        table.setHorizontalHeaderLabels(["参数", "界面值", "设备值", "状态"])
        table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        table.verticalHeader().setVisible(False)
        table.setEditTriggers(QTableWidget.NoEditTriggers)
        for row, (label_text, ui_value, device_value, status) in enumerate(differences):
            for col, value in enumerate([label_text, ui_value, device_value, status]):
                table.setItem(row, col, QTableWidgetItem(value))
        layout.addWidget(table)

        buttons = QHBoxLayout()
        apply_btn = QPushButton("用界面参数覆盖设备并开始扫频")
        sync_btn = QPushButton("同步设备参数到界面并取消扫频")
        cancel_btn = QPushButton("取消")
        apply_btn.setDefault(True)
        apply_btn.clicked.connect(self._apply)
        sync_btn.clicked.connect(self._sync)
        cancel_btn.clicked.connect(self.reject)
        buttons.addWidget(apply_btn)
        buttons.addWidget(sync_btn)
        buttons.addWidget(cancel_btn)
        layout.addLayout(buttons)

    def _apply(self) -> None:
        self.choice = self.APPLY_AND_START
        self.accept()

    def _sync(self) -> None:
        self.choice = self.SYNC_AND_CANCEL
        self.accept()


class SMB100AControlGUI(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.smb = SMB100AController()
        self.lockin = OE1022DController()
        self.presets = PresetStore()
        self.recorder = DataRecorder()

        self.is_sweeping = False
        self.last_freq_value: float | None = None
        self.cycle_stop_enabled = True
        self.cyclic_sweep_active = False
        self.remaining_cycles = 0
        self.cycle_interval_ms = 0
        self.data_packet_count = 0

        self.lockin_thread: QThread | None = None
        self.lockin_worker: LockinWorker | None = None
        self.experiment_thread: QThread | None = None
        self.experiment_worker: ExperimentWorker | None = None
        self.current_plot_payload: dict[str, Any] | None = None

        self.cycle_timer = QTimer(self)
        self.cycle_timer.timeout.connect(self._start_next_cycle)
        self.smb_update_timer = QTimer(self)
        self.smb_update_timer.timeout.connect(self.update_smb_frequency_display)

        self.init_ui()
        self.refresh_presets()

    def init_ui(self) -> None:
        self.setWindowTitle("信号源 & 锁相放大器 控制面板")
        self.setGeometry(100, 100, 1100, 760)
        self.setMinimumSize(940, 640)
        self.setStyleSheet(self.get_stylesheet())

        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QVBoxLayout(central_widget)
        main_layout.setSpacing(8)
        main_layout.setContentsMargins(8, 8, 8, 8)

        self.main_tabs = QTabWidget()
        self.main_tabs.addTab(self._build_overview_page(), "连接/总览")
        self.main_tabs.addTab(self._build_source_page(), "微波源")
        self.main_tabs.addTab(self._build_lockin_page(), "锁相实时")
        self.main_tabs.addTab(self._build_experiment_page(), "同步采集")
        self.main_tabs.addTab(self._build_plot_page(), "波形/数据")
        self.main_tabs.addTab(self._build_log_page(), "日志/诊断")
        main_layout.addWidget(self.main_tabs, 1)

        self.update_smb_ui_state()
        self.update_lockin_ui_state()
        self.update_experiment_ui_state()
        self.log_message("信号源", "程序已启动")
        self.log_message("锁相放大器", "程序已启动")

    def _build_overview_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.addWidget(self._build_connection_group())
        layout.addWidget(self._build_status_summary_group())
        layout.addStretch()
        return page

    def _build_source_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.addWidget(self._build_source_control_group())
        layout.addWidget(self._build_params_group())
        layout.addWidget(self._build_live_data_group())
        layout.addStretch()
        return page

    def _build_lockin_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.addWidget(self._build_lockin_control_group())
        record_group = QGroupBox("低频监视记录")
        record_layout = QVBoxLayout(record_group)
        record_layout.addLayout(self._build_record_row())
        layout.addWidget(record_group)
        layout.addStretch()
        return page

    def _build_log_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.addWidget(self._build_log_group())
        return page

    def _build_experiment_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)

        settings = QGroupBox("同步采集设置")
        grid = QGridLayout(settings)
        self.experiment_name_input = QLineEdit("odmr")
        self.capture_dir_input = QLineEdit(str(Path("captures").resolve()))
        self.browse_capture_dir_btn = QPushButton("浏览")
        self.browse_capture_dir_btn.clicked.connect(self.browse_capture_dir)
        self.settle_ms_input = QLineEdit("20")
        self.settle_ms_input.setValidator(QIntValidator(0, 600000))
        self.lockin_sample_interval_input = QLineEdit("1")
        self.lockin_sample_interval_input.setValidator(QIntValidator(1, 100000))
        self.lockin_sample_count_input = QLineEdit("1000")
        self.lockin_sample_count_input.setValidator(QIntValidator(1, 16384))
        self.capture_channel_a_checkbox = QCheckBox("Channel A")
        self.capture_channel_a_checkbox.setChecked(True)
        self.capture_channel_b_checkbox = QCheckBox("Channel B")
        self.capture_channel_b_checkbox.setChecked(True)

        grid.addWidget(QLabel("实验名称:"), 0, 0)
        grid.addWidget(self.experiment_name_input, 0, 1)
        grid.addWidget(QLabel("保存目录:"), 0, 2)
        grid.addWidget(self.capture_dir_input, 0, 3)
        grid.addWidget(self.browse_capture_dir_btn, 0, 4)
        grid.addWidget(QLabel("settle(ms):"), 1, 0)
        grid.addWidget(self.settle_ms_input, 1, 1)
        grid.addWidget(QLabel("采样间隔(ms):"), 1, 2)
        grid.addWidget(self.lockin_sample_interval_input, 1, 3)
        grid.addWidget(QLabel("每点样本数:"), 2, 0)
        grid.addWidget(self.lockin_sample_count_input, 2, 1)
        grid.addWidget(self.capture_channel_a_checkbox, 2, 2)
        grid.addWidget(self.capture_channel_b_checkbox, 2, 3)
        layout.addWidget(settings)

        timing = QGroupBox("同步采集流程")
        timing_layout = QVBoxLayout(timing)
        self.experiment_hint_label = QLabel("频率范围、步进、功率、RF/LF/FM 使用“微波源”页当前界面参数。")
        timing_layout.addWidget(self.experiment_hint_label)
        control_row = QHBoxLayout()
        self.start_experiment_btn = QPushButton("开始同步采集")
        self.start_experiment_btn.setObjectName("startExperimentBtn")
        self.start_experiment_btn.clicked.connect(self.start_experiment)
        self.stop_experiment_btn = QPushButton("停止采集")
        self.stop_experiment_btn.setObjectName("stopExperimentBtn")
        self.stop_experiment_btn.clicked.connect(self.stop_experiment)
        self.experiment_progress = QProgressBar()
        self.experiment_progress.setRange(0, 100)
        self.experiment_progress.setValue(0)
        control_row.addWidget(self.start_experiment_btn)
        control_row.addWidget(self.stop_experiment_btn)
        control_row.addWidget(self.experiment_progress, 1)
        timing_layout.addLayout(control_row)
        self.experiment_detail_label = QLabel("空闲")
        self.experiment_detail_label.setObjectName("experimentDetail")
        timing_layout.addWidget(self.experiment_detail_label)
        layout.addWidget(timing)
        layout.addStretch()
        return page

    def _build_plot_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        self.plot_channel_tabs = QTabWidget()
        self.plot_widgets: dict[str, dict[str, Any]] = {}
        for channel_name in ["a", "b"]:
            self.plot_channel_tabs.addTab(self._build_single_channel_plot(channel_name), f"Channel {channel_name.upper()}")
        layout.addWidget(self.plot_channel_tabs, 1)
        return page

    def _build_single_channel_plot(self, channel_name: str) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        if pg is None:
            label = QLabel("未安装 pyqtgraph，运行 pip install -r requirements.txt 后可显示波形。")
            label.setAlignment(Qt.AlignCenter)
            layout.addWidget(label, 1)
            self.plot_widgets[channel_name] = {"available": False}
            return page

        amp_plot = pg.PlotWidget()
        amp_plot.setBackground("#252525")
        amp_plot.addLegend()
        amp_plot.setLabel("left", "Amplitude", units="mV")
        amp_plot.setLabel("bottom", "Sample")
        phase_plot = pg.PlotWidget()
        phase_plot.setBackground("#252525")
        phase_plot.setLabel("left", "Theta", units="deg")
        phase_plot.setLabel("bottom", "Sample")
        curves = {
            "X_mV": amp_plot.plot(pen=pg.mkPen("#64b5f6", width=1.5), name="X"),
            "Y_mV": amp_plot.plot(pen=pg.mkPen("#81c784", width=1.5), name="Y"),
            "R_mV": amp_plot.plot(pen=pg.mkPen("#ffb74d", width=1.5), name="R"),
            "theta_deg": phase_plot.plot(pen=pg.mkPen("#f06292", width=1.5), name="θ"),
        }
        layout.addWidget(amp_plot, 2)
        layout.addWidget(phase_plot, 1)
        self.plot_widgets[channel_name] = {"available": True, "curves": curves}
        return page

    def _build_connection_group(self) -> QGroupBox:
        group = QGroupBox("设备连接")
        layout = QVBoxLayout(group)

        smb_row = QHBoxLayout()
        smb_row.addWidget(QLabel("信号源 VISA:"))
        self.smb_address_input = QLineEdit("USB::0x0AAD::0x0054::101623::INSTR")
        smb_row.addWidget(self.smb_address_input, 1)
        self.smb_conn_btn = QPushButton("连接")
        self.smb_conn_btn.setObjectName("connBtn")
        self.smb_conn_btn.clicked.connect(self.toggle_smb_connection)
        smb_row.addWidget(self.smb_conn_btn)
        layout.addLayout(smb_row)

        # OE1022D 连接设置
        lockin_mode_row = QHBoxLayout()
        lockin_mode_row.addWidget(QLabel("锁相放大器:"))
        self.lockin_mode_combo = QComboBox()
        self.lockin_mode_combo.addItems(["RS232", "USB"])
        self.lockin_mode_combo.setCurrentText("RS232")
        self.lockin_mode_combo.currentTextChanged.connect(self.on_lockin_mode_changed)
        lockin_mode_row.addWidget(self.lockin_mode_combo)

        # RS232 模式设置
        self.lockin_rs232_widget = QWidget()
        rs232_layout = QHBoxLayout()
        rs232_layout.setContentsMargins(0, 0, 0, 0)
        self.lockin_port_combo = QComboBox()
        self.lockin_port_combo.setEditable(True)
        self.lockin_port_combo.addItems([f"COM{i}" for i in range(1, 21)])
        self.lockin_port_combo.setCurrentText("COM5")
        rs232_layout.addWidget(self.lockin_port_combo, 1)
        self.lockin_baud_combo = QComboBox()
        self.lockin_baud_combo.addItems(["9600", "19200", "38400", "57600", "115200", "921600"])
        self.lockin_baud_combo.setCurrentText("921600")
        rs232_layout.addWidget(self.lockin_baud_combo)
        self.scan_ports_btn = QPushButton("扫描")
        self.scan_ports_btn.clicked.connect(self.scan_serial_ports)
        rs232_layout.addWidget(self.scan_ports_btn)
        self.lockin_rs232_widget.setLayout(rs232_layout)
        lockin_mode_row.addWidget(self.lockin_rs232_widget, 1)

        # USB 模式设置
        self.lockin_usb_widget = QWidget()
        usb_layout = QHBoxLayout()
        usb_layout.setContentsMargins(0, 0, 0, 0)
        self.lockin_usb_combo = QComboBox()
        self.lockin_usb_combo.setEditable(True)
        self.lockin_usb_combo.addItems([f"COM{i}" for i in range(1, 21)])
        self.lockin_usb_combo.setCurrentText("COM5")
        usb_layout.addWidget(self.lockin_usb_combo, 1)
        self.scan_usb_btn = QPushButton("扫描USB")
        self.scan_usb_btn.clicked.connect(self.scan_usb_devices)
        usb_layout.addWidget(self.scan_usb_btn)
        self.lockin_usb_widget.setLayout(usb_layout)
        self.lockin_usb_widget.setVisible(False)
        lockin_mode_row.addWidget(self.lockin_usb_widget, 1)

        self.lockin_conn_btn = QPushButton("连接")
        self.lockin_conn_btn.setObjectName("connBtn")
        self.lockin_conn_btn.clicked.connect(self.toggle_lockin_connection)
        lockin_mode_row.addWidget(self.lockin_conn_btn)
        layout.addLayout(lockin_mode_row)

        return group

    def _build_status_summary_group(self) -> QGroupBox:
        group = QGroupBox("总览")
        layout = QGridLayout(group)
        self.smb_status_label = QLabel("SMB100A: 未连接")
        self.lockin_status_label = QLabel("OE1022D: 未连接")
        self.experiment_status_label = QLabel("同步采集: 空闲")
        for label in [self.smb_status_label, self.lockin_status_label, self.experiment_status_label]:
            label.setObjectName("statusPill")
            label.setAlignment(Qt.AlignCenter)
            label.setMinimumHeight(42)
        layout.addWidget(self.smb_status_label, 0, 0)
        layout.addWidget(self.lockin_status_label, 0, 1)
        layout.addWidget(self.experiment_status_label, 0, 2)
        return group

    def _build_live_data_group(self) -> QGroupBox:
        group = QGroupBox("实时数据")
        layout = QVBoxLayout(group)

        self.smb_freq_display = QLabel("当前频率: N/A")
        self.smb_freq_display.setAlignment(Qt.AlignCenter)
        self.smb_freq_display.setObjectName("freqDisplay")
        layout.addWidget(self.smb_freq_display)

        data_row = QHBoxLayout()
        for label, attr in [
            ("X 幅值（mV）：", "x_display"),
            ("Y 幅值（mV）：", "y_display"),
            ("R 幅值（mV）：", "r_display"),
            ("θ 度数：", "theta_display"),
        ]:
            box = QVBoxLayout()
            box.addWidget(QLabel(label))
            display = QLabel("0.00000000000")
            display.setObjectName("lockinDataDisplay")
            box.addWidget(display)
            wrapper = QWidget()
            wrapper.setLayout(box)
            data_row.addWidget(wrapper, 1)
            setattr(self, attr, display)
        layout.addLayout(data_row)
        return group

    def _build_control_row(self) -> QWidget:
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.addWidget(self._build_source_control_group(), 2)
        layout.addWidget(self._build_lockin_control_group(), 1)
        return row

    def _build_source_control_group(self) -> QGroupBox:
        group = QGroupBox("微波源控制")
        layout = QVBoxLayout(group)

        sweep_row = QHBoxLayout()
        self.start_sweep_btn = QPushButton("开始扫频")
        self.start_sweep_btn.setObjectName("startSweepBtn")
        self.start_sweep_btn.clicked.connect(self.start_sweep)
        self.stop_sweep_btn = QPushButton("停止扫频")
        self.stop_sweep_btn.setObjectName("stopSweepBtn")
        self.stop_sweep_btn.clicked.connect(lambda: self.stop_sweep(manual=True))
        self.sweep_status_label = QLabel("扫频已停止")
        self.sweep_status_label.setObjectName("sweepStatusLabel")
        self.sweep_status_label.setAlignment(Qt.AlignCenter)
        sweep_row.addWidget(self.start_sweep_btn)
        sweep_row.addWidget(self.stop_sweep_btn)
        sweep_row.addWidget(self.sweep_status_label)
        layout.addLayout(sweep_row)

        cycle_row = QHBoxLayout()
        cycle_row.addWidget(QLabel("循环次数:"))
        self.cycle_count_input = QLineEdit("0")
        self.cycle_count_input.setValidator(QIntValidator(0, 9999))
        cycle_row.addWidget(self.cycle_count_input)
        cycle_row.addWidget(QLabel("循环间隔(ms):"))
        self.cycle_interval_input = QLineEdit("200")
        self.cycle_interval_input.setValidator(QIntValidator(0, 3600000))
        cycle_row.addWidget(self.cycle_interval_input)
        layout.addLayout(cycle_row)

        output_row = QHBoxLayout()
        self.lf_output_checkbox = QCheckBox("LF输出")
        self.lf_output_checkbox.stateChanged.connect(self.toggle_lf_output)
        self.fm_mod_checkbox = QCheckBox("FM调制")
        self.fm_mod_checkbox.stateChanged.connect(self.toggle_fm_mod)
        self.output_checkbox = QCheckBox("RF输出")
        self.output_checkbox.stateChanged.connect(self.toggle_output)
        output_row.addWidget(self.lf_output_checkbox)
        output_row.addWidget(self.fm_mod_checkbox)
        output_row.addWidget(self.output_checkbox)
        output_row.addStretch()
        layout.addLayout(output_row)
        return group

    def _build_lockin_control_group(self) -> QGroupBox:
        group = QGroupBox("锁相控制")
        layout = QHBoxLayout(group)
        self.start_query_btn = QPushButton("开始查询")
        self.start_query_btn.setObjectName("startQueryBtn")
        self.start_query_btn.clicked.connect(self.start_query)
        self.stop_query_btn = QPushButton("停止查询")
        self.stop_query_btn.setObjectName("stopQueryBtn")
        self.stop_query_btn.clicked.connect(self.stop_query)
        layout.addWidget(self.start_query_btn)
        layout.addWidget(self.stop_query_btn)
        return group

    def _build_params_group(self) -> QGroupBox:
        group = QGroupBox("参数设置与数据记录")
        layout = QVBoxLayout(group)

        grid = QGridLayout()
        input_width = 105

        self.power_input = self._add_param(grid, 0, 0, "功率(dBm):", "-30", self.set_power, input_width)
        self.start_freq_input = self._add_param(grid, 0, 3, "起始频率:", "2.82 GHz", self.set_start_frequency, input_width)
        self.stop_freq_input = self._add_param(grid, 0, 6, "终止频率:", "2.92 GHz", self.set_stop_frequency, input_width)
        self.step_input = self._add_param(grid, 1, 0, "扫频步进:", "500 kHz", self.set_step, input_width)
        self.dwell_input = self._add_param(grid, 1, 3, "驻留(ms):", "500", self.set_dwell, input_width)
        self.lf_freq_input = self._add_param(grid, 1, 6, "LF频率(Hz):", "500", self.set_lf_frequency, input_width)
        self.lf_amp_input = self._add_param(grid, 2, 0, "LF幅度(mV):", "137", self.set_lf_amplitude, input_width)

        grid.addWidget(QLabel("LF波形:"), 2, 3)
        self.lf_shape_combo = QComboBox()
        self.lf_shape_combo.addItems(["SINE", "SQUARE", "TRIANGLE", "SAWTOOTH", "ISAWTOOTH"])
        self.lf_shape_combo.setCurrentText("SQUARE")
        grid.addWidget(self.lf_shape_combo, 2, 4)
        self.set_lf_shape_btn = QPushButton("设")
        self.set_lf_shape_btn.setObjectName("paramBtn")
        self.set_lf_shape_btn.clicked.connect(self.set_lf_shape)
        grid.addWidget(self.set_lf_shape_btn, 2, 5)

        self.fm_dev_input = self._add_param(grid, 2, 6, "FM偏差:", "4E6", self.set_fm_deviation, input_width)
        self.cw_freq_input = self._add_param(grid, 3, 0, "当前频率:", "2.82 GHz", self.set_cw_frequency, input_width)

        self.apply_params_btn = QPushButton("应用所有参数")
        self.apply_params_btn.clicked.connect(self.apply_parameters)
        grid.addWidget(self.apply_params_btn, 4, 0, 1, 9)
        layout.addLayout(grid)

        layout.addLayout(self._build_preset_row())
        return group

    def _add_param(self, grid: QGridLayout, row: int, col: int, label: str, default: str, slot, width: int) -> QLineEdit:
        grid.addWidget(QLabel(label), row, col)
        line_edit = QLineEdit(default)
        line_edit.setFixedWidth(width)
        grid.addWidget(line_edit, row, col + 1)
        button = QPushButton("设")
        button.setObjectName("paramBtn")
        button.clicked.connect(slot)
        grid.addWidget(button, row, col + 2)
        return line_edit

    def _build_preset_row(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.addWidget(QLabel("快捷配置:"))
        self.preset_combo = QComboBox()
        self.preset_combo.setEditable(True)
        row.addWidget(self.preset_combo, 1)
        self.load_preset_btn = QPushButton("加载")
        self.load_preset_btn.clicked.connect(self.load_selected_preset)
        self.save_preset_btn = QPushButton("保存")
        self.save_preset_btn.clicked.connect(self.save_selected_preset)
        self.save_as_preset_btn = QPushButton("另存为")
        self.save_as_preset_btn.clicked.connect(self.save_preset_as)
        self.delete_preset_btn = QPushButton("删除")
        self.delete_preset_btn.clicked.connect(self.delete_selected_preset)
        for button in [self.load_preset_btn, self.save_preset_btn, self.save_as_preset_btn, self.delete_preset_btn]:
            row.addWidget(button)
        return row

    def _build_record_row(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.addWidget(QLabel("采集间隔(ms):"))
        self.query_interval_input = QLineEdit("94")
        self.query_interval_input.setValidator(QIntValidator(10, 5000))
        row.addWidget(self.query_interval_input)
        self.record_data_checkbox = QCheckBox("保存到文件")
        row.addWidget(self.record_data_checkbox)
        self.record_file_input = QLineEdit("lockin_data.csv")
        row.addWidget(self.record_file_input, 1)
        self.browse_file_btn = QPushButton("浏览")
        self.browse_file_btn.clicked.connect(self.browse_record_file)
        row.addWidget(self.browse_file_btn)
        return row

    def _build_log_group(self) -> QGroupBox:
        group = QGroupBox("日志信息")
        layout = QVBoxLayout(group)
        count_row = QHBoxLayout()
        count_row.addWidget(QLabel("数据包计数:"))
        self.data_count_label = QLabel("0")
        self.data_count_label.setObjectName("dataCountLabel")
        count_row.addWidget(self.data_count_label)
        count_row.addStretch()
        layout.addLayout(count_row)

        self.info_tabs = QTabWidget()
        self.smb_info_text = QTextEdit()
        self.smb_info_text.setReadOnly(True)
        self.lockin_info_text = QTextEdit()
        self.lockin_info_text.setReadOnly(True)
        self.info_tabs.addTab(self.smb_info_text, "信号源日志")
        self.info_tabs.addTab(self.lockin_info_text, "锁相放大器日志")
        layout.addWidget(self.info_tabs)

        clear_row = QHBoxLayout()
        clear_row.addStretch()
        clear_smb = QPushButton("清空信号源日志")
        clear_smb.clicked.connect(self.smb_info_text.clear)
        clear_lockin = QPushButton("清空锁相放大器日志")
        clear_lockin.clicked.connect(self.lockin_info_text.clear)
        clear_row.addWidget(clear_smb)
        clear_row.addWidget(clear_lockin)
        layout.addLayout(clear_row)
        return group

    def get_stylesheet(self) -> str:
        return """
            QMainWindow { background-color: #4d4d4d; }
            QWidget { color: #ffffff; font-family: "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif; }
            QGroupBox {
                font-weight: bold; border: 2px solid #3a3a3a; border-radius: 6px;
                margin-top: 12px; padding-top: 10px; color: #e0e0e0;
            }
            QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 8px; color: #4CAF50; background-color: #1e1e1e; }
            QTabWidget::pane { border: 1px solid #3a3a3a; border-radius: 6px; background-color: #4d4d4d; }
            QTabBar::tab { min-width: 92px; padding: 8px 14px; background-color: #333333; border: 1px solid #3a3a3a; color: #e0e0e0; }
            QTabBar::tab:selected { background-color: #1f6f8b; color: #ffffff; }
            QPushButton { min-height: 30px; padding: 6px 12px; border-radius: 5px; font-weight: bold; border: 1px solid #3a3a3a; background-color: #2d2d2d; color: #ffffff; }
            QPushButton:hover { background-color: #3e3e3e; border: 1px solid #5a5a5a; }
            QPushButton:pressed { background-color: #111111; padding-top: 8px; padding-bottom: 4px; }
            QPushButton:disabled { background-color: #666666; color: #bdbdbd; border: 1px solid #555555; }
            QPushButton#connBtn { background-color: #4CAF50; color: white; border: none; }
            QPushButton#startSweepBtn { background-color: #2196F3; color: white; border: none; }
            QPushButton#stopSweepBtn { background-color: #FF9800; color: white; border: none; }
            QPushButton#paramBtn { background-color: #4CAF50; color: white; border: none; }
            QPushButton#startQueryBtn { background-color: #FF5722; color: white; border: none; }
            QPushButton#stopQueryBtn { background-color: #757575; color: white; border: none; }
            QPushButton#startExperimentBtn { background-color: #1565C0; color: white; border: none; }
            QPushButton#stopExperimentBtn { background-color: #C62828; color: white; border: none; }
            QLineEdit, QComboBox { padding: 4px; border: 1px solid #3a3a3a; border-radius: 4px; background-color: #2d2d2d; color: #ffffff; selection-background-color: #4CAF50; }
            QTextEdit { font-family: Consolas, "Courier New", monospace; font-size: 11px; background-color: #252525; border: 1px solid #3a3a3a; color: #d0d0d0; }
            QLabel#freqDisplay { font-size: 22px; font-weight: bold; color: #64b5f6; padding: 8px; border: 1px solid #64b5f6; border-radius: 10px; background-color: #252525; }
            QLabel#lockinDataDisplay { font-size: 14px; font-weight: bold; color: #64b5f6; padding: 6px; border: 1px solid #3a3a3a; border-radius: 5px; background-color: #252525; }
            QLabel#dataCountLabel { font-weight: bold; color: #81c784; font-size: 13px; }
            QLabel#sweepStatusLabel { font-size: 13px; font-weight: bold; color: #ffffff; padding: 5px 10px; border-radius: 15px; background-color: #555555; }
            QLabel#statusPill, QLabel#experimentDetail { font-size: 13px; font-weight: bold; color: #ffffff; padding: 8px; border-radius: 6px; background-color: #252525; border: 1px solid #3a3a3a; }
            QProgressBar { border: 1px solid #3a3a3a; border-radius: 5px; text-align: center; background-color: #252525; min-height: 26px; }
            QProgressBar::chunk { background-color: #1f9d55; border-radius: 4px; }
        """

    def update_smb_ui_state(self) -> None:
        connected = self.smb.is_connected
        experiment_running = self.experiment_worker is not None
        self.smb_conn_btn.setText("断开" if connected else "连接")
        self.smb_conn_btn.setStyleSheet(
            "background-color: #f44336; color: white;" if connected else "background-color: #4CAF50; color: white;"
        )
        if hasattr(self, "smb_status_label"):
            self.smb_status_label.setText("SMB100A: 已连接" if connected else "SMB100A: 未连接")
        for widget in [
            self.start_sweep_btn,
            self.apply_params_btn,
            self.set_lf_shape_btn,
        ]:
            widget.setEnabled(connected and not experiment_running)
        for button in self.findChildren(QPushButton):
            if button.objectName() == "paramBtn":
                button.setEnabled(connected and not experiment_running)
        for widget in [
            self.power_input,
            self.start_freq_input,
            self.stop_freq_input,
            self.step_input,
            self.dwell_input,
            self.lf_freq_input,
            self.lf_amp_input,
            self.lf_shape_combo,
            self.fm_dev_input,
            self.cw_freq_input,
            self.output_checkbox,
            self.lf_output_checkbox,
            self.fm_mod_checkbox,
            self.load_preset_btn,
            self.save_preset_btn,
            self.save_as_preset_btn,
            self.delete_preset_btn,
        ]:
            widget.setEnabled(not experiment_running)
        self.stop_sweep_btn.setEnabled(connected and self.is_sweeping)
        if not connected:
            self.smb_freq_display.setText("当前频率: N/A")
        self.update_experiment_ui_state()

    def update_lockin_ui_state(self) -> None:
        connected = self.lockin.is_connected
        self.lockin_conn_btn.setText("断开" if connected else "连接")
        self.lockin_conn_btn.setStyleSheet(
            "background-color: #f44336; color: white;" if connected else "background-color: #4CAF50; color: white;"
        )
        if hasattr(self, "lockin_status_label"):
            mode = self.lockin_mode_combo.currentText()
            if connected:
                self.lockin_status_label.setText(f"OE1022D: 已连接({mode})")
                self.lockin_status_label.setStyleSheet(
                    "QLabel#statusPill { font-size: 13px; font-weight: bold; color: #ffffff; "
                    "padding: 8px; border-radius: 6px; background-color: #4CAF50; border: 1px solid #3a3a3a; }"
                )
            else:
                self.lockin_status_label.setText("OE1022D: 未连接")
                self.lockin_status_label.setStyleSheet(
                    "QLabel#statusPill { font-size: 13px; font-weight: bold; color: #ffffff; "
                    "padding: 8px; border-radius: 6px; background-color: #252525; border: 1px solid #3a3a3a; }"
                )
        # 连接时禁用模式切换和端口选择
        if hasattr(self, "lockin_mode_combo"):
            self.lockin_mode_combo.setEnabled(not connected)
        if hasattr(self, "lockin_rs232_widget"):
            self.lockin_rs232_widget.setEnabled(not connected)
        if hasattr(self, "lockin_usb_widget"):
            self.lockin_usb_widget.setEnabled(not connected)
        self.start_query_btn.setEnabled(connected and self.lockin_worker is None and self.experiment_worker is None)
        self.stop_query_btn.setEnabled(connected and self.lockin_worker is not None)
        if not connected:
            for display in [self.x_display, self.y_display, self.r_display, self.theta_display]:
                display.setText("0.00000000000")
        self.update_experiment_ui_state()

    def update_experiment_ui_state(self) -> None:
        if not hasattr(self, "start_experiment_btn"):
            return
        running = self.experiment_worker is not None
        ready = self.smb.is_connected and self.lockin.is_connected and not self.is_sweeping
        self.smb_conn_btn.setEnabled(not running)
        self.lockin_conn_btn.setEnabled(not running)
        self.start_experiment_btn.setEnabled(ready and not running)
        self.stop_experiment_btn.setEnabled(running)
        for widget in [
            self.experiment_name_input,
            self.capture_dir_input,
            self.browse_capture_dir_btn,
            self.settle_ms_input,
            self.lockin_sample_interval_input,
            self.lockin_sample_count_input,
            self.capture_channel_a_checkbox,
            self.capture_channel_b_checkbox,
        ]:
            widget.setEnabled(not running)
        if hasattr(self, "experiment_status_label"):
            self.experiment_status_label.setText("同步采集: 运行中" if running else "同步采集: 空闲")

    def log_message(self, device: str, message: str) -> None:
        timestamp = datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]
        target = self.smb_info_text if device == "信号源" else self.lockin_info_text
        target.append(f"[{timestamp}] {message}")
        lines = target.toPlainText().splitlines()
        if len(lines) > 100:
            target.setPlainText("\n".join(lines[-100:]))

    def toggle_smb_connection(self) -> None:
        if self.smb.is_connected:
            self.disconnect_smb()
        else:
            self.connect_smb()

    def connect_smb(self) -> None:
        address = self.smb_address_input.text().strip()
        if not address:
            QMessageBox.warning(self, "警告", "请输入VISA地址")
            return
        self.smb_conn_btn.setEnabled(False)
        # 显示连接中状态（橙色）
        if hasattr(self, "smb_status_label"):
            self.smb_status_label.setText("SMB100A: 连接中...")
            self.smb_status_label.setStyleSheet(
                "QLabel#statusPill { font-size: 13px; font-weight: bold; color: #ffffff; "
                "padding: 8px; border-radius: 6px; background-color: #FFA500; border: 1px solid #3a3a3a; }"
            )
        try:
            self.log_message("信号源", f"连接 {address}")
            idn = self.smb.connect(address)
            self.log_message("信号源", f"成功: {idn}")
            self.get_current_settings()
        except Exception as exc:
            self.log_message("信号源", f"连接失败: {exc}")
            if hasattr(self, "smb_status_label"):
                self.smb_status_label.setText("SMB100A: 连接失败")
                self.smb_status_label.setStyleSheet(
                    "QLabel#statusPill { font-size: 13px; font-weight: bold; color: #ffffff; "
                    "padding: 8px; border-radius: 6px; background-color: #f44336; border: 1px solid #3a3a3a; }"
                )
            QMessageBox.critical(self, "连接错误", str(exc))
            self.smb.close(rf_off=False)
        finally:
            self.update_smb_ui_state()
            self.smb_conn_btn.setEnabled(True)

    def disconnect_smb(self) -> None:
        if self.is_sweeping:
            self.stop_sweep(manual=True)
        self.cyclic_sweep_active = False
        self.remaining_cycles = 0
        self.cycle_timer.stop()
        self.smb.close(rf_off=True)
        self.sweep_status_label.setText("扫频已停止")
        self.update_smb_ui_state()

    def toggle_lockin_connection(self) -> None:
        if self.lockin.is_connected:
            self.disconnect_lockin()
        else:
            self.connect_lockin()

    def connect_lockin(self) -> None:
        mode = self.lockin_mode_combo.currentText()
        self.lockin_conn_btn.setEnabled(False)

        # 显示连接中状态（橙色）
        if hasattr(self, "lockin_status_label"):
            self.lockin_status_label.setText("OE1022D: 连接中...")
            self.lockin_status_label.setStyleSheet(
                "QLabel#statusPill { font-size: 13px; font-weight: bold; color: #ffffff; "
                "padding: 8px; border-radius: 6px; background-color: #FFA500; border: 1px solid #3a3a3a; }"
            )

        try:
            if mode == "USB":
                port = self.lockin_usb_combo.currentText().strip()
                self.log_message("锁相放大器", f"USB 连接 {port}")
                self.lockin.connect_usb(port)
            else:
                port = self.lockin_port_combo.currentText().strip()
                baud = int(self.lockin_baud_combo.currentText())
                self.log_message("锁相放大器", f"RS232 连接 {port} @ {baud}")
                self.lockin.connect_rs232(port, baud)

            self.log_message("锁相放大器", "连接成功")
            response = self.lockin.identify()
            if response.strip():
                self.log_message("锁相放大器", f"设备: {response.strip()}")
            self.start_query()

        except Exception as exc:
            self.log_message("锁相放大器", f"连接失败: {exc}")
            if hasattr(self, "lockin_status_label"):
                self.lockin_status_label.setText("OE1022D: 连接失败")
                self.lockin_status_label.setStyleSheet(
                    "QLabel#statusPill { font-size: 13px; font-weight: bold; color: #ffffff; "
                    "padding: 8px; border-radius: 6px; background-color: #f44336; border: 1px solid #3a3a3a; }"
                )
            QMessageBox.critical(self, "连接错误", str(exc))
            self.lockin.close()
        finally:
            self.update_lockin_ui_state()
            self.lockin_conn_btn.setEnabled(True)

    def disconnect_lockin(self) -> None:
        self.stop_query()
        self.lockin.close()
        self.log_message("锁相放大器", "已断开")
        self.update_lockin_ui_state()

    def scan_serial_ports(self) -> None:
        self.log_message("锁相放大器", "扫描串口...")
        available: list[str] = []
        for port_num in range(1, 21):
            port = f"COM{port_num}"
            try:
                probe = serial.Serial(port)
                probe.close()
                available.append(port)
            except Exception:
                pass
        if available:
            self.lockin_port_combo.clear()
            self.lockin_port_combo.addItems(available)
            self.lockin_port_combo.setCurrentText(available[0])
            self.log_message("锁相放大器", f"找到 {len(available)} 个串口")
        else:
            QMessageBox.warning(self, "串口扫描", "未找到可用串口")

    def on_lockin_mode_changed(self, mode: str) -> None:
        """切换 RS232 / USB 模式"""
        if mode == "USB":
            self.lockin_rs232_widget.setVisible(False)
            self.lockin_usb_widget.setVisible(True)
        else:
            self.lockin_rs232_widget.setVisible(True)
            self.lockin_usb_widget.setVisible(False)

    def scan_usb_devices(self) -> None:
        """扫描 USB 设备（显示为 COM 端口的 USB 设备）"""
        self.log_message("锁相放大器", "扫描 USB 设备...")
        available: list[str] = []
        import serial.tools.list_ports
        ports = serial.tools.list_ports.comports()
        for p in ports:
            # 检查是否为 USB 设备
            if "USB" in p.description.upper() or "FTDI" in p.description.upper():
                available.append(p.device)
        if available:
            self.lockin_usb_combo.clear()
            self.lockin_usb_combo.addItems(available)
            self.lockin_usb_combo.setCurrentText(available[0])
            self.log_message("锁相放大器", f"找到 {len(available)} 个 USB 设备: {', '.join(available)}")
        else:
            # 如果没找到 USB 设备，显示所有 COM 端口
            all_ports = [p.device for p in ports]
            self.lockin_usb_combo.clear()
            self.lockin_usb_combo.addItems(all_ports)
            if all_ports:
                self.lockin_usb_combo.setCurrentText(all_ports[0])
            QMessageBox.warning(self, "USB 扫描", f"未找到专用 USB 端口，显示所有 COM 端口: {', '.join(all_ports) if all_ports else '无'}")

    def get_current_settings(self) -> None:
        try:
            params = self.smb.read_parameters()
            self._update_current_frequency_display(params.cw_hz)
            self.log_message("信号源", f"频率: {format_frequency(params.cw_hz)}, 功率: {format_float(params.power_dbm, ' dBm')}")
        except Exception as exc:
            self.log_message("信号源", f"获取设置失败: {exc}")

    def _ui_params(self) -> SMBParameters:
        return SMBParameters(
            power_dbm=parse_power_dbm(self.power_input.text()),
            cw_hz=parse_frequency_to_hz(self.cw_freq_input.text()),
            start_hz=parse_frequency_to_hz(self.start_freq_input.text()),
            stop_hz=parse_frequency_to_hz(self.stop_freq_input.text()),
            step_hz=parse_frequency_to_hz(self.step_input.text()),
            dwell_ms=parse_time_to_ms(self.dwell_input.text()),
            lf_freq_hz=parse_frequency_to_hz(self.lf_freq_input.text()),
            lf_amp_mv=parse_voltage_to_mv(self.lf_amp_input.text()),
            lf_shape=self.lf_shape_combo.currentText(),
            fm_dev_hz=parse_frequency_to_hz(self.fm_dev_input.text()),
            rf_output=self.output_checkbox.isChecked(),
            lf_output=self.lf_output_checkbox.isChecked(),
            fm_state=self.fm_mod_checkbox.isChecked(),
        )

    def _set_ui_from_smb_params(self, params: SMBParameters) -> None:
        if params.power_dbm is not None:
            self.power_input.setText(format_float(params.power_dbm))
        if params.cw_hz is not None:
            self.cw_freq_input.setText(format_frequency(params.cw_hz))
        if params.start_hz is not None:
            self.start_freq_input.setText(format_frequency(params.start_hz))
        if params.stop_hz is not None:
            self.stop_freq_input.setText(format_frequency(params.stop_hz))
        if params.step_hz is not None:
            self.step_input.setText(format_frequency(params.step_hz))
        if params.dwell_ms is not None:
            self.dwell_input.setText(format_float(params.dwell_ms))
        if params.lf_freq_hz is not None:
            self.lf_freq_input.setText(format_float(params.lf_freq_hz))
        if params.lf_amp_mv is not None:
            self.lf_amp_input.setText(format_float(params.lf_amp_mv))
        if params.lf_shape:
            self.lf_shape_combo.setCurrentText(params.lf_shape.upper())
        if params.fm_dev_hz is not None:
            self.fm_dev_input.setText(format_float(params.fm_dev_hz))
        self._set_checkbox_safely(self.output_checkbox, params.rf_output)
        self._set_checkbox_safely(self.lf_output_checkbox, params.lf_output)
        self._set_checkbox_safely(self.fm_mod_checkbox, params.fm_state)

    def _set_checkbox_safely(self, checkbox: QCheckBox, value: bool | None) -> None:
        if value is None:
            return
        checkbox.blockSignals(True)
        checkbox.setChecked(value)
        checkbox.blockSignals(False)

    def set_power(self) -> None:
        self._write_smb(lambda params: self.smb.write(f"POW:LEV {params.power_dbm:.12g}DBM"), "功率设定")

    def set_start_frequency(self) -> None:
        self._write_smb(lambda params: self.smb.write(f"FREQ:START {params.start_hz:.12g}Hz"), "起始频率设定")

    def set_stop_frequency(self) -> None:
        self._write_smb(lambda params: self.smb.write(f"FREQ:STOP {params.stop_hz:.12g}Hz"), "终止频率设定")

    def set_step(self) -> None:
        self._write_smb(lambda params: self.smb.write(f"SWE:STEP {params.step_hz:.12g}Hz"), "步进设定")

    def set_dwell(self) -> None:
        self._write_smb(lambda params: self.smb.write(f"SWE:DWELL {params.dwell_ms:.12g}MS"), "驻留设定")

    def set_lf_frequency(self) -> None:
        self._write_smb(lambda params: self.smb.write(f"SOUR:LFO:FREQ {params.lf_freq_hz:.12g}Hz"), "LF频率设定")

    def set_lf_amplitude(self) -> None:
        self._write_smb(lambda params: self.smb.write(f"SOUR:LFO:VOLT {params.lf_amp_mv:.12g} mV"), "LF幅度设定")

    def set_lf_shape(self) -> None:
        self._write_smb(lambda params: self.smb.write(f"SOUR:LFO:SHAP {params.lf_shape}"), "LF波形设定")

    def set_fm_deviation(self) -> None:
        self._write_smb(lambda params: self.smb.write(f"SOUR:FM:DEV {params.fm_dev_hz:.12g}"), "FM偏差设定")

    def set_cw_frequency(self) -> None:
        if self.is_sweeping:
            QMessageBox.warning(self, "警告", "扫频模式下无法设置CW频率")
            return
        self._write_smb(lambda params: self.smb.write(f"FREQ:CW {params.cw_hz:.12g}Hz"), "CW频率设定")
        try:
            self._update_current_frequency_display(self.smb.current_frequency_hz())
        except Exception:
            pass

    def _write_smb(self, writer, action: str) -> None:
        if not self.smb.is_connected:
            return
        try:
            params = self._ui_params()
            writer(params)
            self.log_message("信号源", f"{action}完成")
        except Exception as exc:
            self.log_message("信号源", f"{action}失败: {exc}")

    def apply_parameters(self) -> None:
        if not self.smb.is_connected:
            return
        try:
            params = self._ui_params()
            if self.is_sweeping:
                self.smb.apply_sweep_parameters(params)
            else:
                self.smb.apply_cw_parameters(params)
            self.log_message("信号源", "所有参数已应用")
        except Exception as exc:
            self.log_message("信号源", f"应用参数失败: {exc}")

    def start_sweep(self) -> None:
        if not self.smb.is_connected:
            return
        try:
            cycle_count = int(self.cycle_count_input.text() or 0)
            self.cycle_interval_ms = int(self.cycle_interval_input.text() or 0)
        except ValueError:
            cycle_count = 0
            self.cycle_interval_ms = 0

        desired = self._ui_params()
        try:
            device_params = self.smb.read_parameters()
        except Exception as exc:
            QMessageBox.critical(self, "参数读取失败", f"无法读取设备参数，已取消扫频。\n{exc}")
            self.log_message("信号源", f"扫频前参数读取失败: {exc}")
            return

        differences = compare_parameters(desired, device_params)
        if differences:
            dialog = ParameterReviewDialog(differences, self)
            if dialog.exec_() != QDialog.Accepted or dialog.choice == ParameterReviewDialog.CANCEL:
                self.log_message("信号源", "扫频已取消")
                return
            if dialog.choice == ParameterReviewDialog.SYNC_AND_CANCEL:
                self._set_ui_from_smb_params(device_params)
                self.log_message("信号源", "已同步设备参数到界面，扫频取消")
                return

        self.cyclic_sweep_active = cycle_count > 0
        self.remaining_cycles = cycle_count if cycle_count > 0 else 0
        if self.cyclic_sweep_active:
            self.log_message("信号源", f"启动循环扫频，共 {cycle_count} 次，间隔 {self.cycle_interval_ms} ms")
        self._start_single_sweep_cycle(desired)

    def _start_single_sweep_cycle(self, params: SMBParameters) -> None:
        try:
            self.smb.apply_sweep_parameters(params)
            self.is_sweeping = True
            self.last_freq_value = None
            self.cycle_stop_enabled = True
            self.start_sweep_btn.setEnabled(False)
            self.stop_sweep_btn.setEnabled(True)
            self.smb_update_timer.start(100)
            self.sweep_status_label.setText("扫频运行中")

            if not self.record_data_checkbox.isChecked():
                self.record_data_checkbox.setChecked(True)
            filename = f"sweep_data_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
            self.record_file_input.setText(filename)
            self.recorder.sweep_buffer = []

            if self.cyclic_sweep_active:
                current_cycle = int(self.cycle_count_input.text() or 0) - self.remaining_cycles + 1
                self.log_message("信号源", f"第 {current_cycle} 次扫频开始")
            else:
                self.log_message("信号源", "扫频已开始（将在完成一个完整周期后自动停止）")
        except Exception as exc:
            self.log_message("信号源", f"启动扫频失败: {exc}")
            self._cleanup_after_sweep_stop()

    def stop_sweep(self, manual: bool = False) -> None:
        self.is_sweeping = False
        self.cycle_stop_enabled = False
        self.last_freq_value = None
        try:
            if self.smb.is_connected:
                self.smb.set_frequency_mode_cw()
        except Exception:
            pass
        self.smb_update_timer.stop()
        self._cleanup_after_sweep_stop()

        if self.recorder.sweep_buffer:
            try:
                filename = self.record_file_input.text()
                count = self.recorder.flush_sweep(filename)
                self.log_message("锁相放大器", f"数据已保存至: {filename}，共 {count} 条记录")
            except Exception as exc:
                self.log_message("锁相放大器", f"保存数据失败: {exc}")
        if self.record_data_checkbox.isChecked():
            self.record_data_checkbox.setChecked(False)

        if self.cyclic_sweep_active and not manual:
            self.remaining_cycles -= 1
            self.log_message("信号源", f"已完成一次扫频，剩余 {self.remaining_cycles} 次")
            if self.remaining_cycles > 0:
                self.cycle_timer.start(self.cycle_interval_ms)
            else:
                self.cyclic_sweep_active = False
                self.log_message("信号源", "循环扫频全部完成")
        else:
            if manual:
                self.cyclic_sweep_active = False
                self.remaining_cycles = 0
                self.cycle_timer.stop()
                self.log_message("信号源", "手动停止扫频")
            else:
                self.log_message("信号源", "扫频已停止")

    def _start_next_cycle(self) -> None:
        self.cycle_timer.stop()
        if self.cyclic_sweep_active and self.remaining_cycles > 0:
            self._start_single_sweep_cycle(self._ui_params())

    def _cleanup_after_sweep_stop(self) -> None:
        self.start_sweep_btn.setEnabled(self.smb.is_connected)
        self.stop_sweep_btn.setEnabled(False)
        self.sweep_status_label.setText("扫频已停止")

    def update_smb_frequency_display(self) -> None:
        if not self.smb.is_connected or not self.is_sweeping:
            return
        try:
            freq_val = self.smb.current_frequency_hz()
            self._update_current_frequency_display(freq_val)
            params = self._ui_params()
            start_val = params.start_hz
            stop_val = params.stop_hz
            step_val = params.step_hz
            if self.cycle_stop_enabled and start_val is not None and stop_val is not None and step_val is not None:
                tolerance = max(step_val / 2, 1e3)
                at_start = abs(freq_val - start_val) <= tolerance
                reached_stop_before = self.last_freq_value is not None and abs(self.last_freq_value - stop_val) <= tolerance
                if at_start and reached_stop_before:
                    self.cycle_stop_enabled = False
                    self.log_message("信号源", "已完成一个扫频周期，自动停止")
                    QTimer.singleShot(0, lambda: self.stop_sweep(manual=False))
                else:
                    self.last_freq_value = freq_val
        except Exception:
            pass

    def _update_current_frequency_display(self, hz: float | None) -> None:
        self.smb_freq_display.setText(f"当前频率: {format_frequency(hz) if hz is not None else 'N/A'}")

    def toggle_output(self, state: int) -> None:
        if self.smb.is_connected:
            self._write_smb(lambda _params: self.smb.write(f"OUTP {'ON' if state == Qt.Checked else 'OFF'}"), "RF输出切换")

    def toggle_lf_output(self, state: int) -> None:
        if self.smb.is_connected:
            self._write_smb(lambda _params: self.smb.write(f"SOUR:LFO:STAT {'ON' if state == Qt.Checked else 'OFF'}"), "LF输出切换")

    def toggle_fm_mod(self, state: int) -> None:
        if self.smb.is_connected:
            self._write_smb(lambda _params: self.smb.write(f"SOUR:FM:STAT {'ON' if state == Qt.Checked else 'OFF'}"), "FM调制切换")

    def start_query(self) -> None:
        if not self.lockin.is_connected:
            QMessageBox.warning(self, "警告", "请先连接锁相放大器")
            return
        if self.lockin_worker is not None:
            self.stop_query(reset_count=False)
        try:
            interval = int(self.query_interval_input.text())
        except ValueError:
            interval = 100

        if self.record_data_checkbox.isChecked() and not self.is_sweeping:
            try:
                self.recorder.init_file(self.record_file_input.text())
                self.log_message("锁相放大器", f"记录文件: {self.record_file_input.text()}")
            except Exception as exc:
                self.log_message("锁相放大器", f"初始化记录失败: {exc}")

        command = "SNAPD? 1,0,1,2,3"
        self.lockin_thread = QThread(self)
        self.lockin_worker = LockinWorker(self.lockin, command, interval)
        self.lockin_worker.moveToThread(self.lockin_thread)
        self.lockin_thread.started.connect(self.lockin_worker.run)
        self.lockin_worker.data_received.connect(self.on_lockin_data)
        self.lockin_worker.log_requested.connect(lambda msg: self.log_message("锁相放大器", msg))
        self.lockin_worker.error_occurred.connect(lambda msg: self.log_message("锁相放大器", msg))
        self.lockin_worker.finished.connect(self.on_lockin_finished)
        self.lockin_worker.finished.connect(self.lockin_thread.quit)
        self.lockin_worker.finished.connect(self.lockin_worker.deleteLater)
        self.lockin_thread.finished.connect(self.lockin_thread.deleteLater)
        self.lockin_thread.start()
        self.log_message("锁相放大器", f"开始查询: {command}, 间隔 {interval}ms")
        self.update_lockin_ui_state()

    def stop_query(self, reset_count: bool = True) -> None:
        worker = self.lockin_worker
        thread = self.lockin_thread
        if worker is not None:
            worker.stop()
        if thread is not None and thread.isRunning():
            thread.quit()
            try:
                wait_ms = int(self.query_interval_input.text()) + 2000
            except ValueError:
                wait_ms = 3000
            thread.wait(max(wait_ms, 3000))
        self.lockin_worker = None
        self.lockin_thread = None
        if reset_count:
            self.data_packet_count = 0
            self.data_count_label.setText("0")
            self.log_message("锁相放大器", "停止查询，计数已清零")
        self.update_lockin_ui_state()

    def on_lockin_finished(self, count: int) -> None:
        self.log_message("锁相放大器", f"查询线程结束，共 {count} 次")
        self.lockin_worker = None
        self.lockin_thread = None
        self.update_lockin_ui_state()

    def on_lockin_data(self, sample: LockinSample, count: int) -> None:
        self.x_display.setText(f"{sample.x_mv:.11f}")
        self.y_display.setText(f"{sample.y_mv:.11f}")
        self.r_display.setText(f"{sample.r_mv:.11f}")
        self.theta_display.setText(f"{sample.theta_deg:.11f}")
        self.data_packet_count += 1
        self.data_count_label.setText(str(self.data_packet_count))
        if self.record_data_checkbox.isChecked():
            try:
                if self.is_sweeping:
                    self.recorder.buffer_x(sample.x_mv)
                else:
                    self.recorder.append_x(self.record_file_input.text(), sample.x_mv)
            except Exception as exc:
                self.log_message("锁相放大器", f"记录数据失败: {exc}")

    def browse_capture_dir(self) -> None:
        dirname = QFileDialog.getExistingDirectory(self, "选择采集保存目录", self.capture_dir_input.text())
        if dirname:
            self.capture_dir_input.setText(dirname)

    def start_experiment(self) -> None:
        if not self.smb.is_connected or not self.lockin.is_connected:
            QMessageBox.warning(self, "同步采集", "请先连接 SMB100A 和 OE1022D")
            return
        if self.is_sweeping:
            QMessageBox.warning(self, "同步采集", "请先停止手动扫频")
            return
        if self.lockin_worker is not None:
            self.stop_query(reset_count=False)

        try:
            config = self._experiment_config_from_ui()
        except Exception as exc:
            QMessageBox.warning(self, "同步采集参数错误", str(exc))
            return

        Path(config.output_root).mkdir(parents=True, exist_ok=True)
        self.experiment_progress.setValue(0)
        self.experiment_detail_label.setText("准备启动")
        self.experiment_thread = QThread(self)
        self.experiment_worker = ExperimentWorker(self.smb, self.lockin, config)
        self.experiment_worker.moveToThread(self.experiment_thread)
        self.experiment_thread.started.connect(self.experiment_worker.run)
        self.experiment_worker.progress_changed.connect(self.on_experiment_progress)
        self.experiment_worker.segment_ready.connect(self.on_experiment_segment_ready)
        self.experiment_worker.event_logged.connect(lambda msg: self.log_message("锁相放大器", msg))
        self.experiment_worker.error_occurred.connect(lambda msg: self.log_message("锁相放大器", f"同步采集错误: {msg}"))
        self.experiment_worker.finished.connect(self.on_experiment_finished)
        self.experiment_worker.finished.connect(self.experiment_thread.quit)
        self.experiment_worker.finished.connect(self.experiment_worker.deleteLater)
        self.experiment_thread.finished.connect(self.experiment_thread.deleteLater)
        self.experiment_thread.start()
        self.log_message("信号源", "同步采集已启动")
        self.update_smb_ui_state()
        self.update_lockin_ui_state()
        self.main_tabs.setCurrentIndex(3)

    def _experiment_config_from_ui(self) -> ExperimentConfig:
        params = self._ui_params()
        if params.start_hz is None or params.stop_hz is None or params.step_hz is None:
            raise ValueError("起始频率、终止频率和步进必须有效")
        sample_interval_ms = int(self.lockin_sample_interval_input.text() or "1")
        sample_count = int(self.lockin_sample_count_input.text() or "1000")
        settle_ms = int(self.settle_ms_input.text() or "0")
        if sample_interval_ms < 1:
            raise ValueError("采样间隔至少 1 ms")
        if sample_count < 1 or sample_count > 16384:
            raise ValueError("每点样本数必须在 1..16384 之间")
        if not self.capture_channel_a_checkbox.isChecked() and not self.capture_channel_b_checkbox.isChecked():
            raise ValueError("至少选择 Channel A 或 Channel B")
        return ExperimentConfig(
            experiment_name=self.experiment_name_input.text().strip() or "odmr",
            output_root=self.capture_dir_input.text().strip() or "captures",
            smb_params=params,
            start_hz=params.start_hz,
            stop_hz=params.stop_hz,
            step_hz=params.step_hz,
            settle_ms=settle_ms,
            sample_interval_ms=sample_interval_ms,
            sample_count=sample_count,
            channel_a_enabled=self.capture_channel_a_checkbox.isChecked(),
            channel_b_enabled=self.capture_channel_b_checkbox.isChecked(),
        )

    def stop_experiment(self) -> None:
        if self.experiment_worker is not None:
            self.experiment_detail_label.setText("正在停止...")
            self.log_message("锁相放大器", "请求停止同步采集")
            self.experiment_worker.stop()
        self.update_experiment_ui_state()

    def on_experiment_progress(self, point_index: int, total_points: int, acquired: int, message: str) -> None:
        percent = int(((point_index - 1) / max(total_points, 1)) * 100)
        try:
            target_count = int(self.lockin_sample_count_input.text() or "1")
        except ValueError:
            target_count = 1
        percent += int(min(acquired, target_count) / max(target_count, 1) * (100 / max(total_points, 1)))
        self.experiment_progress.setValue(max(0, min(percent, 100)))
        self.experiment_detail_label.setText(f"{point_index}/{total_points}: {message}")

    def on_experiment_segment_ready(self, payload: object) -> None:
        if not isinstance(payload, dict):
            return
        self.current_plot_payload = payload
        self._update_plots(payload)
        self.main_tabs.setCurrentIndex(4)

    def on_experiment_finished(self, status: str, output_path: str) -> None:
        self.experiment_progress.setValue(100 if status == "completed" else self.experiment_progress.value())
        self.experiment_detail_label.setText(f"采集结束: {status}  {output_path}")
        self.log_message("锁相放大器", f"同步采集结束: {status}, 目录: {output_path}")
        self.experiment_worker = None
        self.experiment_thread = None
        self.update_smb_ui_state()
        self.update_lockin_ui_state()

    def _update_plots(self, payload: dict[str, Any]) -> None:
        if pg is None:
            return
        channels = payload.get("channels", {})
        for channel_name, traces in channels.items():
            plot_info = self.plot_widgets.get(channel_name)
            if not plot_info or not plot_info.get("available"):
                continue
            curves = plot_info["curves"]
            for key, curve in curves.items():
                values = traces.get(key, [])
                curve.setData([math.nan if value is None else value for value in values])

    def browse_record_file(self) -> None:
        filename, _ = QFileDialog.getSaveFileName(self, "保存数据", "", "CSV Files (*.csv)")
        if filename:
            self.record_file_input.setText(filename)

    def refresh_presets(self) -> None:
        current = self.preset_combo.currentText() if hasattr(self, "preset_combo") else ""
        try:
            names = self.presets.names()
        except Exception as exc:
            self.log_message("信号源", f"读取快捷配置失败: {exc}")
            names = []
        self.preset_combo.blockSignals(True)
        self.preset_combo.clear()
        self.preset_combo.addItems(names)
        if current:
            self.preset_combo.setCurrentText(current)
        self.preset_combo.blockSignals(False)

    def collect_preset_payload(self) -> dict[str, Any]:
        return {
            "smb": asdict(self._ui_params()),
            "cycle": {
                "count": self.cycle_count_input.text(),
                "interval_ms": self.cycle_interval_input.text(),
            },
            "lockin": {
                "mode": self.lockin_mode_combo.currentText(),
                "port": self.lockin_port_combo.currentText(),
                "baud": self.lockin_baud_combo.currentText(),
                "usb_port": self.lockin_usb_combo.currentText(),
                "query_interval_ms": self.query_interval_input.text(),
                "record_file": self.record_file_input.text(),
            },
            "experiment": {
                "name": self.experiment_name_input.text(),
                "output_root": self.capture_dir_input.text(),
                "settle_ms": self.settle_ms_input.text(),
                "sample_interval_ms": self.lockin_sample_interval_input.text(),
                "sample_count": self.lockin_sample_count_input.text(),
                "channel_a": self.capture_channel_a_checkbox.isChecked(),
                "channel_b": self.capture_channel_b_checkbox.isChecked(),
            },
        }

    def apply_preset_payload(self, payload: dict[str, Any]) -> None:
        smb_data = payload.get("smb", {})
        self._set_ui_from_smb_params(SMBParameters(**{key: smb_data.get(key) for key in SMBParameters.__dataclass_fields__}))
        cycle = payload.get("cycle", {})
        self.cycle_count_input.setText(str(cycle.get("count", "0")))
        self.cycle_interval_input.setText(str(cycle.get("interval_ms", "200")))
        lockin = payload.get("lockin", {})
        mode = lockin.get("mode", "RS232")
        self.lockin_mode_combo.setCurrentText(mode)
        self.on_lockin_mode_changed(mode)  # 更新 UI 显示
        self.lockin_port_combo.setCurrentText(str(lockin.get("port", self.lockin_port_combo.currentText())))
        self.lockin_baud_combo.setCurrentText(str(lockin.get("baud", self.lockin_baud_combo.currentText())))
        self.lockin_usb_combo.setCurrentText(str(lockin.get("usb_port", self.lockin_usb_combo.currentText())))
        self.query_interval_input.setText(str(lockin.get("query_interval_ms", self.query_interval_input.text())))
        self.record_file_input.setText(str(lockin.get("record_file", self.record_file_input.text())))
        experiment = payload.get("experiment", {})
        self.experiment_name_input.setText(str(experiment.get("name", self.experiment_name_input.text())))
        self.capture_dir_input.setText(str(experiment.get("output_root", self.capture_dir_input.text())))
        self.settle_ms_input.setText(str(experiment.get("settle_ms", self.settle_ms_input.text())))
        self.lockin_sample_interval_input.setText(str(experiment.get("sample_interval_ms", self.lockin_sample_interval_input.text())))
        self.lockin_sample_count_input.setText(str(experiment.get("sample_count", self.lockin_sample_count_input.text())))
        self.capture_channel_a_checkbox.setChecked(bool(experiment.get("channel_a", self.capture_channel_a_checkbox.isChecked())))
        self.capture_channel_b_checkbox.setChecked(bool(experiment.get("channel_b", self.capture_channel_b_checkbox.isChecked())))

    def load_selected_preset(self) -> None:
        name = self.preset_combo.currentText().strip()
        if not name:
            return
        try:
            payload = self.presets.get(name)
            if payload is None:
                QMessageBox.warning(self, "快捷配置", f"未找到配置: {name}")
                return
            self.apply_preset_payload(payload)
            self.log_message("信号源", f"已加载快捷配置: {name}")
        except Exception as exc:
            QMessageBox.critical(self, "快捷配置错误", str(exc))

    def save_selected_preset(self) -> None:
        name = self.preset_combo.currentText().strip()
        if not name:
            self.save_preset_as()
            return
        try:
            self.presets.save(name, self.collect_preset_payload())
            self.refresh_presets()
            self.preset_combo.setCurrentText(name)
            self.log_message("信号源", f"已保存快捷配置: {name}")
        except Exception as exc:
            QMessageBox.critical(self, "保存失败", str(exc))

    def save_preset_as(self) -> None:
        name, ok = QInputDialog.getText(self, "另存为快捷配置", "配置名称:")
        if not ok or not name.strip():
            return
        self.preset_combo.setCurrentText(name.strip())
        self.save_selected_preset()

    def delete_selected_preset(self) -> None:
        name = self.preset_combo.currentText().strip()
        if not name:
            return
        if QMessageBox.question(self, "删除快捷配置", f"确定删除 {name}？") != QMessageBox.Yes:
            return
        try:
            self.presets.delete(name)
            self.refresh_presets()
            self.log_message("信号源", f"已删除快捷配置: {name}")
        except Exception as exc:
            QMessageBox.critical(self, "删除失败", str(exc))

    def closeEvent(self, event) -> None:
        reply = QMessageBox.question(self, "退出", "确定退出？", QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if reply == QMessageBox.Yes:
            if self.experiment_worker is not None:
                self.stop_experiment()
                if self.experiment_thread is not None and self.experiment_thread.isRunning():
                    self.experiment_thread.quit()
                    self.experiment_thread.wait(3000)
            if self.smb.is_connected:
                self.disconnect_smb()
            if self.lockin.is_connected:
                self.disconnect_lockin()
            event.accept()
        else:
            event.ignore()


def compare_parameters(desired: SMBParameters, device: SMBParameters) -> list[tuple[str, str, str, str]]:
    desired_values = desired.as_comparable()
    device_values = device.as_comparable()
    differences: list[tuple[str, str, str, str]] = []
    for key, label in FIELD_LABELS.items():
        desired_value = desired_values.get(key)
        device_value = device_values.get(key)
        if desired_value is None:
            continue
        if device_value is None:
            differences.append((label, display_param_value(key, desired_value), "读取失败", "无法校对"))
            continue
        if not values_match(key, desired_value, device_value):
            differences.append(
                (
                    label,
                    display_param_value(key, desired_value),
                    display_param_value(key, device_value),
                    "不一致",
                )
            )
    return differences


def values_match(key: str, left: Any, right: Any) -> bool:
    if isinstance(left, bool) or isinstance(right, bool):
        return left is right
    if key == "lf_shape":
        return str(left).upper() == str(right).upper()
    tolerance = 0.01
    if key.endswith("_hz"):
        tolerance = max(abs(float(left)) * 1e-9, 1.0)
    elif key == "dwell_ms":
        tolerance = 0.1
    elif key == "lf_amp_mv":
        tolerance = 0.1
    return abs(float(left) - float(right)) <= tolerance


def display_param_value(key: str, value: Any) -> str:
    if isinstance(value, bool):
        return "ON" if value else "OFF"
    if key.endswith("_hz") and isinstance(value, (int, float)):
        return format_frequency(float(value))
    if key == "dwell_ms" and isinstance(value, (int, float)):
        return format_float(float(value), " ms")
    if key == "lf_amp_mv" and isinstance(value, (int, float)):
        return format_float(float(value), " mV")
    if key == "power_dbm" and isinstance(value, (int, float)):
        return format_float(float(value), " dBm")
    return str(value)
