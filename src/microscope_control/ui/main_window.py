"""Main window: live view centre, device panels in a right-hand dock.

The layout anticipates the rig rather than the current milestone. Devices go in
the dock as independent panels, each bound to its own controller and knowing
nothing about the others, so adding the Marlin stage, the motorised nosepiece
and the LED driver is a matter of dropping panels into the same column. The
live view stays central and unshared, because every one of those devices exists
to change what appears in it.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (
    QDockWidget,
    QHBoxLayout,
    QPushButton,
    QScroller,
    QGroupBox,
    QLabel,
    QMainWindow,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from ..devices.base import DeviceState
from ..devices.camera import CameraController
from .compact import allow_shrink
from .liveview import LiveView
from .panels.camera_panel import CameraPanel
from .panels.focus_panel import FocusPanel


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("MicroscopeControl")
        self.resize(1280, 820)

        self._camera = CameraController()

        # Keep trying to open the camera whenever it is not open, unless the
        # user deliberately disconnected. A tethered body is not a stable
        # peripheral: this one drops off the bus on its own idle timer, and gets
        # unplugged and power-cycled constantly at the bench. A single attempt
        # at launch means the app is wrong about the hardware for as long as it
        # runs, and the only cue is a stale error message.
        self._auto_reconnect = True
        self._reported_failure = ""
        self._reconnect = QTimer(self)
        self._reconnect.setInterval(3000)
        self._reconnect.timeout.connect(self._try_connect)

        self._view = LiveView()
        self._pending_capture = None
        central = QWidget()
        central_layout = QVBoxLayout(central)
        central_layout.setContentsMargins(0, 0, 0, 0)
        central_layout.setSpacing(0)
        central_layout.addWidget(self._view, 1)
        central_layout.addWidget(self._build_review_bar())
        self.setCentralWidget(central)

        self._camera_panel = CameraPanel()
        self._focus_panel = FocusPanel()
        self._dock = self._build_dock()
        self._build_toolbar()

        self._status = QLabel("Camera disconnected")
        self.statusBar().addWidget(self._status)
        self._zoom_status = QLabel("1.0x")
        self.statusBar().addPermanentWidget(self._zoom_status)

        self._wire_camera()
        self._wire_view()

        # Push the panel's defaults down to the worker so the two agree from
        # the first frame rather than from the first user interaction.
        self._camera.update_focus(roi_fraction=self._focus_panel.roi_fraction)
        self._view.set_roi_fraction(self._focus_panel.roi_fraction)

        # Connect and start streaming without being asked. The app has exactly
        # one job on launch -- show what is under the objective -- and making
        # that wait behind two button presses is pure friction at a bench where
        # you may not have a keyboard to hand. Deferred rather than called
        # inline so the window is painted first: opening the PTP session takes
        # a few hundred milliseconds, and doing it before the first paint looks
        # like a failed start.
        QTimer.singleShot(150, self._try_connect)
        self._reconnect.start()

    # -- construction -----------------------------------------------------

    def _build_review_bar(self) -> QWidget:
        """Keep-or-discard bar, shown only while a capture is under review.

        A bar under the image rather than a modal dialog: the decision needs a
        proper look at the frame -- zoomed in, panned around -- and a dialog
        would either cover the image or shrink it to a thumbnail. This way the
        capture occupies the same viewport the live view did, with the existing
        zoom and pan still working on it.
        """
        self._review_bar = QWidget()
        row = QHBoxLayout(self._review_bar)
        row.setContentsMargins(8, 6, 8, 6)

        self._review_label = QLabel("")
        self._review_label.setWordWrap(True)
        row.addWidget(self._review_label, 1)

        # Verdict on the frame that was actually recorded. Deliberately loud:
        # this is the check that catches an exposure the live view lied about,
        # and it has to be readable at a glance before Keep is pressed.
        self._review_verdict = QLabel("")
        self._review_verdict.setWordWrap(True)
        row.addWidget(self._review_verdict, 1)

        keep = QPushButton("Keep")
        keep.setToolTip("Keep this frame and go back to live view")
        keep.clicked.connect(self._keep_capture)
        discard = QPushButton("Delete")
        discard.setToolTip("Delete this frame from disk and from the camera card")
        discard.clicked.connect(self._discard_capture)
        # Red, because it destroys two files and there is no undo.
        discard.setStyleSheet("background: #7a3434;")
        row.addWidget(keep)
        row.addWidget(discard)

        self._review_bar.hide()
        return self._review_bar

    def _build_dock(self) -> QDockWidget:
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(8, 8, 8, 8)
        for panel in (self._camera_panel, self._focus_panel, self._planned_devices()):
            allow_shrink(panel, minimum=140)
            layout.addWidget(panel)
        layout.addStretch(1)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(container)
        # Deliberately small. The dock cannot be dragged narrower than its
        # contents demand, so every widget inside that refuses to shrink --
        # a combo sized to its longest entry, a label sized to its full text --
        # becomes a floor on the whole panel. At 300 it was claiming half the
        # window on the tablet and squeezing the live view into a strip.
        # The floor has to clear what the panel's contents still need after
        # they have shrunk as far as they will go (~156 px measured), plus the
        # vertical scrollbar. Set it lower and the column clips instead --
        # which, with horizontal scrolling off, means controls simply lose
        # their right-hand edge with nothing to reveal them.
        scroll.setMinimumWidth(190)
        # Never horizontally. The panel is a column of controls; a sideways
        # scrollbar there hides settings behind a gesture and steals a row of
        # height. Everything inside is built to shrink instead (see
        # ui/compact.py), so the column narrows with the dock rather than
        # sliding under a viewport.
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        # Kinetic drag-scrolling. Without this the panel can only be scrolled by
        # its scrollbar, which is a ~10px target -- unusable with a fingertip,
        # and the reason the focus controls were unreachable on the tablet.
        QScroller.grabGesture(
            scroll.viewport(), QScroller.ScrollerGestureType.TouchGesture
        )

        dock = QDockWidget("Devices", self)
        dock.setWidget(scroll)
        dock.setAllowedAreas(
            Qt.DockWidgetArea.LeftDockWidgetArea | Qt.DockWidgetArea.RightDockWidgetArea
        )
        dock.setFeatures(
            QDockWidget.DockWidgetFeature.DockWidgetMovable
            | QDockWidget.DockWidgetFeature.DockWidgetFloatable
            | QDockWidget.DockWidgetFeature.DockWidgetClosable
        )
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, dock)
        # Preferred opening width, not a constraint -- the user can drag it
        # anywhere between the minimum above and the window width.
        self.resizeDocks([dock], [330], Qt.Orientation.Horizontal)
        return dock

    def _planned_devices(self) -> QGroupBox:
        """Placeholder marking where the rest of the rig plugs in."""
        box = QGroupBox("Stage / Illumination")
        box.setEnabled(False)
        layout = QVBoxLayout(box)
        label = QLabel(
            "Not connected. Marlin XYZ, motorised nosepiece and LED control "
            "attach here as separate device panels."
        )
        label.setWordWrap(True)
        layout.addWidget(label)
        return box

    def _build_toolbar(self) -> None:
        toolbar = self.addToolBar("View")
        toolbar.setMovable(False)

        zoom_in = QAction("Zoom in", self)
        zoom_in.setShortcut(QKeySequence.StandardKey.ZoomIn)
        zoom_in.triggered.connect(self._view.zoom_in)

        zoom_out = QAction("Zoom out", self)
        zoom_out.setShortcut(QKeySequence.StandardKey.ZoomOut)
        zoom_out.triggered.connect(self._view.zoom_out)

        reset = QAction("Fit", self)
        reset.setShortcut("Ctrl+0")
        reset.triggered.connect(self._view.reset_view)

        for action in (zoom_in, zoom_out, reset):
            toolbar.addAction(action)
            self.addAction(action)

        toolbar.addSeparator()
        # Hiding the panel hands the whole window to the live view, which is
        # what you want once the camera is set up and you are just focusing --
        # especially on the tablet, where the on-screen keyboard already takes
        # half the display. Qt's own toggle action keeps the checked state in
        # step with the dock's close button for free.
        toggle = self._dock.toggleViewAction()
        toggle.setText("Devices")
        toggle.setShortcut("Ctrl+D")
        toggle.setToolTip("Show or hide the device panel (Ctrl+D)")
        toolbar.addAction(toggle)
        self.addAction(toggle)

    # -- wiring -----------------------------------------------------------

    def _wire_camera(self) -> None:
        self._camera.stateChanged.connect(self._on_state)
        self._camera.previewFrame.connect(self._on_frame)
        self._camera.settingsRead.connect(self._camera_panel.set_settings)
        self._camera.captureComplete.connect(self._on_capture)
        self._camera.captureDiscarded.connect(self._on_discarded)
        self._camera.error.connect(self._on_error)
        self._camera.previewStopped.connect(self._on_preview_stopped)

        self._camera_panel.connectRequested.connect(self._on_connect_clicked)
        self._camera_panel.disconnectRequested.connect(self._on_disconnect_clicked)
        self._camera_panel.previewStartRequested.connect(self._start_preview)
        self._camera_panel.previewStopRequested.connect(self._camera.stop_preview)
        self._camera_panel.settingChanged.connect(self._camera.set_setting)
        self._camera_panel.captureRequested.connect(self._camera.capture)
        self._camera_panel.targetFpsChanged.connect(self._camera.set_target_fps)

        self._focus_panel.focusConfigChanged.connect(self._on_focus_config)
        self._focus_panel.resetPeakRequested.connect(self._camera.reset_focus_peak)
        self._focus_panel.viewOptionsChanged.connect(self._on_view_options)

    def _wire_view(self) -> None:
        self._view.zoomChanged.connect(
            lambda zoom: self._zoom_status.setText(f"{zoom:.1f}x")
        )

    def _try_connect(self) -> None:
        """Open the camera if it is not already open. Safe to call repeatedly."""
        if self._camera.state in (
            DeviceState.READY,
            DeviceState.BUSY,
            DeviceState.CONNECTING,
        ):
            return
        if not self._reported_failure:
            self._status.setText("Looking for camera...")
        self._camera.open()

    def _autostart_preview(self) -> None:
        if self._camera.state == DeviceState.READY and not self._camera.previewing:
            self._start_preview()

    def _on_connect_clicked(self) -> None:
        self._auto_reconnect = True
        self._reported_failure = ""
        self._reconnect.start()
        self._try_connect()

    def _on_disconnect_clicked(self) -> None:
        # An explicit disconnect has to stick, or the retry loop immediately
        # undoes it and the button looks broken.
        self._auto_reconnect = False
        self._reconnect.stop()
        self._camera.close()

    # -- slots ------------------------------------------------------------

    def _on_state(self, state: DeviceState, detail: str) -> None:
        self._camera_panel.set_state(state, detail)

        if state == DeviceState.READY:
            self._reported_failure = ""
            self._status.setText(detail or state.label)
            self._status.setStyleSheet("")
            if self._auto_reconnect and not self._camera.previewing:
                # The session is open; start streaming without being asked.
                QTimer.singleShot(300, self._autostart_preview)
        elif state == DeviceState.ERROR and self._auto_reconnect:
            # Say what went wrong once, then stop repainting the same error
            # every three seconds -- a status line that flickers reads as a
            # fault in the app rather than a missing camera.
            self._status.setText(f"Waiting for camera - {detail.splitlines()[0]}")
            self._status.setStyleSheet("color: #d0a050;")
        else:
            self._status.setText(detail or state.label)
            self._status.setStyleSheet(
                "color: #e06060;" if state == DeviceState.ERROR else ""
            )

        if state in (DeviceState.DISCONNECTED, DeviceState.ERROR):
            self._view.clear(
                "Waiting for camera..." if self._auto_reconnect else "Camera disconnected"
            )
            self._focus_panel.clear()

    def _on_frame(self, frame) -> None:
        self._view.set_frame(frame)
        self._focus_panel.set_reading(frame.reading)
        self._camera_panel.set_exposure(frame.exposure)

    def _start_preview(self) -> None:
        self._view.clear("Starting live view...")
        self._focus_panel.clear()
        self._camera.start_preview()
        self._camera_panel.set_previewing(True)

    def _on_preview_stopped(self) -> None:
        self._camera_panel.set_previewing(False)
        self._view.clear("Live view stopped")

    def _on_focus_config(self, changes: dict) -> None:
        self._camera.update_focus(**changes)
        # The ROI box is drawn by the view but sized by the panel, so the two
        # have to be kept in step or the rectangle stops matching what the
        # meter actually measures.
        if "roi_fraction" in changes:
            self._view.set_roi_fraction(changes["roi_fraction"])

    def _on_view_options(self, show_roi: bool, show_grid: bool) -> None:
        self._view.set_show_roi(show_roi)
        self._view.set_show_grid(show_grid)

    def _on_capture(self, result) -> None:
        """Show the new frame for a keep-or-discard decision."""
        self._pending_capture = result
        name = Path(result.local_path).name
        if not result.review.isNull():
            self._view.show_still(result.review)
            self._review_label.setText(
                f"{name}  -  {result.review.width()}x{result.review.height()} preview. "
                "Zoom in to check focus."
            )
            self._show_capture_exposure(result.exposure)
        else:
            # RAW-only: the file is saved but there is no embedded JPEG to show.
            self._review_label.setText(
                f"{name} saved. No preview available for this format."
            )
            self._review_verdict.setText("")
        self._review_bar.show()
        self._status.setText(f"Saved {result.local_path}")

    def _show_capture_exposure(self, exposure) -> None:
        if exposure is None:
            self._review_verdict.setText("")
            return
        if exposure.is_usable and exposure.verdict == "ok":
            self._review_verdict.setText(f"Exposure {exposure.mean:.0f}/255 - ok")
            self._review_verdict.setStyleSheet("color: #7ac77a;")
            return
        colour = "#e05a5a" if not exposure.is_usable else "#e0a050"
        self._review_verdict.setText(
            f"CAPTURED FRAME: {exposure.verdict} ({exposure.mean:.0f}/255). "
            f"{exposure.advice}"
        )
        self._review_verdict.setStyleSheet(f"color: {colour}; font-weight: bold;")

    def _end_review(self) -> None:
        self._review_verdict.setText("")
        self._pending_capture = None
        self._review_bar.hide()
        self._view.clear_still()

    def _keep_capture(self) -> None:
        if self._pending_capture is not None:
            self._status.setText(f"Kept {Path(self._pending_capture.local_path).name}")
        self._end_review()

    def _discard_capture(self) -> None:
        if self._pending_capture is not None:
            self._camera.discard(self._pending_capture)
        self._end_review()

    def _on_discarded(self, path: str) -> None:
        self._status.setText(f"Deleted {Path(path).name}")

    def _on_error(self, message: str) -> None:
        # No modal. The panel already shows the full, word-wrapped text, and
        # with a retry loop running a dialog would reappear every three seconds
        # -- unusable, especially on a touchscreen where dismissing it costs a
        # deliberate tap. The status line carries the summary, the panel the
        # detail and the suggested fix.
        if self._reported_failure == message:
            return
        self._reported_failure = message
        self._status.setText(message.splitlines()[0])
        self._status.setStyleSheet("color: #d0a050;")

    # -- shutdown ---------------------------------------------------------

    def closeEvent(self, event) -> None:
        # Blocks briefly so live view is torn down and the mirror drops before
        # the process exits; a body left in live view needs a power cycle.
        self._camera.shutdown()
        super().closeEvent(event)
