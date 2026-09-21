"""Render the real owned Qt interface, with hosting disabled."""
import os
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtWidgets import QApplication
from elink.ui.theme import apply_theme
from elink.ui.window import MainWindow


def main():
    root = Path(".artifacts/independent-ui").resolve()
    root.mkdir(parents=True, exist_ok=True)
    app = QApplication([])
    apply_theme(app)
    window = MainWindow(root, auto_refresh=False)
    window.show()
    for width, height in ((1120, 800), (900, 680)):
        window.resize(width, height)
        for index, name in enumerate(("remote", "host", "network", "about")):
            window.tabs.setCurrentIndex(index)
            app.processEvents()
            window.grab().save(str(root / f"{name}-{width}.png"))
    window.close()
    deadline = time.monotonic() + 10
    while not window._closed and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)
    assert window._closed
    print(root)


if __name__ == "__main__":
    main()
