import sys

from PyQt5.QtWidgets import QApplication

from app.gui import SMB100AControlGUI


def main() -> None:s
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    window = SMB100AControlGUI()
    window.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()

