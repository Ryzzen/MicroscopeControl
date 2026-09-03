"""Application entry point."""

from __future__ import annotations

import signal
import sys

from PySide6.QtCore import QTimer
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication

from .ui.main_window import MainWindow


def _apply_dark_palette(app: QApplication) -> None:
    """Dark UI, because the app is used in a darkened room.

    Not a style preference: darkfield and fluorescence work happens with the
    room lights down, and a bright chrome around the live view both wrecks dark
    adaptation and shifts how you judge contrast in the image next to it.
    Fusion is forced so the palette applies regardless of the desktop theme.
    """
    app.setStyle("Fusion")
    palette = QPalette()
    window = QColor(32, 33, 36)
    base = QColor(24, 25, 28)
    text = QColor(225, 226, 230)
    highlight = QColor(64, 132, 214)

    palette.setColor(QPalette.ColorRole.Window, window)
    palette.setColor(QPalette.ColorRole.WindowText, text)
    palette.setColor(QPalette.ColorRole.Base, base)
    palette.setColor(QPalette.ColorRole.AlternateBase, window)
    palette.setColor(QPalette.ColorRole.Text, text)
    palette.setColor(QPalette.ColorRole.Button, window)
    palette.setColor(QPalette.ColorRole.ButtonText, text)
    palette.setColor(QPalette.ColorRole.ToolTipBase, base)
    palette.setColor(QPalette.ColorRole.ToolTipText, text)
    palette.setColor(QPalette.ColorRole.Highlight, highlight)
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor(255, 255, 255))
    palette.setColor(
        QPalette.ColorGroup.Disabled,
        QPalette.ColorRole.Text,
        QColor(120, 121, 126),
    )
    palette.setColor(
        QPalette.ColorGroup.Disabled,
        QPalette.ColorRole.ButtonText,
        QColor(120, 121, 126),
    )
    app.setPalette(palette)


# Minimum comfortable touch target. Qt's desktop defaults assume a mouse, and
# on the tablet the stock ~22px controls are genuinely hard to hit with a
# fingertip -- which is the only pointing device available at the bench, since
# the on-screen keyboard already covers half the display.
_TOUCH_TARGET_PX = 40


def _apply_touch_metrics(app: QApplication) -> None:
    """Enlarge interactive controls to finger size.

    Applied unconditionally rather than behind a "tablet mode" flag: the larger
    targets cost nothing with a mouse, and a setting that has to be discovered
    before the UI becomes usable is a setting in the wrong place.
    """
    app.setStyleSheet(
        f"""
        QPushButton, QComboBox, QLineEdit, QToolButton {{
            min-height: {_TOUCH_TARGET_PX}px;
            padding: 2px 10px;
        }}
        QCheckBox {{ spacing: 10px; min-height: {_TOUCH_TARGET_PX - 8}px; }}
        QCheckBox::indicator {{ width: 22px; height: 22px; }}
        /* Sliders get a deliberately oversized handle: the ROI and sensitivity
           sliders are the two controls most likely to be dragged rather than
           tapped, and a thin handle is unusable without a stylus. */
        QSlider::groove:horizontal {{ height: 8px; border-radius: 4px;
                                      background: #3a3b40; }}
        QSlider::handle:horizontal {{ width: 26px; height: 26px;
                                      margin: -10px 0; border-radius: 13px;
                                      background: #6a9fe0; }}
        QComboBox QAbstractItemView::item {{ min-height: {_TOUCH_TARGET_PX}px; }}
        QGroupBox {{ margin-top: 8px; padding-top: 6px; }}
        """
    )


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("MicroscopeControl")
    app.setOrganizationName("MicroscopeControl")
    _apply_dark_palette(app)
    _apply_touch_metrics(app)

    window = MainWindow()
    window.show()

    # Shut the camera down on Ctrl-C and on SIGTERM, rather than dying where we
    # stand. This matters more than it looks: killed mid-session the body is
    # left with live view armed and the mirror up, and its PTP stack can wedge
    # so thoroughly that it stops answering entirely -- every subsequent
    # connect, from this app or from gphoto2, fails with a PTP timeout until
    # the camera is physically power-cycled. Closing the window runs the normal
    # teardown, which drops the mirror and ends the session properly.
    #
    # SIG_DFL was the previous behaviour and was exactly wrong: it made Ctrl-C
    # the fastest way to wedge the hardware.
    def _graceful_exit(_signum, _frame) -> None:
        window.close()
        app.quit()

    signal.signal(signal.SIGINT, _graceful_exit)
    signal.signal(signal.SIGTERM, _graceful_exit)
    # Qt's event loop runs no Python bytecode while idle, so a signal handler
    # would not execute until the next event arrives. The idle timer guarantees
    # the interpreter gets control several times a second.
    heartbeat = QTimer()
    heartbeat.start(250)
    heartbeat.timeout.connect(lambda: None)

    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
