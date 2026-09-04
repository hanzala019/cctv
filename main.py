"""
Entry point for the CCTV viewer.

Deliberately thin. Everything that used to live in the old 1,805-line
main.py now sits under core/ui/ -- this file only does the three things
that genuinely have to happen before any of it can run:

    1. Set OpenCV's FFmpeg options. This MUST happen before cv2 is
       imported anywhere, which is why it's here and not in
       capture/video_stream.py.
    2. Construct the QApplication and apply the palette.
    3. Show the main window.

Run with:  python main.py       (from the project root)
"""

import os

# Before any cv2 import, anywhere in the import graph.
os.environ.setdefault(
    "OPENCV_FFMPEG_CAPTURE_OPTIONS",
    "rtsp_transport;tcp|fflags;nobuffer|flags;low_delay|max_delay;500000",
)

import sys  # noqa: E402

from PyQt6.QtGui import QColor, QPalette  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

from core.ui.app import MainWindow  # noqa: E402
from core.ui.theme import (  # noqa: E402
    COLOR_ACCENT,
    COLOR_BG,
    COLOR_CAPTION_BG,
    COLOR_PANEL_BG,
    COLOR_TEXT_PRIMARY,
)


def _apply_dark_palette(app):
    """Belt-and-suspenders dark theme: sets the app-wide QPalette in
    addition to per-widget stylesheets, so native dialogs (file
    pickers, message boxes) and any unstyled widget still look
    consistent rather than flashing white."""
    palette = QPalette()
    palette.setColor(QPalette.ColorRole.Window, QColor(COLOR_BG))
    palette.setColor(QPalette.ColorRole.WindowText, QColor(COLOR_TEXT_PRIMARY))
    palette.setColor(QPalette.ColorRole.Base, QColor(COLOR_CAPTION_BG))
    palette.setColor(QPalette.ColorRole.AlternateBase, QColor(COLOR_PANEL_BG))
    palette.setColor(QPalette.ColorRole.Text, QColor(COLOR_TEXT_PRIMARY))
    palette.setColor(QPalette.ColorRole.Button, QColor(COLOR_PANEL_BG))
    palette.setColor(QPalette.ColorRole.ButtonText, QColor(COLOR_TEXT_PRIMARY))
    palette.setColor(QPalette.ColorRole.Highlight, QColor(COLOR_ACCENT))
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor("white"))
    app.setPalette(palette)


def main():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    _apply_dark_palette(app)

    window = MainWindow()
    window.show()


    sys.exit(app.exec())


if __name__ == "__main__":
    main()
