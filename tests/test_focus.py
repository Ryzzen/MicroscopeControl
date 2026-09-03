"""Tests for the focus metrics.

Deliberately Qt-free: `focus` imports nothing but numpy, so these run without a
display or a QApplication. That separation is the reason the scoring lives apart
from `imaging`, and it is what will let the same metrics be driven headlessly by
a Z-stage autofocus sweep later.

The properties asserted here are the ones the UI actually relies on. If any of
them breaks, the focus readout becomes confidently wrong rather than obviously
broken, which is the worst failure mode for an instrument you are trusting to
tell you when to stop turning the knob.
"""

from __future__ import annotations

import numpy as np
import pytest

from microscope_control.core import focus


def synthetic_field() -> np.ndarray:
    """A stand-in for a brightfield specimen: hard-edged blobs on a dim ground."""
    field = np.full((480, 640), 20.0, dtype=np.float32)
    height, width = field.shape
    yy, xx = np.ogrid[:height, :width]
    for cy, cx, r in ((120, 160, 40), (300, 420, 70), (200, 300, 25), (380, 120, 55)):
        field[(yy - cy) ** 2 + (xx - cx) ** 2 < r * r] = 200.0
    return field


def defocus(field: np.ndarray, passes: int) -> np.ndarray:
    """Approximate defocus by repeated smoothing."""
    out = field.copy()
    for _ in range(passes):
        p = np.pad(out, 1, mode="edge")
        out = (p[:-2, 1:-1] + p[2:, 1:-1] + p[1:-1, :-2] + p[1:-1, 2:] + 4 * out) / 8.0
    return out


@pytest.mark.parametrize("metric", focus.METRICS)
def test_score_decreases_with_defocus(metric: str) -> None:
    """The core property: sharper must score higher, at every step."""
    field = synthetic_field()
    scores = [
        focus.score(defocus(field, n).astype(np.uint8), metric) for n in (0, 2, 8, 24)
    ]
    assert scores == sorted(scores, reverse=True), scores


@pytest.mark.parametrize("metric", focus.METRICS)
def test_score_is_illumination_invariant(metric: str) -> None:
    """Halving the LED must not read as a focus change.

    Both metrics are quadratic in intensity and are normalised by mean squared,
    so a uniform brightness scale should cancel out. Without this the meter
    would chase the illumination, which becomes a live hazard once the LED is
    under software control.
    """
    field = synthetic_field()
    bright = focus.score(field.astype(np.uint8), metric)
    dim = focus.score((field * 0.5).astype(np.uint8), metric)
    assert bright > 0
    assert dim / bright == pytest.approx(1.0, abs=0.15)


def test_meter_reports_unsettled_until_it_has_a_range() -> None:
    """The first frame is trivially its own peak; the meter must not claim focus."""
    meter = focus.FocusMeter()
    soft = defocus(synthetic_field(), 20).astype(np.uint8)

    first = meter.update(soft, focus.LAPLACIAN)
    assert not first.settled
    # It still reports 100% of peak -- which is exactly why `settled` exists.
    assert first.fraction_of_peak == pytest.approx(1.0)

    for _ in range(focus.SETTLE_SAMPLES):
        reading = meter.update(soft, focus.LAPLACIAN)
    assert reading.settled


def test_meter_holds_peak_through_a_rack() -> None:
    """Racking past best focus must leave the peak behind as a reference."""
    field = synthetic_field()
    meter = focus.FocusMeter()
    for _ in range(focus.SETTLE_SAMPLES + 1):
        meter.update(defocus(field, 20).astype(np.uint8), focus.LAPLACIAN)

    at_best = meter.update(field.astype(np.uint8), focus.LAPLACIAN)
    assert at_best.fraction_of_peak == pytest.approx(1.0)

    past_best = meter.update(defocus(field, 8).astype(np.uint8), focus.LAPLACIAN)
    assert past_best.fraction_of_peak < 0.2
    assert past_best.peak == pytest.approx(at_best.peak, rel=0.01)


def test_reset_clears_peak_and_settling() -> None:
    """Reset must undo settling too, or a stale peak survives an objective change."""
    meter = focus.FocusMeter()
    field = synthetic_field().astype(np.uint8)
    for _ in range(focus.SETTLE_SAMPLES + 5):
        meter.update(field, focus.LAPLACIAN)
    assert meter.peak > 0

    meter.reset()
    assert meter.peak == 0.0
    assert meter.history == []
    assert not meter.update(field, focus.LAPLACIAN).settled


def test_score_handles_degenerate_input() -> None:
    """Empty and sub-kernel ROIs must return 0.0, not raise.

    Reachable in practice: the ROI slider bottoms out small, and a preview frame
    arrives truncated occasionally while the body reshuffles live view.
    """
    for shape in ((0, 0), (1, 1), (2, 2)):
        for metric in focus.METRICS:
            assert focus.score(np.zeros(shape, dtype=np.uint8), metric) == 0.0
