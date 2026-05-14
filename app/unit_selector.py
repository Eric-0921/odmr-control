"""单位选择器组件

提供带单位选择的输入框，用于频率、磁场、功率、时间等物理量。
遵循 Siemens 工业风设计，与 GUI 现有样式保持一致。

用法示例::

    from app.unit_selector import FrequencyUnitSelector, PowerUnitSelector

    # 频率输入
    freq = FrequencyUnitSelector(2.87e9, parent=self)  # 默认 GHz
    freq.value_changed.connect(self.on_freq_changed)

    # 功率输入（dBm / mW 互转）
    pwr = PowerUnitSelector(-10.0, parent=self)  # 默认 dBm
    pwr.value_changed.connect(self.on_power_changed)
"""

from __future__ import annotations

import math
from typing import Dict, Optional

from PyQt5.QtCore import pyqtSignal
from PyQt5.QtGui import QDoubleValidator
from PyQt5.QtWidgets import QHBoxLayout, QLineEdit, QComboBox, QSizePolicy, QWidget


# ---------------------------------------------------------------------------
# 单位转换规则
# ---------------------------------------------------------------------------

UNIT_CONVERSIONS: Dict[str, Dict[str, float]] = {
    "frequency": {
        "Hz": 1.0,
        "kHz": 1e3,
        "MHz": 1e6,
        "GHz": 1e9,
    },
    "magnetic_field": {
        "nT": 1.0,
        "μT": 1e3,
        "mT": 1e6,
        "T": 1e9,
        "G": 1e5,    # 1 G = 100,000 nT
        "Oe": 1e5,   # 1 Oe = 100,000 nT (in vacuum)
    },
    "time": {
        "us": 1e-6,
        "ms": 1e-3,
        "s": 1.0,
        "min": 60.0,
        "h": 3600.0,
    },
}


# ---------------------------------------------------------------------------
# dBm <-> mW 转换（对数单位特殊处理）
# ---------------------------------------------------------------------------

def dbm_to_mw(dbm: float) -> float:
    """dBm 转 mW: P_mW = 10^(P_dBm / 10)"""
    return 10 ** (dbm / 10)


def mw_to_dbm(mw: float) -> float:
    """mW 转 dBm: P_dBm = 10 * log10(P_mW)"""
    if mw <= 0:
        return float("-inf")
    return 10 * math.log10(mw)


# ---------------------------------------------------------------------------
# 基础组件
# ---------------------------------------------------------------------------

class UnitSelector(QWidget):
    """带单位选择的输入框

    内部维护一个"基础单位"值（如 Hz、s、dBm），
    用户在 UI 上看到的值经过当前单位系数转换。
    """

    value_changed = pyqtSignal(float)  # 发射基础单位的值

    def __init__(
        self,
        value: float,
        units: Dict[str, float],
        default_unit: str,
        validator=None,
        parent=None,
    ):
        """
        Args:
            value: 初始值（基础单位）
            units: 单位字典 {单位名: 转换系数}，
                   如 {"Hz": 1.0, "kHz": 1e3, "MHz": 1e6}
            default_unit: 默认单位（必须是 units 中的键）
            validator: 可选的 QValidator 实例
            parent: 父组件
        """
        super().__init__(parent)

        self._units = dict(units)
        if default_unit not in self._units:
            raise ValueError(
                f"default_unit '{default_unit}' not in units: {list(units.keys())}"
            )
        self._default_unit = default_unit
        self._base_value = value

        # --- UI ---
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        self._edit = QLineEdit()
        self._edit.setMinimumWidth(100)
        self._edit.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        if validator is not None:
            self._edit.setValidator(validator)
        else:
            self._edit.setValidator(QDoubleValidator())
        layout.addWidget(self._edit, 1)

        self._combo = QComboBox()
        self._combo.addItems(list(self._units.keys()))
        self._combo.setCurrentText(default_unit)
        self._combo.setMinimumWidth(72)
        self._combo.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        layout.addWidget(self._combo)

        # 初始化显示值
        self._update_display()

        # 信号连接
        self._edit.editingFinished.connect(self._on_editing_finished)
        self._combo.currentTextChanged.connect(self._on_unit_changed)

    # --- 公共 API ---------------------------------------------------------

    def value_in_base_unit(self) -> float:
        """返回基础单位的值"""
        return self._base_value

    def set_value_in_base_unit(self, value: float):
        """设置基础单位的值，自动转换单位显示"""
        self._base_value = value
        self._update_display()

    def current_unit(self) -> str:
        """返回当前单位名"""
        return self._combo.currentText()

    def set_unit(self, unit: str):
        """设置当前单位"""
        if unit not in self._units:
            raise ValueError(f"Unknown unit '{unit}', expected one of {list(self._units.keys())}")
        self._combo.setCurrentText(unit)

    # --- 内部方法 ---------------------------------------------------------

    def _update_display(self):
        """根据当前单位系数更新输入框显示"""
        unit = self._combo.currentText()
        factor = self._units[unit]
        display_val = self._base_value / factor
        self._edit.setText(self._format_display(display_val))

    def _format_display(self, val: float) -> str:
        """格式化显示值（默认保留 6 位有效数字）"""
        if val == 0:
            return "0"
        abs_val = abs(val)
        if abs_val >= 1e6 or abs_val < 1e-3:
            return f"{val:.6g}"
        return f"{val:.6f}".rstrip("0").rstrip(".")

    def _on_editing_finished(self):
        """输入框编辑完成时，计算基础单位值并发射信号"""
        text = self._edit.text().strip()
        if not text:
            return
        try:
            display_val = float(text)
        except ValueError:
            return
        unit = self._combo.currentText()
        factor = self._units[unit]
        new_base = display_val * factor
        if new_base != self._base_value:
            self._base_value = new_base
            self.value_changed.emit(self._base_value)

    def _on_unit_changed(self, _unit: str):
        """单位切换时刷新显示"""
        self._update_display()


