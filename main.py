"""ODMR Control - 入口点

遵循 mag-field-control-v2 的架构风格：
- PyQt5 + Fusion style
- 多层安全策略
- 双模式数据采集（SNAPD? 监控 + RALL? 采集）
"""

import sys
from pathlib import Path

from PyQt5.QtWidgets import QApplication

# 确保项目根目录在路径中
PROJECT_ROOT = Path(__file__).parent.resolve()
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.config_io import load_config
from app.gui import ODMRControlGUI
from core.command_service import CommandService
from core.instrument_controller import InstrumentController


def main() -> None:
    app = QApplication(sys.argv)
    app.setStyle("Fusion")

    # 初始化核心层
    cfg = load_config()
    controller = InstrumentController()
    cmd_service = CommandService(controller, config=cfg)
    cmd_service.start()

    # 初始化 GUI（传入 CommandService，后续 Part 逐步迁移调用）
    window = ODMRControlGUI(cmd_service=cmd_service)
    window.show()

    # 应用退出时停止 CommandService
    app.aboutToQuit.connect(cmd_service.stop)

    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
