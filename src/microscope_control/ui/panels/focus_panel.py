"""Focus aids: the sharpness trace, peaking controls and ROI size.

The trace is the part that earns its space. A single sharpness number tells you
almost nothing, because you cannot tell a good score from a bad one without
something to compare against -- but the *shape* of that number over the last
few seconds tells you everything: rising means keep turning the knob the way
you are, falling means you have gone past, and a plateau at the held peak means
stop. On a manual-focus BH3 that curve is the whole instrument.
"""

from __future__ import annotations

from collections import deque

from PySide6.QtCore import QPointF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPen, QPolygonF
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from ..compact import allow_shrink
from ...core import focus as focus_mod

_TRACE_BACKGROUND = QColor(24, 24, 28)
_TRACE_PEAK = QColor(120, 120, 130)
_TRACE_GOOD = QColor(90, 220, 130)
_TRACE_FAIR = QColor(230, 190, 80)
_TRACE_POOR = QColor(200, 90, 90)

# Fraction-of-peak boundaries for the trace colour. 95% is tight enough that
# hitting green means you are genuinely at the top of the curve rather than
# merely nearby, which matters because the Laplacian peak is narrow.
_GOOD_THRESHOLD = 0.95
_FAIR_THRESHOLD = 0.75

PEAKING_COLORS = (
    ("Red", (255, 48, 48)),
    ("Green", (60, 255, 90)),
    ("Blue", (70, 150, 255)),
    ("Yellow", (255, 220, 60)),
    ("White", (255, 255, 255)),
)


def _quality_color(fraction: float) -> QColor:
    if fraction >= _GOOD_THRESHOLD:
        return _TRACE_GOOD
    if fraction >= _FAIR_THRESHOLD:
        return _TRACE_FAIR
    return _TRACE_POOR


