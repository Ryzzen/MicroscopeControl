"""QImage <-> numpy plumbing and the focus-peaking overlay.

Kept apart from `focus` so the scoring maths stays free of Qt: the metrics are
plain array functions and can be exercised without a QApplication, which is
what makes them testable and reusable when the Z stage starts driving an
autofocus sweep.

Everything here runs on the camera worker thread, never on the GUI thread. A
preview frame is a JPEG that has to be decoded, greyscaled and differentiated
before anything can be drawn, and at 15-25 fps that is more than enough work to
visibly stutter the UI if it happened during paint.
"""

from __future__ import annotations

import numpy as np

from . import focus
from PySide6.QtCore import Qt
from PySide6.QtGui import QImage

# Peaking thresholds on absolute gradient strength, guarded by a noise floor.
#
# Two mechanisms were wrong before this one, and both are worth recording
# because both look obviously correct until measured.
#
# A percentile of the frame's gradients paints the top N% whatever the frame
# contains, so the overlay is equally dense in focus and out. Worse, it inverts:
# a defocused frame's top N% reaches further down into noise, so it paints
# *more* as focus gets worse.
#
# Scaling the threshold to the frame's noise level fails differently. Defocus
# does not change noise -- the blur is optical, the noise is added afterwards by
# the sensor -- so a noise-proportional threshold stays put while the image
# softens, and on a clean low-ISO frame it lands low enough that the broad soft
# ramps of a defocused edge sail over it. Measured on synthetic fields, that
# again paints more when defocused, because blurring conserves edge energy while
# spreading it: a sharp edge is a few pixels of gradient ~45, the same edge
# defocused is a wide ramp of gradient ~8, and a low threshold catches far more
# of the second.
#
# What actually tracks focus is the *peak* gradient, and that is an absolute
# quantity in image levels per pixel. So the sensitivity slider selects an
# absolute threshold, and the noise estimate only ever raises it -- which is
# what keeps high-ISO grain from being painted as detail.
#
# The Sobel magnitude is the basis rather than the Laplacian, which was also
# tried. The Laplacian discriminates focus better in principle -- a step edge
# blurred to width w gives gradient ~C/w but curvature ~C/w^2, so it falls off
# far faster -- but it amplifies noise by as much. Measured on a flat field at
# ISO-3200-like noise it painted 85% of the frame, against 13% for the Sobel.
# A focus aid that lights up on grain is worse than one that discriminates
# less sharply.
# Calibrated against 500 real frames off the BH3 at analysis resolution, for
# coverage of roughly 2% of the frame at the strict end and 9% at the loose
# end, passing through ~4% at the default. An earlier band tuned only on
# synthetic high-contrast blobs painted 0.24% of a real microscope frame at
# mid-slider -- technically correct and completely useless to look at, because
# a real field is far lower in contrast than a synthetic one and only the very
# top of the slider did anything at all.
#
# The lower bound is set by the recede-with-defocus property, not by taste.
# Measured falloff (heavily-defocused coverage / focused coverage) against
# level: 0.61 at 24, 0.80 at 18, 0.97 at 16, and 1.26 at 14 -- i.e. below about
# 20 the broad soft ramps of a defocused edge start outnumbering the thin hard
# ones of a focused edge and the overlay quietly inverts its own meaning. 24 is
# the loosest setting that still both discriminates (0.61) and paints enough of
# a real frame to read (7%).
_STRICT_LEVEL = 45.0  # at sensitivity 0: only hard, well-resolved edges
_LOOSE_LEVEL = 24.0  # at sensitivity 100: soft detail counts too

# The noise guard, as a multiple of a low quantile of the ROI's gradient
# magnitude. A low quantile rather than the median because the median climbs
# once real detail resolves, which would raise the threshold exactly when edges
# appear. Measured inside the ROI rather than across the frame because a
# whole-frame statistic is dominated by the dead surround outside the field
# stop, which carries no noise and drags the estimate to zero.
_NOISE_QUANTILE = 25.0
_NOISE_GUARD_MULTIPLE = 8.0

# Floor for a perfectly flat frame, where every estimate collapses to zero.
_MIN_GRADIENT = 3.0

# Subsampling stride for the quantile estimate. A full sort over ~600k floats
# every frame is real time at 20 fps; every 4th pixel in each axis is 16x
# cheaper and statistically indistinguishable for a quantile.
_SAMPLE_STRIDE = 4


def decode_preview(data: bytes) -> QImage | None:
    """Decode a gphoto2 preview payload into a QImage.

    The 5D Mk II returns baseline JPEG over PTP, but the format is not
    guaranteed across bodies, so this lets Qt sniff it rather than asserting
    JPEG. Returns None on a truncated or malformed frame, which does happen
    occasionally when the camera is mid-mode-change -- the caller drops the
    frame and carries on rather than tearing down the session.
    """
    image = QImage()
    if not image.loadFromData(data):
        return None
    return image