# ---------------------------------------------------------------------------
# 预设子类
# ---------------------------------------------------------------------------

class FrequencyUnitSelector(UnitSelector):
    """频率单位选择器: Hz / kHz / MHz / GHz"""

    def __init__(self, value: float = 0.0, validator=None, parent=None):
        super().__init__(
            value=value,
            units=UNIT_CONVERSIONS["frequency"],
            default_unit="GHz",
            validator=validator,
            parent=parent,
        )


class MagneticFieldUnitSelector(UnitSelector):
    """磁场单位选择器: nT / uT / mT / T / G / Oe"""

    def __init__(self, value: float = 0.0, default_unit: str = "nT", validator=None, parent=None):
        super().__init__(
            value=value,
            units=UNIT_CONVERSIONS["magnetic_field"],
            default_unit=default_unit,
            validator=validator,
            parent=parent,
        )


class TimeUnitSelector(UnitSelector):
    """时间单位选择器: us / ms / s / min / h"""

    def __init__(self, value: float = 0.0, validator=None, parent=None):
        super().__init__(
            value=value,
            units=UNIT_CONVERSIONS["time"],
            default_unit="s",
            validator=validator,
            parent=parent,
        )


class PowerUnitSelector(UnitSelector):
    """功率单位选择器: dBm / mW

    dBm 和 mW 之间是对数关系，不能用简单的乘法系数转换。
    此类覆写内部转换逻辑，使用 dbm_to_mw / mw_to_dbm 进行互转。
    内部基础单位始终是 **dBm**。
    """

    # 功率单位不走乘法系数，用 1.0 作为占位符，实际转换在覆写方法中完成
    _POWER_UNITS: Dict[str, float] = {
        "dBm": 1.0,
        "mW": 1.0,
    }

    def __init__(self, value: float = 0.0, validator=None, parent=None):
        super().__init__(
            value=value,
            units=self._POWER_UNITS,
            default_unit="dBm",
            validator=validator,
            parent=parent,
        )

    def _update_display(self):
        """覆写：dBm <-> mW 对数转换"""
        unit = self._combo.currentText()
        if unit == "mW":
            display_val = dbm_to_mw(self._base_value)
        else:
            display_val = self._base_value
        self._edit.setText(self._format_display(display_val))

    def _on_editing_finished(self):
        """覆写：dBm <-> mW 对数转换"""
        text = self._edit.text().strip()
        if not text:
            return
        try:
            display_val = float(text)
        except ValueError:
            return
        unit = self._combo.currentText()
        if unit == "mW":
            new_base = mw_to_dbm(display_val)
        else:
            new_base = display_val
        if new_base != self._base_value:
            self._base_value = new_base
            self.value_changed.emit(self._base_value)

    def _format_display(self, val: float) -> str:
        """功率值格式化（对数/线性统一）"""
        if val == 0:
            return "0"
        abs_val = abs(val)
        if abs_val >= 1e4 or (abs_val < 1e-3 and abs_val != 0):
            return f"{val:.4g}"
        return f"{val:.4f}".rstrip("0").rstrip(".")
