from __future__ import annotations

import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PIL import Image
from PyQt5.QtCore import QTimer
from PyQt5.QtWidgets import QApplication, QWidget

from clonef.screenshots import ScreenCapture


def main():
    app = QApplication([])
    window = QWidget()
    window.setGeometry(10, 10, 100, 100)
    window.setStyleSheet("background-color: rgb(10, 150, 80)")
    window.show()
    capture = ScreenCapture()
    result = []

    def received(data):
        with Image.open(io.BytesIO(data)) as image:
            color = image.convert("RGB").getpixel((50, 50))
            result.append(color == (10, 150, 80))
        app.quit()

    def failed(message):
        print(message, file=sys.stderr)
        result.append(False)
        app.quit()

    capture.captured.connect(received)
    capture.failed.connect(failed)
    QTimer.singleShot(300, capture.capture)
    QTimer.singleShot(10000, app.quit)
    app.exec_()
    window.close()
    if result != [True]:
        print("X11 screenshot check failed", file=sys.stderr)
        return 1
    print("Qt/X11 window and screenshot OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