def to_gray(image: QImage) -> np.ndarray:
    """Copy a QImage into a 2-D uint8 luminance array.

    The copy is deliberate. numpy would otherwise alias the QImage's own
    buffer, and that QImage is about to be handed to the GUI thread through a
    queued signal; sharing a mutable view across that boundary is how you get
    a tear or a use-after-free that only shows up under load.
    """
    gray = image.convertToFormat(QImage.Format.Format_Grayscale8)
    width, height, stride = gray.width(), gray.height(), gray.bytesPerLine()
    if width == 0 or height == 0:
        return np.zeros((0, 0), dtype=np.uint8)
    buffer = np.frombuffer(gray.constBits(), dtype=np.uint8, count=stride * height)
    # Rows are padded to `stride`; slice the padding off before copying.
    return np.array(buffer.reshape(height, stride)[:, :width])


def analysis_gray(image: QImage, scale: float = 0.5) -> np.ndarray:
    """Downscale and greyscale a preview frame for analysis, in one step.

    Analysis does not need full preview resolution. Focus is a property of the
    whole field, not of individual pixels, and halving each axis cuts the work
    by four with a measured 0.99 correlation against the full-resolution score.

    On the tablet this is the single biggest win in the pipeline: the complete
    per-frame cost (decode, greyscale, score, gradient, overlay) drops from
    52 ms to 10 ms, which takes analysis from being the bottleneck -- below the
    camera's own ~14 fps -- to costing a small fraction of the frame budget.

    Nearest-neighbour, not smooth: interpolation would invent gradient where
    the sensor recorded none, which is precisely the quantity being measured.
    """
    if scale >= 0.999:
        return to_gray(image)
    width = max(2, int(image.width() * scale))
    height = max(2, int(image.height() * scale))
    small = image.scaled(
        width,
        height,
        Qt.AspectRatioMode.IgnoreAspectRatio,
        Qt.TransformationMode.FastTransformation,
    )
    return to_gray(small)


def centre_roi(gray: np.ndarray, fraction: float) -> np.ndarray:
    """Return the centred sub-array covering `fraction` of each axis.

    Focus is scored here rather than over the frame because on a microscope the
    edges of the field are vignetted, and with a low-NA objective on a
    full-frame sensor a large part of the frame is simply black.
    """
    height, width = gray.shape[:2]
    fraction = max(0.05, min(1.0, fraction))
    roi_h, roi_w = int(height * fraction), int(width * fraction)
    top, left = (height - roi_h) // 2, (width - roi_w) // 2
    return gray[top : top + roi_h, left : left + roi_w]


def peaking_overlay(
    magnitude: np.ndarray,
    sensitivity: int,
    color: tuple[int, int, int],
    roi_fraction: float = 0.4,
) -> QImage | None:
    """Build an ARGB overlay highlighting edges sharp enough to count as resolved.

    `magnitude` is raw `focus.gradient_l1` output. `sensitivity` (0-100) picks
    the absolute gradient a pixel must reach, so the overlay thins as the image
    softens -- see the notes above for why this is neither a percentile nor a
    noise-proportional threshold.

    The result is one pixel smaller per edge than the array it was built from
    (the Sobel has no valid interior on the border) and is at *analysis*
    resolution, which is normally half the preview. The caller stretches it
    over the corresponding region of the frame.
    """
    if magnitude.size == 0:
        return None

    fraction = max(0, min(100, sensitivity)) / 100.0
    level = _STRICT_LEVEL + (_LOOSE_LEVEL - _STRICT_LEVEL) * fraction

    roi = centre_roi(magnitude, roi_fraction)
    reference = roi if roi.size else magnitude
    noise = float(
        np.percentile(reference[::_SAMPLE_STRIDE, ::_SAMPLE_STRIDE], _NOISE_QUANTILE)
    )
    threshold = max(
        level * focus.L1_SCALE, noise * _NOISE_GUARD_MULTIPLE, _MIN_GRADIENT * focus.L1_SCALE
    )

    mask = magnitude >= threshold
    if not mask.any():
        return None

    height, width = mask.shape
    # One multiply over the mask, rather than allocating an (h, w, 4) buffer and
    # scattering into it through boolean indexing: 13.8 ms -> 8.1 ms at full
    # resolution, and it is pure memory traffic either way.
    packed = np.uint32(
        (255 << 24) | (color[0] << 16) | (color[1] << 8) | color[2]
    )
    argb = mask.astype(np.uint32) * packed
    # tobytes() + copy() hands Qt an owned buffer; constructing a QImage over
    # the numpy memory would leave it dangling once `argb` is collected.
    return QImage(
        argb.tobytes(), width, height, width * 4, QImage.Format.Format_ARGB32
    ).copy()
