"""Tests for the focus-peaking overlay.

These drive `focus.gradient_l1` -- the raw integer Sobel strength the live path
actually uses -- rather than the tidier normalised magnitude, so the thresholds
under test are the ones that run on every frame.

The property under test is the one the overlay exists to provide: it must paint
*less* as the image defocuses. That sounds too obvious to test, but two earlier
implementations of this threshold both got it backwards while looking entirely
reasonable -- a gradient percentile, and a noise-proportional level -- because
blurring conserves edge energy while spreading it, so a low threshold catches
more of a soft wide ramp than of a hard thin edge. Nothing about the overlay
*looks* wrong when that happens; it just quietly tells you the opposite of the
truth. Hence a test.
"""

from __future__ import annotations

import numpy as np
import pytest

from microscope_control.core import focus, imaging

HEIGHT, WIDTH = 480, 640


def clean_field() -> np.ndarray:
    """A brightfield-like scene: circular field stop, blobs with darker nuclei."""
    yy, xx = np.ogrid[:HEIGHT, :WIDTH]
    field = np.full((HEIGHT, WIDTH), 6.0, dtype=np.float32)
    stop = (yy - HEIGHT / 2) ** 2 + (xx - WIDTH / 2) ** 2 < 200**2
    field[stop] = 205.0
    rng = np.random.default_rng(7)
    for _ in range(30):
        angle = rng.uniform(0, 2 * np.pi)
        radius = 200 * np.sqrt(rng.uniform(0, 0.85))
        cy = HEIGHT / 2 + radius * np.sin(angle)
        cx = WIDTH / 2 + radius * np.cos(angle)
        size = rng.uniform(10, 22)
        disc = ((yy - cy) ** 2 + (xx - cx) ** 2) < size * size
        field[disc & stop] = rng.uniform(105, 150)
        nucleus = ((yy - cy) ** 2 + (xx - cx) ** 2) < (size * 0.42) ** 2
        field[nucleus & stop] = rng.uniform(38, 62)
    return field


def optical_blur(field: np.ndarray, passes: int) -> np.ndarray:
    out = field.copy()
    for _ in range(passes):
        p = np.pad(out, 1, mode="edge")
        out = (p[:-2, 1:-1] + p[2:, 1:-1] + p[1:-1, :-2] + p[1:-1, 2:] + 4 * out) / 8.0
    return out


def capture(passes: int, sigma: float) -> np.ndarray:
    """Blur the scene, *then* add sensor noise -- the physical order.

    Getting this backwards makes any test of this code meaningless: blurring an
    already-noisy frame removes the noise, which is not something defocusing a
    microscope does.
    """
    rng = np.random.default_rng(11)
    noisy = optical_blur(clean_field(), passes) + rng.normal(
        0, sigma, (HEIGHT, WIDTH)
    )
    return np.clip(noisy, 0, 255).astype(np.uint8)


def painted_fraction(image: np.ndarray, sensitivity: int) -> float:
    overlay = imaging.peaking_overlay(
        focus.gradient_l1(image), sensitivity, (255, 48, 48), 0.4
    )
    if overlay is None:
        return 0.0
    raw = np.frombuffer(
        overlay.constBits(),
        dtype=np.uint8,
        count=overlay.bytesPerLine() * overlay.height(),
    ).reshape(overlay.height(), overlay.bytesPerLine())
    alpha = raw[:, 3 : overlay.width() * 4 : 4]
    return float((alpha > 0).mean())


@pytest.mark.parametrize("sensitivity", [0, 25, 50, 75, 100])
@pytest.mark.parametrize("sigma", [2.2, 7.0])
def test_peaking_recedes_as_focus_is_lost(sensitivity: int, sigma: float) -> None:
    """Painted area must fall away as the scene defocuses, at every setting.

    Stated as "clear falloff end to end, no meaningful climb along the way"
    rather than as strict monotonicity: the step from sharp to slightly-soft
    can wobble by a few hundredths of a percentage point, and pinning that
    exactly would be fitting the test to sampling noise. What matters to
    someone turning the knob is that heavy defocus is obviously emptier than
    focus, and that the overlay never *grows* as things get worse.

    This is the test that pins the loose end of the sensitivity slider. Below
    roughly level 20 the ratio asserted here climbs past 1.0 and the overlay
    inverts -- so if this fails after a threshold change, the threshold is
    wrong, not the test.
    """
    sharp, soft, softest = (
        painted_fraction(capture(p, sigma), sensitivity) for p in (0, 6, 24)
    )
    assert softest < 0.7 * sharp, (sharp, soft, softest)
    # No step may climb by more than a hair relative to the previous one.
    assert soft <= sharp * 1.05, (sharp, soft)
    assert softest <= soft * 1.05, (soft, softest)


@pytest.mark.parametrize("sigma", [2.2, 7.0])
def test_sensitivity_widens_selection(sigma: float) -> None:
    """The slider has to do something, in the direction advertised."""
    sharp = capture(0, sigma)
    assert painted_fraction(sharp, 0) < painted_fraction(sharp, 100)


def test_noise_guard_suppresses_grain_on_a_blank_field() -> None:
    """A featureless noisy frame must not be painted as if it had detail.

    This is the failure the noise guard exists for: at high ISO an empty field
    is pure grain, and grain has plenty of local gradient.
    """
    rng = np.random.default_rng(3)
    flat = np.clip(np.full((HEIGHT, WIDTH), 128.0) + rng.normal(0, 9.0, (HEIGHT, WIDTH)), 0, 255)
    assert painted_fraction(flat.astype(np.uint8), 100) < 0.01


def test_overlay_is_inset_by_the_sobel_border() -> None:
    """The view composites at (1, 1) assuming exactly this size."""
    overlay = imaging.peaking_overlay(
        focus.gradient_l1(capture(0, 2.2)), 50, (255, 48, 48), 0.4
    )
    assert overlay is not None
    assert (overlay.width(), overlay.height()) == (WIDTH - 2, HEIGHT - 2)
