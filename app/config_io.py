"""配置读写模块

兼容 XML 格式（类似 mag-field-control-v2 的 para.xml），
同时支持 JSON 作为更现代的替代格式。
"""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Dict

DEFAULT_CONFIG_PATH = Path(__file__).parent.parent / "config.xml"

DEFAULT_CONFIG: Dict[str, Any] = {
    "smb": {
        "visa_address": "USB0::0x0AAD::0x0054::106789::INSTR",
        "timeout_ms": 10000,
        "amplifier_installed": True,
    },
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
    },
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
        "max_power_mw": 150,
        "power_stability_pct": 0.117,
        "warmup_s": 600,
        "power_mode": "APC",
    },
    "magnetic_field": {
        "baudrate": 9600,
        "poll_interval_ms": 500,
        "bindings": {
            "X": {"idn": "", "port": "COM1"},
            "Y": {"idn": "", "port": "COM2"},
            "Z": {"idn": "", "port": "COM3"},
        },
        "coil_constant": {"X": 143.26, "Y": 141.77, "Z": 156.15},
        "zero_offset": {"X": 0.0, "Y": 0.0, "Z": 0.0},
        "axes": {
            "X": {"port": "COM1", "coil_constant": 143.26, "zero_offset_mA": 0.0},
            "Y": {"port": "COM2", "coil_constant": 141.77, "zero_offset_mA": 0.0},
            "Z": {"port": "COM3", "coil_constant": 156.15, "zero_offset_mA": 0.0},
        },
        "sequence": {
            "settle_seconds": 0.5,
            "return_to_zero": True,
        },
    },
    "lockin_bindings": {},  # {idn_string: com_port}
    "smb_bindings": {},  # {serial_number: visa_address}
    "sweep": {
        "power_dbm": -30.0,
        "start_freq_hz": 2.82e9,
        "stop_freq_hz": 2.92e9,
        "step_hz": 500e3,
        "dwell_ms": 500,
        "lf_freq_hz": 500.0,
        "lf_amp_mv": 137.0,
        "lf_shape": "SQUARE",
        "fm_dev_hz": 4e6,
        "cw_freq_hz": 2.82e9,
    },
    "acquisition": {
        "monitor_interval_ms": 94,
        "rall_batch_interval_ms": 50,
        "save_dir": "./experiments",
        "auto_save": True,
    },
    "ui": {
        "poll_interval_ms": 100,
        "window_width": 1200,
        "window_height": 800,
    },
    "automation": {
        "default_safety_policy": {
            "on_error": "safe_outputs_off",
            "stop_recording": True,
            "smb_output_off": True,
            "magnetic_output_off": True,
            "laser_output_off": False,
        }
    },
}


def _xml_to_dict(element: ET.Element) -> Any:
    """递归将 XML Element 转为 dict / list / str。"""
    children = list(element)
    if not children:
        text = element.text
        if text is None:
            return ""
        text = text.strip()
        # 尝试数值转换
        for converter in (int, float):
            try:
                return converter(text)
            except ValueError:
                pass
        if text.lower() in ("true", "on", "1"):
            return True
        if text.lower() in ("false", "off", "0"):
            return False
        return text

    result: Dict[str, Any] = {}
    for child in children:
        tag = child.tag
        child_data = _xml_to_dict(child)
        if tag in result:
            if not isinstance(result[tag], list):
                result[tag] = [result[tag]]
            result[tag].append(child_data)
        else:
            # 如果该 tag 有多个同级元素，也转成 list
            same_tags = [c for c in children if c.tag == tag]
            if len(same_tags) > 1:
                result.setdefault(tag, []).append(child_data)
            else:
                result[tag] = child_data
    return result


def _dict_to_xml(parent: ET.Element, data: Any, tag: str = "item") -> None:
    """递归将 dict / list 写入 XML Element。"""
    if isinstance(data, dict):
        for key, val in data.items():
            child = ET.SubElement(parent, key)
            _dict_to_xml(child, val, key)
    elif isinstance(data, list):
        for item in data:
            child = ET.SubElement(parent, tag)
            _dict_to_xml(child, item, tag)
    else:
        parent.text = str(data)


def load_config(path: Path | None = None) -> Dict[str, Any]:
    """加载配置，缺失字段使用默认值填充。"""
    path = path or DEFAULT_CONFIG_PATH
    cfg = DEFAULT_CONFIG.copy()
    if not path.exists():
        return cfg

    try:
        if path.suffix.lower() == ".json":
            with open(path, "r", encoding="utf-8") as f:
                loaded = json.load(f)
        else:
            tree = ET.parse(path)
            loaded = _xml_to_dict(tree.getroot())
    except Exception:
        return cfg

    if not isinstance(loaded, dict):
        return cfg

    # 深度合并，保留默认值
    def _merge(base: Dict, override: Dict) -> Dict:
        for key, val in override.items():
            if key in base and isinstance(base[key], dict) and isinstance(val, dict):
                _merge(base[key], val)
            else:
                base[key] = val
        return base

    return _merge(cfg, loaded)


def save_config(cfg: Dict[str, Any], path: Path | None = None) -> None:
    """保存配置到 XML（默认）或 JSON。"""
    path = path or DEFAULT_CONFIG_PATH
    path.parent.mkdir(parents=True, exist_ok=True)

    if path.suffix.lower() == ".json":
        with open(path, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    else:
        root = ET.Element("Root")
        _dict_to_xml(root, cfg, "item")
        tree = ET.ElementTree(root)
        ET.indent(tree, space="  ")
        tree.write(path, encoding="utf-8", xml_declaration=True)
