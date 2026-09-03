"""Tests for the exposure readout.

This exists because of a real failure: eight frames were captured through the
app at a daylight exposure, three of them with a darkfield stop in place, and
the app said nothing. The darkfield frames came back at a mean level of about
1/255 -- no signal whatsoever -- and nothing in the UI had distinguished them
from a correctly exposed shot beforehand.

The thresholds are deliberately coarse. The readout only has to answer "is
there enough light here to be worth pressing the shutter", which is a decision
with wide margins, not a light meter.
"""

from __future__ import annotations

import numpy as np
import pytest

from microscope_control.core.focus import measure_exposure


def flat(level: int, shape: tuple[int, int] = (120, 160)) -> np.ndarray:
    return np.full(shape, level, dtype=np.uint8)


@pytest.mark.parametrize(
    "level, expected",
    [(0, "no signal"), (1, "no signal"), (15, "very dark"), (40, "dark"),
     (110, "ok"), (180, "ok"), (220, "bright")],
)
def test_verdict_tracks_level(level: int, expected: str) -> None:
    assert measure_exposure(flat(level)).verdict == expected


def test_darkfield_capture_is_reported_as_no_signal() -> None:
    """The exact failure that motivated this: mean ~1, peak ~10, all shadow."""
    rng = np.random.default_rng(0)
    frame = np.clip(rng.normal(1.0, 1.5, (120, 160)), 0, 10).astype(np.uint8)
    reading = measure_exposure(frame)
    assert reading.verdict == "no signal"
    assert not reading.is_usable
    assert "shutter" in reading.advice.lower()


def test_clipping_is_caught_even_when_the_mean_looks_reasonable() -> None:
    """A frame can average mid-grey while a large area is blown out."""
    frame = flat(20)
    frame[:, :40] = 255  # a quarter of the frame at saturation
    reading = measure_exposure(frame)
    assert reading.highlight_fraction > 0.2
    assert reading.verdict == "blown"


def test_a_well_exposed_frame_offers_no_advice() -> None:
    reading = measure_exposure(flat(120))
    assert reading.verdict == "ok"
    assert reading.is_usable
    assert reading.advice == ""


def test_empty_input_does_not_raise() -> None:
    reading = measure_exposure(np.zeros((0, 0), dtype=np.uint8))
    assert reading.verdict == "no signal"
