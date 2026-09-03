"""Canon EOS 5D Mark II control over PTP, via libgphoto2.

Why gphoto2 and not Canon's own SDK: the EDSDK ships for Windows and macOS
only, so on NixOS libgphoto2 is the whole field. It supports the 5D Mk II's
PTP capture, configuration and -- the part this app is built around -- the
Canon live view stream, which arrives as a run of 1024x680 JPEGs (~100 KB each,
about 14 per second on this rig)
pulled one at a time.

Two hard constraints shape everything below.

libgphoto2 is not thread-safe. A `Camera` and its context belong to exactly one
thread; calling into them from two is not a race you will win. So the handle
lives on `CameraWorker`, which is moved onto its own QThread, and every entry
point is a slot. `CameraController` is the only thing the UI touches.

Pulling a preview frame blocks. Each `capture_preview` is a USB round trip of
tens of milliseconds, and the decode-and-analyse work that follows it is not
free either. All of it happens on the device thread; the GUI thread receives
a finished, immutable `PreviewFrame` and only has to paint.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, replace
from pathlib import Path

from PySide6.QtCore import QMetaObject, QRectF, Qt, QTimer, Signal, Slot
from PySide6.QtGui import QImage

from ..core import exif
from ..core import focus as focus_mod
from ..core import imaging
from ..core.focus import ExposureReading, FocusMeter, FocusReading
from .base import DeviceController, DeviceState, DeviceWorker

try:
    import gphoto2 as gp
except ImportError:  # pragma: no cover - environment problem, reported in UI
    gp = None

# Camera settings surfaced in the panel, in display order.
#
# Exposure mode comes first because it gates everything under it. In a Basic
# Zone position -- "Green" on the mode dial -- the body reports exactly one
# choice for ISO, shutter and white balance ("Auto"), so the panel fills with
# dead single-entry combo boxes and nothing explains why. Switching to Manual
# repopulates all three (20, 55 and 9 choices respectively on the 5D Mk II), and
# since set_setting re-reads the tree after every write, changing it here
# refills the rest of the panel immediately. Verified writable over PTP, so the
# trap is escapable from the app rather than only at the dial.
#
# The Basic Zone lockout hides widgets outright, not just their choices:
# `imageformat` does not appear in the config tree at all in Green mode and
# returns with 27 choices in Manual. So a widget being missing says nothing
# permanent about the body, and the reader skips absent ones and re-reads after
# every write rather than caching a fixed set.
#
# Aperture is deliberately absent. On the BH3 the body sits on the trinocular
# phototube with no electronic lens attached, so there is no iris for the camera
# to drive -- the aperture that matters is the objective's NA and the condenser
# diaphragm, both of which are set by hand on the microscope. libgphoto2 will
# happily show an `aperture` widget reading "implicit auto"; wiring a control to
# it would only invite you to change a number that does nothing.
INTERESTING_SETTINGS = (
    "autoexposuremode",
    "iso",
    "shutterspeed",
    "whitebalance",
    "imageformat",
)

# Give up on live view after this many consecutive failed grabs. Single dropped
# frames are normal (the body reshuffles live view on any settings change);
# a sustained run means the session is gone, usually an unplugged cable.
_MAX_CONSECUTIVE_FAILURES = 10

# Exponential moving average weight for the displayed frame rate. Raw
# per-frame deltas jitter far too much to read.
_FPS_SMOOTHING = 0.15

# Default live view pacing, in frames per second.
#
# Left unpaced, the loop pulls frames as fast as the body will produce them --
# about 22 fps on this rig -- and that costs roughly a full CPU core between
# the USB transfer, the JPEG decode and the repaint. Nothing about focusing
# needs 22 fps: the hand turning the knob is the slow part, and the sharpness
# trace is legible at a third of that. Pacing is by far the cheapest lever on
# power and heat, which matters because this runs on a tablet.
DEFAULT_TARGET_FPS = 15.0

# "As fast as the camera allows", for when latency matters more than power.
UNPACED = 0.0


@dataclass(frozen=True)
class ConfigChoice:
    """One enumerable camera setting as read from the PTP config tree."""

    name: str
    label: str
    value: str
    choices: tuple[str, ...]
    readonly: bool


@dataclass(frozen=True)
class PreviewFrame:
    """One fully-processed live view frame, ready to paint.

    Immutable, and every field is either a value or an implicitly-shared
    QImage, because this crosses from the device thread to the GUI thread.
    """

    image: QImage
    overlay: QImage | None
    # Where the overlay belongs, in image coordinates. The overlay is built at
    # analysis resolution (normally half the preview) and is inset by the
    # Sobel's invalid border, so it neither matches the frame's size nor sits
    # at its origin -- the view stretches it into this rectangle.
    overlay_rect: QRectF | None
    reading: FocusReading | None
    # Light level of the frame. Carried separately from the focus reading
    # because it stays meaningful when focus scoring does not -- a frame with
    # no signal in it has no sharpness to measure, and saying so is the whole
    # point.
    exposure: ExposureReading | None
    fps: float
    sequence: int


# Pixel dimensions behind the 5D Mk II's image-size names, so the settings
# panel can say what a format actually produces.
#
# libgphoto2 reports these as bare words -- "Large Fine JPEG", "Medium Normal
# JPEG" -- which say nothing about resolution. That is how five of eight frames
# in one session were shot at 11 MP instead of 21 without anyone noticing: the
# only visible difference between "Large" and "Medium" is the word.
#
# Large and Medium are measured from real captures off this body (5616x3744 and
# 4080x2720). The rest are Canon's published figures for the 5D Mk II and are
# unverified here, which is why an unrecognised name is left unannotated rather
# than guessed at -- a wrong number in the UI is worse than no number.
_IMAGE_SIZES = {
    "large": (5616, 3744),
    "medium": (4080, 2720),
    "small": (2784, 1856),
    "raw": (5616, 3744),
    "sraw1": (3861, 2574),
    "sraw2": (2784, 1856),
    "mraw": (3861, 2574),
}


def describe_image_format(name: str) -> str:
    """Annotate a camera image-format name with the resolution it produces.

    Returns the name unchanged when the size cannot be determined -- combined
    formats like "RAW + Large Fine JPEG" resolve on the JPEG half, since that
    is the file the app downloads and shows.
    """
    lowered = name.lower()
    # Check the JPEG half first: in "RAW + Large Fine JPEG" the JPEG size is
    # what lands on disk, and "raw" would otherwise match and report 21 MP for
    # a pairing whose JPEG is Medium.
    for key in ("small", "medium", "large", "sraw2", "sraw1", "mraw", "raw"):
        if key in lowered:
            width, height = _IMAGE_SIZES[key]
            megapixels = width * height / 1_000_000
            return f"{name}  -  {width}x{height} ({megapixels:.1f} MP)"
    return name


# Longest edge of the review image handed to the UI after a capture.
#
# The full frame is 5616x3744; as a QImage that is ~84 MB, and building one per
# shot on a tablet is not worth it. 2600 px still resolves far more than the
# 1024 px live view, so it answers the question the review step exists for --
# is this frame sharp and exposed, keep or bin it -- while staying cheap enough
# to appear instantly.
REVIEW_MAX_EDGE = 2600


@dataclass(frozen=True)
class CaptureResult:
    """A downloaded still, awaiting a keep-or-discard decision.

    Carries the camera-side location as well as the local path so that
    discarding can remove both copies -- otherwise rejected frames silently
    accumulate on the CF card.
    """

    local_path: str
    camera_folder: str
    camera_name: str
    review: QImage
    # Exposure of the *captured* frame, not of the preview that preceded it.
    #
    # This is the only exposure figure that can be trusted. Canon's live view
    # gains its display up to stay viewable, and only simulates the real
    # exposure within a limited range -- push several stops past that and the
    # preview still looks perfectly normal while the sensor is receiving
    # essentially nothing. That is not hypothetical: a session here produced
    # three darkfield frames averaging 1/255 from a live view that "looked
    # fine", which is exactly how a preview-based warning fails.
    exposure: ExposureReading | None


@dataclass(frozen=True)
class FocusConfig:
    """Focus-aid settings, owned by the UI and pushed to the worker.

    Held as one immutable object rather than as separate slots so that the
    worker can never see a half-applied combination -- e.g. peaking switched on
    while the sensitivity is still the previous value.
    """

    metric: str = focus_mod.LAPLACIAN
    roi_fraction: float = 0.4
    peaking: bool = False
    sensitivity: int = 50
    color: tuple[int, int, int] = (255, 48, 48)
    analysis_enabled: bool = True
    # Fraction of preview resolution used for scoring and peaking. Half means
    # a quarter of the pixels and, measured on the tablet, the whole per-frame
    # analysis dropping from 52 ms to 10 ms -- the difference between analysis
    # being the bottleneck and it being nearly free. Correlation with the
    # full-resolution focus score is 0.99.
    analysis_scale: float = 0.5


def _error_message(err: Exception) -> str:
    """Translate a libgphoto2 error into something actionable.

    The raw strings are unhelpful at exactly the moments they matter most. On
    NixOS the overwhelmingly common failure is the gvfs volume monitor grabbing
    the camera the instant it enumerates, which surfaces as a bare "Could not
    claim the USB device" with no hint as to who took it.
    """
    if gp is None:
        return "python-gphoto2 is not installed (see README: nix develop)."

    code = getattr(err, "code", None)
    if code == getattr(gp, "GP_ERROR_MODEL_NOT_FOUND", -105):
        return (
            "No camera found. Check the USB cable, that the body is powered on "
            "and awake, and that the mode dial is not on a locked position."
        )
    if code == getattr(gp, "GP_ERROR_IO_USB_CLAIM", -53):
        return (
            "The camera is claimed by another process -- almost always the gvfs "
            "photo mount. Close any file manager showing the camera, then:\n"
            "    pkill -f gvfs-gphoto2-volume-monitor"
        )
    if code == getattr(gp, "GP_ERROR_IO_LOCK", -60):
        return "The camera is locked by another gphoto2 session; close it and retry."
    if code == getattr(gp, "GP_ERROR_NOT_SUPPORTED", -6):
        return "The camera rejected that operation as unsupported."
    return str(err)


class CameraWorker(DeviceWorker):
    """Owns the libgphoto2 handle. Every method here runs on the device thread."""

    previewFrame = Signal(object)  # PreviewFrame
    settingsRead = Signal(object)  # dict[str, ConfigChoice]
    captureComplete = Signal(object)  # CaptureResult
    captureDiscarded = Signal(str)  # local path that was removed
    previewStopped = Signal()

    def __init__(self) -> None:
        super().__init__()
        self._camera = None
        self._model = ""
        self._timer: QTimer | None = None
        self._meter = FocusMeter()
        self._focus = FocusConfig()
        self._sequence = 0
        self._fps = 0.0
        self._last_frame_at = 0.0
        self._failures = 0
        self._target_fps = DEFAULT_TARGET_FPS

    # -- thread lifecycle -------------------------------------------------

    @Slot()
    def on_thread_started(self) -> None:
        """Create thread-affine objects.

        The QTimer must be constructed here rather than in __init__: a QTimer
        belongs to the thread that created it, and one created on the GUI
        thread would never fire on this one.
        """
        self._timer = QTimer()
        # Single-shot, re-armed after every frame with whatever remains of the
        # target period. A repeating zero-interval timer would grab flat out;
        # this idles the thread for the leftover time instead, and going back
        # through the event loop between frames is what lets a stop request or
        # a settings change be serviced promptly rather than after a burst.
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._grab_frame)

    # -- connection -------------------------------------------------------

    @Slot()
    def open(self) -> None:
        if gp is None:
            self._fail(_error_message(ImportError()))
            return
        if self._camera is not None:
            return

        self._set_state(DeviceState.CONNECTING, "Opening camera...")
        camera = None
        try:
            camera = gp.Camera()
            camera.init()
        except Exception as err:  # gp.GPhoto2Error, plus anything the driver throws
            # Release the USB claim before dropping the handle. init() can fail
            # *after* it has claimed interface 0 -- a refused Canon handshake,
            # a body that sleeps mid-negotiation -- and simply letting the
            # object go does not hand the interface back. The next attempt then
            # fails with -53 "Could not claim the USB device", against a claim
            # this very process is holding.
            #
            # Harmless when connect was a one-shot at startup. Fatal with a
            # retry loop: every attempt leaks another claim and guarantees the
            # next one fails, so the app sits at "Opening camera..." forever
            # while holding the device it says it cannot open.
            if camera is not None:
                try:
                    camera.exit()
                except Exception:
                    pass
            self._camera = None
            self._fail(_error_message(err))
            return

        self._camera = camera
        try:
            self._model = camera.get_abilities().model
        except Exception:
            # Cosmetic only -- a body that connects but will not report its
            # abilities is still perfectly usable.
            self._model = "Unknown camera"

        self._keep_awake()
        self._set_state(DeviceState.READY, self._model)
        self._read_settings()

    def _keep_awake(self) -> None:
        """Stop the body powering itself off while we hold the session.

        The 5D Mk II ships with a 60 second idle timeout, and when it fires the
        camera does not merely sleep -- it drops off the USB bus entirely,
        taking the PTP session with it and leaving the app reporting "no camera
        found" for what looks like a cable fault. Long focusing sessions on a
        microscope are exactly the workload that trips it, since minutes can
        pass at the knob with no PTP traffic at all.

        The write is session-scoped: the camera reverts to its menu setting
        once the session closes, so this is not a persistent change to the
        user's body, and it has to be redone on every connect. Best effort --
        a body that does not expose the widget is no reason to refuse to run.
        """
        try:
            config = self._camera.get_config()
            config.get_child_by_name("autopoweroff").set_value("0")
            self._camera.set_config(config)
        except Exception:
            pass

    @Slot()
    def close(self) -> None:
        self.stop_preview()
        if self._camera is not None:
            try:
                self._camera.exit()
            except Exception:
                # Already gone (cable pulled, body slept). Nothing useful to do
                # and nothing worth alarming the user about on the way out.
                pass
            self._camera = None
        self._model = ""
        self._set_state(DeviceState.DISCONNECTED, "")

    # -- settings ---------------------------------------------------------

    def _read_settings(self) -> None:
        if self._camera is None:
            return
        try:
            config = self._camera.get_config()
        except Exception as err:
            self.error.emit(_error_message(err))
            return

        found: dict[str, ConfigChoice] = {}
        for name in INTERESTING_SETTINGS:
            try:
                widget = config.get_child_by_name(name)
            except Exception:
                # Not every body exposes every widget; absence is normal.
                continue
            try:
                choices = tuple(
                    str(widget.get_choice(i)) for i in range(widget.count_choices())
                )
                found[name] = ConfigChoice(
                    name=name,
                    label=str(widget.get_label() or name),
                    value=str(widget.get_value()),
                    choices=choices,
                    readonly=bool(widget.get_readonly()),
                )
            except Exception:
                continue
        self.settingsRead.emit(found)

    @Slot(str, str)
    def set_setting(self, name: str, value: str) -> None:
        """Write one config widget.

        libgphoto2 has no per-widget write: you fetch the tree, mutate a node
        and push the whole tree back. Re-fetching each time rather than caching
        matters because the camera invalidates choice lists on its own -- the
        available shutter speeds shift when live view engages.
        """
        if self._camera is None:
            return
        try:
            config = self._camera.get_config()
            widget = config.get_child_by_name(name)
            widget.set_value(value)
            self._camera.set_config(config)
        except Exception as err:
            self.error.emit(f"Could not set {name} to {value}: {_error_message(err)}")
        # Re-read regardless: a rejected write leaves the UI showing a value the
        # camera never accepted, and one accepted write can move others.
        self._read_settings()

    @Slot()
    def refresh_settings(self) -> None:
        self._read_settings()

    @Slot(object)
    def set_focus_config(self, config: FocusConfig) -> None:
        # A metric change makes previously-recorded scores incomparable, so the
        # held peak has to go with it or the bar reads against a target from a
        # different scale entirely.
        if config.metric != self._focus.metric:
            self._meter.reset()
        self._focus = config

    @Slot()
    def reset_focus_peak(self) -> None:
        self._meter.reset()

    # -- live view --------------------------------------------------------

    @Slot()
    def start_preview(self) -> None:
        if self._camera is None or self._timer is None:
            return

        # Canon bodies need live view explicitly armed before capture_preview
        # returns frames; this is what flips the mirror up. Some driver versions
        # arm it implicitly, so a failure here is not fatal -- the first grab
        # will tell us for real.
        try:
            config = self._camera.get_config()
            viewfinder = config.get_child_by_name("viewfinder")
            viewfinder.set_value(1)
            self._camera.set_config(config)
        except Exception:
            pass

        self._failures = 0
        self._sequence = 0
        self._fps = 0.0
        self._last_frame_at = 0.0
        self._meter.reset()
        self._timer.start(0)
        self._set_state(DeviceState.READY, f"{self._model} - live view")

    @Slot()
    def stop_preview(self) -> None:
        if self._timer is not None:
            self._timer.stop()
        if self._camera is not None:
            # Drop the mirror again. Leaving live view armed keeps the sensor
            # powered and warming, and sensor heat is read noise -- which is
            # exactly what you do not want on long microscope exposures.
            try:
                config = self._camera.get_config()
                viewfinder = config.get_child_by_name("viewfinder")
                viewfinder.set_value(0)
                self._camera.set_config(config)
            except Exception:
                pass
        self.previewStopped.emit()

    @Slot(float)
    def set_target_fps(self, fps: float) -> None:
        """Cap the live view rate. 0 means as fast as the camera allows."""
        self._target_fps = max(0.0, fps)

    def _rearm(self, started_at: float) -> None:
        """Schedule the next grab, leaving the thread idle for the remainder.

        Measured from the *start* of this frame so the pacing tracks the target
        period rather than drifting by however long the frame happened to take.
        """
        if self._timer is None or self._camera is None:
            return
        if self._target_fps <= 0:
            self._timer.start(0)
            return
        period_ms = 1000.0 / self._target_fps
        elapsed_ms = (time.monotonic() - started_at) * 1000.0
        self._timer.start(max(0, int(period_ms - elapsed_ms)))

    def _grab_frame(self) -> None:
        if self._camera is None:
            return
        started_at = time.monotonic()
        try:
            camera_file = self._camera.capture_preview()
            data = bytes(camera_file.get_data_and_size())
        except Exception as err:
            self._failures += 1
            if self._failures >= _MAX_CONSECUTIVE_FAILURES:
                self.stop_preview()
                self._fail(f"Live view stopped: {_error_message(err)}")
                return
            self._rearm(started_at)
            return

        image = imaging.decode_preview(data)
        if image is None:
            self._failures += 1
            self._rearm(started_at)
            return
        self._failures = 0

        overlay, overlay_rect, reading, exposure = self._analyse(image)

        now = time.monotonic()
        if self._last_frame_at:
            delta = now - self._last_frame_at
            if delta > 0:
                instant = 1.0 / delta
                self._fps = (
                    instant
                    if self._fps == 0.0
                    else self._fps + _FPS_SMOOTHING * (instant - self._fps)
                )
        self._last_frame_at = now
        self._sequence += 1

        self._rearm(started_at)
        self.previewFrame.emit(
            PreviewFrame(
                image=image,
                overlay=overlay,
                overlay_rect=overlay_rect,
                reading=reading,
                exposure=exposure,
                fps=self._fps,
                sequence=self._sequence,
            )
        )

    def _analyse(
        self, image: QImage
    ) -> tuple[QImage | None, QRectF | None, FocusReading | None, ExposureReading | None]:
        """Score focus and build the peaking overlay for one frame.

        Runs at `analysis_scale` rather than full preview resolution -- see
        FocusConfig for the measurements. Skippable entirely via
        `analysis_enabled` when frame rate matters more than the numbers.
        """
        if not self._focus.analysis_enabled:
            return None, None, None, None

        scale = self._focus.analysis_scale
        gray = imaging.analysis_gray(image, scale)
        if gray.size == 0:
            return None, None, None, None

        roi = imaging.centre_roi(gray, self._focus.roi_fraction)
        # Exposure is measured over the ROI, not the frame: on a microscope the
        # surround outside the field stop is black by construction, and
        # including it would report every correctly-exposed frame as dark.
        exposure = focus_mod.measure_exposure(roi)
        reading = self._meter.update(roi, self._focus.metric)

        overlay, rect = None, None
        if self._focus.peaking:
            magnitude = focus_mod.gradient_l1(gray)
            overlay = imaging.peaking_overlay(
                magnitude,
                self._focus.sensitivity,
                self._focus.color,
                self._focus.roi_fraction,
            )
            if overlay is not None:
                # One analysis pixel spans 1/scale image pixels, and the
                # overlay lost one of them to the Sobel border on every side.
                inset = 1.0 / scale if scale > 0 else 1.0
                rect = QRectF(
                    inset,
                    inset,
                    image.width() - 2 * inset,
                    image.height() - 2 * inset,
                )
        return overlay, rect, reading, exposure

    # -- stills -----------------------------------------------------------

    @Slot(str)
    def capture(self, directory: str) -> None:
        """Take a full-resolution frame and download it.

        Live view is dropped first and restored after. The Canon driver can
        capture with the mirror up, but doing so leaves the body in a state
        where the next few preview grabs fail, and the resulting stutter looks
        like a bug. Stopping cleanly costs about a second and always works.

        This is intentionally minimal -- v1 is about focusing, and structured
        session capture arrives with the stage, when a filename finally has
        coordinates worth recording.
        """
        if self._camera is None:
            return

        was_previewing = self._timer is not None and self._timer.isActive()
        if was_previewing:
            self.stop_preview()

        self._set_state(DeviceState.BUSY, "Capturing...")
        try:
            path = self._camera.capture(gp.GP_CAPTURE_IMAGE)
            target = Path(directory).expanduser()
            target.mkdir(parents=True, exist_ok=True)
            destination = target / path.name

            # Never silently clobber: a session's frames are the whole point of
            # the exercise and the camera restarts its own numbering at 0001.
            stem, suffix, index = destination.stem, destination.suffix, 1
            while destination.exists():
                destination = target / f"{stem}_{index:03d}{suffix}"
                index += 1

            self._set_state(DeviceState.BUSY, f"Downloading {path.name}...")
            camera_file = self._camera.file_get(
                path.folder, path.name, gp.GP_FILE_TYPE_NORMAL
            )
            camera_file.save(str(destination))

            # The body tags every frame from this rig as rotated, because its
            # orientation sensor is reading gravity while the camera points
            # straight down the phototube -- there is no lateral component to
            # resolve, so it guesses. Viewers honour the tag and a landscape
            # 5616x3744 frame is displayed on its side. See core/exif.py.
            was = exif.normalise_orientation(destination)
            if was is not None:
                self._set_state(
                    DeviceState.BUSY, f"Corrected orientation tag on {destination.name}"
                )

            # Decode here rather than in the UI: the bytes are already in hand,
            # and a 21 MP JPEG takes long enough to decode that doing it on the
            # GUI thread would visibly stall the window.
            review = QImage()
            try:
                review.loadFromData(bytes(camera_file.get_data_and_size()))
                if max(review.width(), review.height()) > REVIEW_MAX_EDGE:
                    review = review.scaled(
                        REVIEW_MAX_EDGE,
                        REVIEW_MAX_EDGE,
                        Qt.AspectRatioMode.KeepAspectRatio,
                        Qt.TransformationMode.SmoothTransformation,
                    )
            except Exception:
                # A RAW-only shot has no embedded preview we can decode here.
                # The file is still saved; review just has nothing to show.
                review = QImage()

            captured_exposure = None
            if not review.isNull():
                gray = imaging.to_gray(review)
                captured_exposure = focus_mod.measure_exposure(
                    imaging.centre_roi(gray, 0.6)
                )

            self.captureComplete.emit(
                CaptureResult(
                    local_path=str(destination),
                    camera_folder=path.folder,
                    camera_name=path.name,
                    review=review,
                    exposure=captured_exposure,
                )
            )
            self._set_state(DeviceState.READY, f"Saved {destination.name}")
        except Exception as err:
            self._fail(f"Capture failed: {_error_message(err)}")
        finally:
            if was_previewing:
                self.start_preview()

    @Slot(str, str, str)
    def discard(self, local_path: str, camera_folder: str, camera_name: str) -> None:
        """Delete a rejected frame from disk and from the camera's card.

        Both copies, because the camera keeps its own regardless of the
        download -- delete only the local file and the card fills up with
        frames you already decided against.
        """
        removed = False
        try:
            Path(local_path).unlink(missing_ok=True)
            removed = True
        except Exception as err:
            self.error.emit(f"Could not delete {local_path}: {err}")

        if camera_folder and camera_name and self._camera is not None:
            try:
                self._camera.file_delete(camera_folder, camera_name)
            except Exception as err:
                # Not fatal: the local copy is what the user asked about, and
                # some bodies refuse deletion while the card is write-protected.
                self.error.emit(
                    f"Removed locally, but the camera kept {camera_name}: "
                    f"{_error_message(err)}"
                )
        if removed:
            self.captureDiscarded.emit(local_path)


class CameraController(DeviceController):
    """GUI-thread face of the camera. UI panels bind to this and nothing else.

    Requests go out as signals rather than direct calls so Qt queues them onto
    the device thread; a click returns immediately even though the USB
    transaction behind it takes 50 ms.
    """

    previewFrame = Signal(object)
    settingsRead = Signal(object)
    captureComplete = Signal(object)
    captureDiscarded = Signal(str)
    previewStopped = Signal()

    _requestOpen = Signal()
    _requestClose = Signal()
    _requestStartPreview = Signal()
    _requestStopPreview = Signal()
    _requestSetting = Signal(str, str)
    _requestRefresh = Signal()
    _requestCapture = Signal(str)
    _requestFocusConfig = Signal(object)
    _requestResetPeak = Signal()
    _requestTargetFps = Signal(float)
    _requestDiscard = Signal(str, str, str)

    def __init__(self) -> None:
        worker = CameraWorker()
        super().__init__(worker, thread_name="camera")
        self._camera_worker = worker
        self._focus = FocusConfig()
        self._previewing = False

        worker.previewFrame.connect(self.previewFrame)
        worker.settingsRead.connect(self.settingsRead)
        worker.captureComplete.connect(self.captureComplete)
        worker.captureDiscarded.connect(self.captureDiscarded)
        worker.previewStopped.connect(self.previewStopped)
        worker.previewStopped.connect(self._on_preview_stopped)

        self._thread.started.connect(worker.on_thread_started)
        self._requestOpen.connect(worker.open)
        self._requestClose.connect(worker.close)
        self._requestStartPreview.connect(worker.start_preview)
        self._requestStopPreview.connect(worker.stop_preview)
        self._requestSetting.connect(worker.set_setting)
        self._requestRefresh.connect(worker.refresh_settings)
        self._requestCapture.connect(worker.capture)
        self._requestFocusConfig.connect(worker.set_focus_config)
        self._requestResetPeak.connect(worker.reset_focus_peak)
        self._requestTargetFps.connect(worker.set_target_fps)
        self._requestDiscard.connect(worker.discard)

        self._start_worker()

    @property
    def previewing(self) -> bool:
        return self._previewing

    @property
    def focus_config(self) -> FocusConfig:
        return self._focus

    def open(self) -> None:
        self._requestOpen.emit()

    def close(self) -> None:
        self._previewing = False
        self._requestClose.emit()

    def start_preview(self) -> None:
        self._previewing = True
        self._requestStartPreview.emit()

    def stop_preview(self) -> None:
        self._previewing = False
        self._requestStopPreview.emit()

    def _on_preview_stopped(self) -> None:
        self._previewing = False

    def set_setting(self, name: str, value: str) -> None:
        self._requestSetting.emit(name, value)

    def refresh_settings(self) -> None:
        self._requestRefresh.emit()

    def capture(self, directory: str) -> None:
        self._requestCapture.emit(directory)

    def update_focus(self, **changes) -> FocusConfig:
        """Patch the focus settings and push the whole object to the worker."""
        self._focus = replace(self._focus, **changes)
        self._requestFocusConfig.emit(self._focus)
        return self._focus

    def reset_focus_peak(self) -> None:
        self._requestResetPeak.emit()

    def set_target_fps(self, fps: float) -> None:
        """Cap the live view rate; 0 (UNPACED) means as fast as the body allows."""
        self._requestTargetFps.emit(float(fps))

    def discard(self, result: CaptureResult) -> None:
        self._requestDiscard.emit(
            result.local_path, result.camera_folder, result.camera_name
        )

    def shutdown(self) -> None:
        # Ask the worker to close first, so the mirror comes down and the USB
        # session ends properly rather than the thread being stopped with the
        # camera still open.
        #
        # Queued, not BlockingQueued. Blocking would make the GUI thread wait
        # with no timeout for a slot that has to queue behind whatever the
        # worker is currently doing -- and when the camera has been unplugged
        # or has powered itself off mid-read, that in-flight libgphoto2 call
        # can sit on a dead USB endpoint for a long time. A body that drops off
        # the bus by itself is not hypothetical here: this one auto-powers-off
        # and takes the session with it. Blocking turns that into an
        # unkillable window on exit, so the close is posted and the bounded
        # wait in DeviceController.shutdown() sets the deadline instead.
        if self._thread.isRunning():
            QMetaObject.invokeMethod(
                self._camera_worker, "close", Qt.ConnectionType.QueuedConnection
            )
        super().shutdown()
