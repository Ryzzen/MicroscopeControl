"""Focus scoring for live view.

Focusing a microscope through a tethered DSLR is the awkward part of this rig.
The BH3's fine-focus knob is manual, the phototube image lands on screen at
preview resolution (1024x680 on the 5D Mk II), and the eye is poor at judging
sharpness from that. So every preview frame gets scored numerically and the
trend is plotted: you turn the knob and watch for the peak instead of squinting.

Two metrics, both computed over a centre ROI rather than the whole frame. The
periphery of a microscope field is usually vignetted or empty -- with a low-NA
objective on a full-frame sensor most of the frame is black -- and that dead
area drags any whole-frame score toward a constant.

  laplacian  Variance of the 4-neighbour Laplacian. Cheap and the conventional
             choice for brightfield. It has the sharpest peak of the two, which
             makes it the better knob-turning guide, but it is noise-sensitive
             so it wants low ISO.
  tenengrad  Mean squared Sobel gradient magnitude. The 3x3 Sobel smooths
             across the gradient direction, so it holds up on noisy or
             low-contrast fields (darkfield, phase) where the Laplacian starts
             chasing sensor noise. The trade is a flatter peak.

Both are *relative*: only their shape as you rack focus carries meaning, never
the absolute number. Both also scale with the square of scene contrast, so both
are divided by the ROI's own mean intensity squared. Without that, brightening
the LED reads as a focus improvement -- which would make the meter actively
misleading on a rig whose illumination is about to become motorised.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

import numpy as np

# Metric identifiers, also used as the UI combo box values.
LAPLACIAN = "laplacian"
TENENGRAD = "tenengrad"
METRICS = (LAPLACIAN, TENENGRAD)

# Scores are tiny after normalisation (order 1e-4). Scale them into a range that
# reads well as a bare integer in the UI. Arbitrary, and deliberately so -- it
# only has to be stable across frames.
_DISPLAY_SCALE = 1.0e6

# Guards the illumination-normalisation divide for a fully black ROI.
_EPS = 1.0e-6


def sobel(gray: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Return (gx, gy) 3x3 Sobel derivatives, each trimmed by one pixel.

    Written as slice arithmetic rather than a convolution call so the module
    stays on plain numpy -- pulling scipy or opencv into the closure for two
    3x3 kernels is not worth the build weight.
    """
    g = gray.astype(np.float32, copy=False)
    gx = (
        (g[:-2, 2:] + 2.0 * g[1:-1, 2:] + g[2:, 2:])
        - (g[:-2, :-2] + 2.0 * g[1:-1, :-2] + g[2:, :-2])
    ) / 8.0
    gy = (
        (g[2:, :-2] + 2.0 * g[2:, 1:-1] + g[2:, 2:])
        - (g[:-2, :-2] + 2.0 * g[:-2, 1:-1] + g[:-2, 2:])
    ) / 8.0
    return gx, gy


def gradient_l1(gray: np.ndarray) -> np.ndarray:
    """Fast Sobel gradient strength as |gx| + |gy|, in raw (un-normalised) units.

    This is the hot path -- it runs on every previewed frame -- so it trades the
    exact Euclidean magnitude for integer arithmetic and no square root. On the
    tablet that is 25.1 ms -> 11.1 ms per full-resolution frame, and the two
    disagree about which pixels clear a matched threshold on well under 1% of
    them, which is invisible in an overlay.

    Stays in int16 through the convolution: a Sobel of uint8 input peaks at
    4*255 = 1020, so it cannot overflow, and the narrower dtype halves the
    memory traffic that dominates at this size.

    Returns raw units -- 8x the conventional levels-per-pixel scaling, since
    dividing 700k elements to make the number prettier is exactly the kind of
    pass this function exists to avoid. Callers scale the threshold instead.
    """
    g = gray.astype(np.int16, copy=False)
    if g.shape[0] < 3 or g.shape[1] < 3:
        return np.zeros((0, 0), dtype=np.int16)
    gx = (g[:-2, 2:] + 2 * g[1:-1, 2:] + g[2:, 2:]) - (
        g[:-2, :-2] + 2 * g[1:-1, :-2] + g[2:, :-2]
    )
    gy = (g[2:, :-2] + 2 * g[2:, 1:-1] + g[2:, 2:]) - (
        g[:-2, :-2] + 2 * g[:-2, 1:-1] + g[:-2, 2:]
    )
    return np.abs(gx) + np.abs(gy)


# Raw gradient_l1 units per conventional level-per-pixel (the Sobel's /8).
L1_SCALE = 8.0


def gradient_magnitude(gray: np.ndarray) -> np.ndarray:
    """Sobel gradient magnitude, same shape as the trimmed interior.

    Shared by the tenengrad score and the peaking overlay so that what the
    meter measures and what the overlay paints are the same quantity.
    """
    gx, gy = sobel(gray)
    return np.sqrt(gx * gx + gy * gy)


def laplacian_variance(gray: np.ndarray) -> float:
    """Variance of the 4-neighbour discrete Laplacian."""
    g = gray.astype(np.float32, copy=False)
    if g.shape[0] < 3 or g.shape[1] < 3:
        return 0.0
    lap = (
        -4.0 * g[1:-1, 1:-1]
        + g[:-2, 1:-1]
        + g[2:, 1:-1]
        + g[1:-1, :-2]
        + g[1:-1, 2:]
    )
    return float(lap.var())


