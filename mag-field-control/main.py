import sys

from PyQt5.QtWidgets import QApplication

from app.gui import MagnetFieldControlGUI


def main() -> None:
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    window = MagnetFieldControlGUI()
    window.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
