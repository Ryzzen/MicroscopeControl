"""The live view viewport.

This is where the focusing actually happens, so it does two things beyond
blitting a JPEG.

Digital zoom, because the preview stream is 1024x680 regardless of how the
window is sized. Scaled to fit a large display, a 1:1 preview pixel covers
several screen pixels and fine detail turns to mush; zooming in on a nearest-
neighbour crop is the only way to judge whether an edge is genuinely resolved
or just big. Panning follows, since at 4x you see an eighth of the field.

An ROI rectangle, drawn because the focus score is computed over that region
and nowhere else. Showing the box makes the number legible -- if the meter is
flat while you rack the knob, the usual reason is that the ROI is parked on
empty background, and that is far easier to see than to deduce.

Note the widget renders with smoothing off. Qt's smooth transform is the right
default for photographs and the wrong one here: interpolation invents an
edge gradient that reads as sharpness, which is precisely the judgement this
window exists to support.
"""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import QWidget

MIN_ZOOM = 1.0
MAX_ZOOM = 12.0
_ZOOM_STEP = 1.25

_BACKGROUND = QColor(18, 18, 20)
_ROI_COLOR = QColor(80, 200, 255)
_GRID_COLOR = QColor(255, 255, 255, 46)
_HUD_COLOR = QColor(230, 230, 235, 200)