def tenengrad(gray: np.ndarray) -> float:
    """Mean squared Sobel gradient magnitude."""
    g = gray.astype(np.float32, copy=False)
    if g.shape[0] < 3 or g.shape[1] < 3:
        return 0.0
    gx, gy = sobel(g)
    return float(np.mean(gx * gx + gy * gy))


def score(gray: np.ndarray, metric: str = LAPLACIAN) -> float:
    """Illumination-normalised sharpness score for a grayscale ROI.

    Dividing by mean^2 makes the score invariant to a uniform brightness
    change, because both metrics are quadratic in intensity. It does not make
    it invariant to a *contrast* change -- nothing does, since that is the same
    thing focus changes -- so the LED still has to hold steady while you focus.
    """
    if gray.size == 0:
        return 0.0
    raw = tenengrad(gray) if metric == TENENGRAD else laplacian_variance(gray)
    mean = float(gray.mean())
    return _DISPLAY_SCALE * raw / (mean * mean + _EPS)


# Frames the meter must see before its peak means anything. The first reading
# is trivially also the best reading, so an unsettled meter reports 100% of peak
# while you are still badly out of focus -- which reads as "in focus" at exactly
# the moment that is most wrong. Roughly a second of live view at typical
# preview rates.
SETTLE_SAMPLES = 12


# Level below which a pixel carries no usable signal, and above which it is
# clipped. 8/255 is roughly where JPEG quantisation and sensor noise swamp any
# real detail; 250 leaves headroom below hard saturation.
SHADOW_LEVEL = 8
HIGHLIGHT_LEVEL = 250


@dataclass(frozen=True)
class ExposureReading:
    """How much light the frame actually received.

    Exists because the app happily shot eight frames into a black sensor
    without comment: with a darkfield stop in place and a daylight exposure
    still dialled in, the captures came back at a mean level of 1/255. Focus
    tools are useless on a frame with no signal in it, and so is the operator's
    eye on a live view the camera has quietly gained up.
    """

    mean: float
    shadow_fraction: float
    highlight_fraction: float

    @property
    def verdict(self) -> str:
        if self.shadow_fraction > 0.98 or self.mean < 3:
            return "no signal"
        if self.mean < 25:
            return "very dark"
        if self.highlight_fraction > 0.10:
            return "blown"
        if self.mean < 55:
            return "dark"
        if self.mean > 200:
            return "bright"
        return "ok"

    @property
    def is_usable(self) -> bool:
        """Whether there is enough signal for focus scoring to mean anything."""
        return self.verdict not in ("no signal", "very dark")

    @property
    def advice(self) -> str:
        return {
            "no signal": "Increase shutter time and ISO - darkfield needs seconds, not 1/125.",
            "very dark": "Increase shutter time or ISO.",
            "dark": "A longer shutter would give the focus meter more to work with.",
            "blown": "Highlights clipped - shorten the shutter or lower ISO.",
            "bright": "Close to clipping.",
            "ok": "",
        }[self.verdict]


def measure_exposure(gray: np.ndarray) -> ExposureReading:
    """Summarise the light level of a frame. Three cheap passes over the array."""
    if gray.size == 0:
        return ExposureReading(0.0, 1.0, 0.0)
    return ExposureReading(
        mean=float(gray.mean()),
        shadow_fraction=float((gray < SHADOW_LEVEL).mean()),
        highlight_fraction=float((gray >= HIGHLIGHT_LEVEL).mean()),
    )


@dataclass(frozen=True)
class FocusReading:
    """One frame's focus result, as handed to the UI."""

    value: float
    peak: float
    metric: str
    samples: int = 0

    @property
    def settled(self) -> bool:
        """Whether the held peak has seen enough frames to be a real reference."""
        return self.samples >= SETTLE_SAMPLES

    @property
    def fraction_of_peak(self) -> float:
        """Current score as a 0..1 fraction of the best seen since reset.

        This is what the UI shows as the headline: the absolute score is
        meaningless, but "you are at 94% of the best focus you have found"
        tells you whether to keep turning and which way you came from.

        Meaningless until `settled` -- check that before trusting it.
        """
        if self.peak <= 0.0:
            return 0.0
        return max(0.0, min(1.0, self.value / self.peak))


class FocusMeter:
    """Rolling focus history with peak-hold.

    Peak-hold is the point of the widget. Racking through focus produces a
    curve, and the peak marks where you should have stopped; without holding it
    you only ever see the instantaneous value and have to guess whether you are
    approaching or leaving best focus. The peak is decayed slowly rather than
    latched forever so that moving to a new field of view -- or swapping
    objectives on the turret, once that is motorised -- does not leave an
    unreachable target from the previous scene pinned on the display.
    """

    def __init__(self, history: int = 300, decay: float = 0.999) -> None:
        self._history: deque[float] = deque(maxlen=history)
        self._peak = 0.0
        self._decay = decay
        self._samples = 0

    def update(self, gray: np.ndarray, metric: str = LAPLACIAN) -> FocusReading:
        value = score(gray, metric)
        self._history.append(value)
        self._peak = max(value, self._peak * self._decay)
        self._samples += 1
        return FocusReading(
            value=value, peak=self._peak, metric=metric, samples=self._samples
        )

    def reset(self) -> None:
        """Drop the history and the held peak.

        Called on objective change, on preview restart, and from the UI button
        -- any point where scores from before and after are not comparable.
        """
        self._history.clear()
        self._peak = 0.0
        self._samples = 0

    @property
    def history(self) -> list[float]:
        return list(self._history)

    @property
    def peak(self) -> float:
        return self._peak