class FocusTrace(QWidget):
    """Rolling plot of the sharpness score with a held-peak reference line.

    Self-scaling to the peak rather than to an absolute range, because the
    score has no absolute range -- swapping a 4x objective for a 40x moves it
    by orders of magnitude.
    """

    def __init__(self, samples: int = 220, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMinimumHeight(84)
        self._values: deque[float] = deque(maxlen=samples)
        self._peak = 0.0
        self._settled = False

    def add(self, value: float, peak: float, settled: bool = True) -> None:
        self._values.append(value)
        self._peak = peak
        self._settled = settled
        self.update()

    def clear(self) -> None:
        self._values.clear()
        self._peak = 0.0
        self._settled = False
        self.update()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.fillRect(self.rect(), _TRACE_BACKGROUND)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        if len(self._values) < 2 or self._peak <= 0:
            painter.setPen(QColor(110, 110, 118))
            painter.setFont(QFont(self.font().family(), 9))
            painter.drawText(
                self.rect(), Qt.AlignmentFlag.AlignCenter, "waiting for frames"
            )
            return

        # Headroom above the peak line so a new maximum is visible rather than
        # clipped flat against the top edge.
        ceiling = self._peak * 1.08
        width, height = self.width(), self.height()
        margin = 4

        def y_for(value: float) -> float:
            normalised = max(0.0, min(1.0, value / ceiling))
            return height - margin - normalised * (height - 2 * margin)

        pen = QPen(_TRACE_PEAK)
        pen.setStyle(Qt.PenStyle.DashLine)
        pen.setWidth(1)
        painter.setPen(pen)
        peak_y = y_for(self._peak)
        painter.drawLine(QPointF(0, peak_y), QPointF(width, peak_y))

        count = len(self._values)
        step = width / max(1, count - 1)
        polygon = QPolygonF(
            [QPointF(i * step, y_for(v)) for i, v in enumerate(self._values)]
        )
        latest = self._values[-1]
        # Neutral until settled: colouring against a one-frame peak would
        # paint the curve green while still badly out of focus.
        pen = QPen(
            _quality_color(latest / self._peak)
            if self._settled and self._peak
            else _TRACE_PEAK
        )
        pen.setWidth(2)
        painter.setPen(pen)
        painter.drawPolyline(polygon)


class FocusPanel(QGroupBox):
    """Controls for the focus metric, the ROI and peaking."""

    # Emits keyword changes destined for CameraController.update_focus().
    focusConfigChanged = Signal(dict)
    viewOptionsChanged = Signal(bool, bool)  # show_roi, show_grid
    resetPeakRequested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__("Focus", parent)
        layout = QVBoxLayout(self)

        self._score_label = QLabel("--")
        score_font = QFont(self.font().family(), 20)
        score_font.setBold(True)
        self._score_label.setFont(score_font)
        self._score_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        allow_shrink(self._score_label, minimum=60)

        self._peak_label = QLabel("no peak yet")
        self._peak_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._peak_label.setWordWrap(True)
        self._peak_label.setStyleSheet("color: #9a9aa2;")

        self._trace = FocusTrace()
        allow_shrink(self._trace, minimum=60)

        layout.addWidget(self._score_label)
        layout.addWidget(self._peak_label)
        layout.addWidget(self._trace)

        form = QFormLayout()
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapAllRows)

        self._metric = QComboBox()
        self._metric.addItem("Laplacian (sharp peak)", focus_mod.LAPLACIAN)
        self._metric.addItem("Tenengrad (noise-tolerant)", focus_mod.TENENGRAD)
        self._metric.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self._metric.setMinimumContentsLength(8)
        allow_shrink(self._metric)
        self._metric.currentIndexChanged.connect(self._on_metric_changed)
        form.addRow("Metric", self._metric)

        self._roi = QSlider(Qt.Orientation.Horizontal)
        self._roi.setRange(10, 100)
        self._roi.setValue(40)
        self._roi.valueChanged.connect(self._on_roi_changed)
        self._roi_label = QLabel("40% of frame")
        roi_box = QVBoxLayout()
        roi_box.setContentsMargins(0, 0, 0, 0)
        roi_box.addWidget(self._roi)
        roi_box.addWidget(self._roi_label)
        roi_widget = QWidget()
        roi_widget.setLayout(roi_box)
        form.addRow("ROI size", roi_widget)

        layout.addLayout(form)

        self._peaking = QCheckBox("Focus peaking")
        self._peaking.toggled.connect(self._on_peaking_toggled)
        layout.addWidget(self._peaking)

        # "Sensitivity" sits above its controls rather than beside them: on a
        # narrow dock the label was squeezed to 40 px against the 64 it needs
        # and clipped mid-word. Same reasoning as the live view rate row.
        sensitivity_label = QLabel("Sensitivity")
        sensitivity_label.setWordWrap(True)
        layout.addWidget(sensitivity_label)

        peak_row = QHBoxLayout()
        self._sensitivity = QSlider(Qt.Orientation.Horizontal)
        self._sensitivity.setRange(0, 100)
        self._sensitivity.setValue(50)
        self._sensitivity.setEnabled(False)
        self._sensitivity.valueChanged.connect(self._on_sensitivity_changed)
        self._color = QComboBox()
        for name, rgb in PEAKING_COLORS:
            self._color.addItem(name, rgb)
        self._color.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self._color.setMinimumContentsLength(4)
        self._color.setEnabled(False)
        allow_shrink(self._color, minimum=48)
        self._color.currentIndexChanged.connect(self._on_color_changed)
        peak_row.addWidget(self._sensitivity, 1)
        peak_row.addWidget(self._color)
        layout.addLayout(peak_row)

        # One per row. A QCheckBox clips its label rather than eliding or
        # wrapping it, so sharing a row turned "Show ROI" and "Thirds grid"
        # into "Show" and "Thirds" once the dock narrowed -- the same failure
        # the buttons had. Stacked, they cannot clip at any width.
        self._show_roi = QCheckBox("Show ROI")
        self._show_roi.setChecked(True)
        self._show_roi.toggled.connect(self._emit_view_options)
        self._show_grid = QCheckBox("Thirds grid")
        self._show_grid.toggled.connect(self._emit_view_options)
        layout.addWidget(self._show_roi)
        layout.addWidget(self._show_grid)

        self._analysis = QCheckBox("Analyse frames")
        self._analysis.setChecked(True)
        self._analysis.setToolTip(
            "Turn off to skip focus scoring and peaking. Costs a few ms per "
            "frame, so disabling it buys frame rate while composing."
        )
        self._analysis.toggled.connect(self._on_analysis_toggled)
        layout.addWidget(self._analysis)

        reset = QPushButton("Reset peak")
        reset.setToolTip(
            "Clear the held peak and history. Do this after moving to a new "
            "field or changing objective -- old scores are not comparable."
        )
        allow_shrink(reset, minimum=70)
        reset.clicked.connect(self._on_reset)
        layout.addWidget(reset)
        layout.addStretch(1)

    # -- incoming ---------------------------------------------------------

    def set_reading(self, reading) -> None:
        if reading is None:
            return
        self._score_label.setText(f"{reading.value:,.0f}")

        # Until the meter has settled, the "peak" is just the highest of a
        # handful of frames -- so the first reading is always 100% of it. Saying
        # so would tell you that you are in focus at the exact moment you have
        # not started focusing, so the readout stays explicitly neutral instead.
        if not reading.settled:
            self._score_label.setStyleSheet("color: #9a9aa2;")
            self._peak_label.setText(
                f"finding range... ({reading.samples}/{focus_mod.SETTLE_SAMPLES})"
            )
        else:
            fraction = reading.fraction_of_peak
            self._score_label.setStyleSheet(
                f"color: {_quality_color(fraction).name()};"
            )
            self._peak_label.setText(
                f"{fraction * 100:.0f}% of peak  ({reading.peak:,.0f})"
            )
        self._trace.add(reading.value, reading.peak, reading.settled)

    def clear(self) -> None:
        self._score_label.setText("--")
        self._score_label.setStyleSheet("")
        self._peak_label.setText("no peak yet")
        self._trace.clear()

    @property
    def roi_fraction(self) -> float:
        return self._roi.value() / 100.0

    # -- outgoing ---------------------------------------------------------

    def _on_metric_changed(self) -> None:
        self._trace.clear()
        self.focusConfigChanged.emit({"metric": self._metric.currentData()})

    def _on_roi_changed(self, value: int) -> None:
        self._roi_label.setText(f"{value}% of frame")
        # Resizing the ROI changes what is being measured, so scores from
        # before and after are not on the same footing; the peak resets with it.
        self._trace.clear()
        self.focusConfigChanged.emit({"roi_fraction": value / 100.0})
        self.resetPeakRequested.emit()

    def _on_peaking_toggled(self, enabled: bool) -> None:
        self._sensitivity.setEnabled(enabled)
        self._color.setEnabled(enabled)
        self.focusConfigChanged.emit({"peaking": enabled})

    def _on_sensitivity_changed(self, value: int) -> None:
        self.focusConfigChanged.emit({"sensitivity": value})

    def _on_color_changed(self) -> None:
        self.focusConfigChanged.emit({"color": self._color.currentData()})

    def _on_analysis_toggled(self, enabled: bool) -> None:
        if not enabled:
            self.clear()
        self.focusConfigChanged.emit({"analysis_enabled": enabled})

    def _on_reset(self) -> None:
        self._trace.clear()
        self.resetPeakRequested.emit()

    def _emit_view_options(self) -> None:
        self.viewOptionsChanged.emit(
            self._show_roi.isChecked(), self._show_grid.isChecked()
        )