class LiveView(QWidget):
    """Displays PreviewFrames with zoom, pan and focus-aid overlays."""

    zoomChanged = Signal(float)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMinimumSize(480, 320)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAutoFillBackground(False)

        self._image = None
        self._overlay = None
        self._overlay_rect = None
        self._fps = 0.0
        self._zoom = 1.0
        # The image-space point pinned to the centre of the widget. Stored in
        # image coordinates rather than as a pixel offset so it stays valid
        # when the window is resized.
        self._centre = QPointF(0.0, 0.0)
        self._dragging = False
        self._drag_origin = QPointF()
        self._drag_centre = QPointF()

        self._still = None
        self._roi_fraction = 0.4
        self._show_roi = True
        self._show_grid = False
        self._placeholder = "No live view"

    # -- content ----------------------------------------------------------

    def show_still(self, image) -> None:
        """Display a captured frame for review, ignoring live frames until cleared.

        Reuses this widget rather than opening a dialog so the existing zoom and
        pan work on the captured image -- deciding whether a shot is sharp needs
        a look at actual pixels, which a thumbnail in a message box cannot give.
        """
        self._still = image
        self._image = image
        self._overlay = None
        self._overlay_rect = None
        self._centre = QPointF(image.width() / 2.0, image.height() / 2.0)
        self._zoom = 1.0
        self.zoomChanged.emit(self._zoom)
        self.update()

    def clear_still(self) -> None:
        """Return to live frames."""
        self._still = None
        self._image = None
        self._zoom = 1.0
        self.update()

    @property
    def reviewing(self) -> bool:
        return self._still is not None

    def set_frame(self, frame) -> None:
        # A still under review owns the viewport; live frames keep arriving in
        # the background and are simply not drawn.
        if self._still is not None:
            return
        first = self._image is None
        self._image = frame.image
        self._overlay = frame.overlay
        self._overlay_rect = frame.overlay_rect
        self._fps = frame.fps
        if first:
            self._centre = QPointF(
                self._image.width() / 2.0, self._image.height() / 2.0
            )
        self.update()

    def clear(self, message: str = "No live view") -> None:
        if self._still is not None:
            return
        self._image = None
        self._overlay = None
        self._overlay_rect = None
        self._fps = 0.0
        self._placeholder = message
        self.update()

    def set_roi_fraction(self, fraction: float) -> None:
        self._roi_fraction = fraction
        self.update()

    def set_show_roi(self, show: bool) -> None:
        self._show_roi = show
        self.update()

    def set_show_grid(self, show: bool) -> None:
        self._show_grid = show
        self.update()

    # -- view transform ---------------------------------------------------

    @property
    def zoom(self) -> float:
        return self._zoom

    def _fit_scale(self) -> float:
        """Screen pixels per image pixel at zoom 1.0 (whole frame visible)."""
        if self._image is None or self._image.width() == 0:
            return 1.0
        return min(
            self.width() / self._image.width(), self.height() / self._image.height()
        )

    def _scale(self) -> float:
        return self._fit_scale() * self._zoom

    def _clamp_centre(self) -> None:
        """Keep the view inside the frame.

        Once zoomed past fit, the visible half-extent is smaller than the image
        half-extent and the centre is confined to what is left. At or below fit
        the axis is pinned to the image centre instead, so an unzoomed frame
        cannot be dragged out of the window.
        """
        if self._image is None:
            return
        scale = self._scale()
        if scale <= 0:
            return
        half_w = self.width() / (2.0 * scale)
        half_h = self.height() / (2.0 * scale)
        img_w, img_h = self._image.width(), self._image.height()

        if half_w >= img_w / 2.0:
            x = img_w / 2.0
        else:
            x = max(half_w, min(img_w - half_w, self._centre.x()))
        if half_h >= img_h / 2.0:
            y = img_h / 2.0
        else:
            y = max(half_h, min(img_h - half_h, self._centre.y()))
        self._centre = QPointF(x, y)

    def _image_point(self, widget_pos: QPointF) -> QPointF:
        scale = self._scale()
        if scale <= 0:
            return QPointF()
        return QPointF(
            self._centre.x() + (widget_pos.x() - self.width() / 2.0) / scale,
            self._centre.y() + (widget_pos.y() - self.height() / 2.0) / scale,
        )

    def set_zoom(self, zoom: float, anchor: QPointF | None = None) -> None:
        """Change magnification, optionally holding an image point under the cursor.

        Anchoring to the cursor is what makes wheel-zoom usable for inspecting a
        specific feature: without it, zooming in on something off-centre walks
        it straight out of frame and you chase it with the pan.
        """
        zoom = max(MIN_ZOOM, min(MAX_ZOOM, zoom))
        if abs(zoom - self._zoom) < 1e-6:
            return
        pinned = self._image_point(anchor) if anchor is not None else None
        self._zoom = zoom
        if pinned is not None and self._image is not None:
            scale = self._scale()
            self._centre = QPointF(
                pinned.x() - (anchor.x() - self.width() / 2.0) / scale,
                pinned.y() - (anchor.y() - self.height() / 2.0) / scale,
            )
        self._clamp_centre()
        self.zoomChanged.emit(self._zoom)
        self.update()

    def zoom_in(self) -> None:
        self.set_zoom(self._zoom * _ZOOM_STEP)

    def zoom_out(self) -> None:
        self.set_zoom(self._zoom / _ZOOM_STEP)

    def reset_view(self) -> None:
        self._zoom = 1.0
        if self._image is not None:
            self._centre = QPointF(
                self._image.width() / 2.0, self._image.height() / 2.0
            )
        self.zoomChanged.emit(self._zoom)
        self.update()

    # -- interaction ------------------------------------------------------

    def wheelEvent(self, event) -> None:
        steps = event.angleDelta().y() / 120.0
        if steps:
            self.set_zoom(self._zoom * (_ZOOM_STEP**steps), event.position())
        event.accept()

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._dragging = True
            self._drag_origin = event.position()
            self._drag_centre = QPointF(self._centre)
            self.setCursor(Qt.CursorShape.ClosedHandCursor)

    def mouseMoveEvent(self, event) -> None:
        if not self._dragging:
            return
        scale = self._scale()
        if scale <= 0:
            return
        delta = event.position() - self._drag_origin
        self._centre = QPointF(
            self._drag_centre.x() - delta.x() / scale,
            self._drag_centre.y() - delta.y() / scale,
        )
        self._clamp_centre()
        self.update()

    def mouseReleaseEvent(self, event) -> None:
        self._dragging = False
        self.unsetCursor()

    def mouseDoubleClickEvent(self, event) -> None:
        self.reset_view()

    def keyPressEvent(self, event) -> None:
        if event.key() in (Qt.Key.Key_Plus, Qt.Key.Key_Equal):
            self.zoom_in()
        elif event.key() == Qt.Key.Key_Minus:
            self.zoom_out()
        elif event.key() == Qt.Key.Key_0:
            self.reset_view()
        else:
            super().keyPressEvent(event)

    def resizeEvent(self, event) -> None:
        self._clamp_centre()
        super().resizeEvent(event)

    # -- painting ---------------------------------------------------------

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.fillRect(self.rect(), _BACKGROUND)

        if self._image is None:
            painter.setPen(QColor(120, 120, 128))
            painter.setFont(QFont(self.font().family(), 11))
            painter.drawText(
                self.rect(), Qt.AlignmentFlag.AlignCenter, self._placeholder
            )
            return

        scale = self._scale()
        painter.save()
        painter.translate(self.width() / 2.0, self.height() / 2.0)
        painter.scale(scale, scale)
        painter.translate(-self._centre.x(), -self._centre.y())

        # Nearest-neighbour on purpose: see the module docstring. Smoothing
        # would fabricate the very edge detail being judged.
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, False)
        painter.drawImage(0, 0, self._image)
        if self._overlay is not None and self._overlay_rect is not None:
            # Stretched, not blitted: the overlay is built at analysis
            # resolution (normally half the preview) and is inset by the
            # Sobel's invalid border, so the worker sends the target rectangle
            # in image coordinates with it. Nearest-neighbour is already set
            # above, so the mask stays crisp rather than feathering out.
            painter.drawImage(self._overlay_rect, self._overlay)

        self._paint_guides(painter, scale)
        painter.restore()
        self._paint_hud(painter)

    def _paint_guides(self, painter: QPainter, scale: float) -> None:
        img_w, img_h = self._image.width(), self._image.height()

        if self._show_grid:
            # Thirds, for centring a feature by eye before zooming in on it.
            pen = QPen(_GRID_COLOR)
            pen.setWidthF(1.0 / scale)
            painter.setPen(pen)
            for i in (1, 2):
                x = img_w * i / 3.0
                y = img_h * i / 3.0
                painter.drawLine(QPointF(x, 0), QPointF(x, img_h))
                painter.drawLine(QPointF(0, y), QPointF(img_w, y))

        # The ROI box belongs to focus metering, which is a live-view concept.
        if self._show_roi and self._still is None:
            roi_w = img_w * self._roi_fraction
            roi_h = img_h * self._roi_fraction
            rect = QRectF(
                (img_w - roi_w) / 2.0, (img_h - roi_h) / 2.0, roi_w, roi_h
            )
            pen = QPen(_ROI_COLOR)
            pen.setWidthF(max(1.0, 1.5 / scale))
            pen.setStyle(Qt.PenStyle.DashLine)
            painter.setPen(pen)
            painter.drawRect(rect)

    def _paint_hud(self, painter: QPainter) -> None:
        painter.setPen(_HUD_COLOR)
        font = QFont(self.font().family(), 9)
        painter.setFont(font)
        text = f"{self._image.width()}x{self._image.height()}   {self._zoom:.1f}x"
        if self._fps > 0:
            text += f"   {self._fps:.1f} fps"
        painter.drawText(
            self.rect().adjusted(8, 0, -8, -6),
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignBottom,
            text,
        )
