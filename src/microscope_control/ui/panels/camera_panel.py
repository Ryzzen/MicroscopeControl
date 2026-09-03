"""Camera connection, exposure and capture controls.

Exposure is here rather than in a later milestone because live view without it
is not usable on a microscope: brightfield and darkfield on the same specimen
differ by several stops, and a preview that is clipped white or crushed black
carries no edge detail for the focus meter to score. ISO and shutter are the
only two that exist -- the phototube has no electronic lens, so there is no
aperture to drive.

Capture is deliberately thin: a button and a folder. Structured session capture
belongs with the Z stage, when a filename finally has coordinates worth
recording in it.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ..compact import allow_shrink, allow_shrink_all
from ...devices.base import DeviceState
from ...devices.camera import (
    DEFAULT_TARGET_FPS,
    UNPACED,
    describe_image_format,
)

DEFAULT_CAPTURE_DIR = Path.home() / "Pictures" / "microscope"


class CameraPanel(QGroupBox):
    """Bindings for a CameraController, with no direct hardware knowledge."""

    connectRequested = Signal()
    disconnectRequested = Signal()
    previewStartRequested = Signal()
    previewStopRequested = Signal()
    settingChanged = Signal(str, str)
    captureRequested = Signal(str)
    targetFpsChanged = Signal(float)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__("Camera", parent)
        self._connected = False
        self._previewing = False
        self._combos: dict[str, QComboBox] = {}

        layout = QVBoxLayout(self)

        self._model_label = QLabel("Not connected")
        self._model_label.setStyleSheet("color: #9a9aa2;")
        self._model_label.setWordWrap(True)
        layout.addWidget(self._model_label)

        # Exposure readout. Sits directly above the capture controls because
        # its job is to be seen before the shutter is pressed: without it the
        # app will happily record a frame with no signal in it and say nothing.
        self._exposure_label = QLabel("")
        self._exposure_label.setWordWrap(True)
        self._exposure_label.hide()
        layout.addWidget(self._exposure_label)

        # Stacked, not side by side. Sharing a row, each button gets half the
        # dock width, and a shrinking QPushButton clips its label mid-word
        # rather than eliding it -- "Disconnect" became "isconne", which reads
        # as a broken widget rather than a narrow one. Full-width rows cannot
        # clip at any dock width, and they are easier to hit on a touchscreen.
        self._connect_button = QPushButton("Connect")
        self._connect_button.clicked.connect(self._on_connect_clicked)
        self._preview_button = QPushButton("Start live view")
        self._preview_button.setEnabled(False)
        self._preview_button.clicked.connect(self._on_preview_clicked)
        allow_shrink_all(self._connect_button, self._preview_button, minimum=70)
        layout.addWidget(self._connect_button)
        layout.addWidget(self._preview_button)

        # Live view rate. Exposed rather than fixed because the right answer
        # depends on what you are doing: pulling frames flat out costs about a
        # full CPU core, which is worth it while hunting focus and wasteful
        # while the rig just sits there.
        # Label above the combo, like every other setting row -- side by side
        # the label itself clips ("Live view rat") once the dock is narrow.
        rate_row = QVBoxLayout()
        rate_row.setContentsMargins(0, 0, 0, 0)
        rate_label = QLabel("Live view rate")
        rate_label.setWordWrap(True)
        rate_row.addWidget(rate_label)
        self._rate = QComboBox()
        for label, value in (
            ("8 fps (idle)", 8.0),
            ("15 fps", DEFAULT_TARGET_FPS),
            ("20 fps", 20.0),
            ("Max", UNPACED),
        ):
            self._rate.addItem(label, value)
        self._rate.setCurrentIndex(1)
        self._rate.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self._rate.setMinimumContentsLength(6)
        self._rate.currentIndexChanged.connect(
            lambda: self.targetFpsChanged.emit(self._rate.currentData())
        )
        allow_shrink(self._rate)
        rate_row.addWidget(self._rate)
        layout.addLayout(rate_row)

        # Settings rows are built on the fly from whatever the body reports,
        # because the widget set libgphoto2 exposes varies by model and by
        # firmware -- hardcoding rows would leave dead controls on some bodies
        # and silently hide usable ones on others.
        self._settings_form = QFormLayout()
        self._settings_form.setContentsMargins(0, 6, 0, 6)
        # Put the label above its field when the panel is narrow. Some of these
        # labels come straight from the camera and are long ("Canon Auto
        # Exposure Mode"); side-by-side they set a width the dock can never go
        # below.
        # WrapAllRows, not WrapLongRows: Qt only wraps a "long" row when the
        # label is disproportionately wide, which never triggered for rows like
        # "ISO Speed" + a combo -- so the pair kept demanding a side-by-side
        # width the dock could not give, and the panel clipped instead of
        # shrinking. Labels above fields halves the width a row needs, and
        # reads better in a narrow column anyway.
        self._settings_form.setRowWrapPolicy(
            QFormLayout.RowWrapPolicy.WrapAllRows
        )
        layout.addLayout(self._settings_form)

        self._settings_hint = QLabel("Connect to read camera settings.")
        self._settings_hint.setStyleSheet("color: #9a9aa2;")
        self._settings_hint.setWordWrap(True)
        layout.addWidget(self._settings_hint)

        capture_row = QHBoxLayout()
        self._capture_dir = QLineEdit(str(DEFAULT_CAPTURE_DIR))
        allow_shrink(self._capture_dir, minimum=60)
        browse = QToolButton()
        browse.setText("...")
        browse.clicked.connect(self._on_browse)
        capture_row.addWidget(self._capture_dir, 1)
        capture_row.addWidget(browse)
        layout.addWidget(QLabel("Save to"))
        layout.addLayout(capture_row)

        self._capture_button = QPushButton("Capture still")
        allow_shrink(self._capture_button, minimum=70)
        self._capture_button.setEnabled(False)
        self._capture_button.clicked.connect(
            lambda: self.captureRequested.emit(self._capture_dir.text())
        )
        layout.addWidget(self._capture_button)
        layout.addStretch(1)

    # -- incoming ---------------------------------------------------------

    def set_state(self, state: DeviceState, detail: str) -> None:
        self._connected = state in (DeviceState.READY, DeviceState.BUSY)
        self._connect_button.setText("Disconnect" if self._connected else "Connect")
        self._connect_button.setEnabled(state != DeviceState.CONNECTING)
        self._preview_button.setEnabled(state == DeviceState.READY)
        # Blocked during a capture: the worker stops live view for the exposure
        # and restarts it afterwards, so the button would be lying about what
        # the camera is doing.
        self._capture_button.setEnabled(state == DeviceState.READY)

        if state == DeviceState.DISCONNECTED:
            self._model_label.setText("Not connected")
            self._exposure_label.hide()
            self._clear_settings()
        elif detail:
            self._model_label.setText(detail)

    _EXPOSURE_COLORS = {
        "no signal": "#e05a5a",
        "very dark": "#e0a050",
        "dark": "#d0c060",
        "blown": "#e05a5a",
        "bright": "#d0c060",
        "ok": "#7ac77a",
    }

    def set_exposure(self, exposure) -> None:
        if exposure is None:
            self._exposure_label.hide()
            return
        verdict = exposure.verdict
        # "Preview", not "Exposure": live view is gained up by the camera and
        # only tracks the real exposure within a limited range, so this figure
        # is a guide to what you are looking at, not a prediction of what will
        # be recorded. The capture itself is measured after the shutter.
        text = f"Preview level: {exposure.mean:.0f}/255 - {verdict}"
        if exposure.advice:
            text += f"\n{exposure.advice}"
        self._exposure_label.setText(text)
        self._exposure_label.setStyleSheet(
            f"color: {self._EXPOSURE_COLORS.get(verdict, '')};"
        )
        self._exposure_label.show()

    def set_previewing(self, previewing: bool) -> None:
        self._previewing = previewing
        self._preview_button.setText(
            "Stop live view" if previewing else "Start live view"
        )

    def set_settings(self, settings: dict) -> None:
        """Rebuild the settings rows from a fresh config read.

        Rows are rebuilt wholesale rather than patched because the camera
        changes the *choice lists* too, not just the values: the shutter speeds
        on offer are not the same set once live view is running.
        """
        self._clear_settings()
        if not settings:
            self._settings_hint.setText(
                "This body exposed no adjustable settings over PTP."
            )
            return

        self._settings_hint.hide()
        for name, choice in settings.items():
            combo = QComboBox()
            # Display text and stored value are kept separate: the camera only
            # accepts its own exact strings, but those strings are opaque
            # ("Large Fine JPEG" says nothing about resolution). Annotated text
            # goes in the item, the verbatim value in its data.
            for option in choice.choices:
                display = (
                    describe_image_format(option)
                    if name == "imageformat"
                    else option
                )
                combo.addItem(display, option)
            # Without this a combo demands the width of its widest entry --
            # "RAW + Large Fine JPEG" and friends -- and refuses to shrink,
            # which is what stopped the dock from scaling down. The full text
            # is still readable in the popup.
            combo.setSizeAdjustPolicy(
                QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
            )
            combo.setMinimumContentsLength(6)
            # Signals are blocked while seeding the current value; otherwise
            # populating the box looks like a user edit and gets written
            # straight back to the camera in a loop.
            combo.blockSignals(True)
            index = combo.findData(choice.value)
            if index >= 0:
                combo.setCurrentIndex(index)
            combo.blockSignals(False)
            combo.setEnabled(not choice.readonly)
            # currentIndexChanged, not currentTextChanged: the visible text is
            # annotated and the camera would reject it.
            combo.currentIndexChanged.connect(
                lambda _index, key=name, box=combo: self.settingChanged.emit(
                    key, box.currentData()
                )
            )
            allow_shrink(combo)
            self._combos[name] = combo
            # Build the label rather than passing a string: addRow() would make
            # a plain QLabel that neither wraps nor shrinks, and these names
            # come from the camera, not from us -- "Canon Auto Exposure Mode"
            # is longer than any dock width we want to insist on. Wrapping puts
            # it on two lines; clipping would just lose the end of it.
            label = QLabel(choice.label)
            label.setWordWrap(True)
            # A word-wrapped QLabel does not need allow_shrink: its minimum width is
        # already just its longest word, which is far below anything the dock
        # imposes. Giving it Ignored policy is actively harmful -- the label then
        # takes its *minimum* rather than the row width, so "Canon Auto Exposure
        # Mode" wrapped onto four lines instead of two.
            self._settings_form.addRow(label, combo)

    def _clear_settings(self) -> None:
        while self._settings_form.rowCount():
            self._settings_form.removeRow(0)
        self._combos.clear()
        self._settings_hint.setText("Connect to read camera settings.")
        self._settings_hint.show()

    # -- outgoing ---------------------------------------------------------

    def _on_connect_clicked(self) -> None:
        if self._connected:
            self.disconnectRequested.emit()
        else:
            self.connectRequested.emit()

    def _on_preview_clicked(self) -> None:
        if self._previewing:
            self.previewStopRequested.emit()
        else:
            self.previewStartRequested.emit()

    def _on_browse(self) -> None:
        directory = QFileDialog.getExistingDirectory(
            self, "Capture folder", self._capture_dir.text()
        )
        if directory:
            self._capture_dir.setText(directory)
