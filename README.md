# MicroscopeControl

Control software for an Olympus BH3 with a Canon EOS 5D Mark II on the
trinocular phototube.

**Status: v1 — camera only.** Live view with focus aids is the working feature;
exposure control and single-shot capture are along for the ride because live
view is not usable without them. The stage, nosepiece, illumination and fault
injection are not implemented — the device layer is shaped for them, nothing
more.

## Why it exists

Focusing a microscope through a tethered DSLR is genuinely awkward. The BH3's
fine-focus knob is manual, the camera has no lens of its own to autofocus, and
the live view preview arrives at 1024×680 regardless of your monitor. So
the app scores every preview frame for sharpness and plots the trend: you turn
the knob and watch for the peak rather than squinting at a soft image.

## Hardware

| Part | Notes |
|---|---|
| Olympus BH3 | Trinocular phototube; focus and condenser are manual |
| Canon EOS 5D Mark II | Body only on the phototube — **no lens, so no aperture control** |
| Link | USB, PTP, driven by libgphoto2 |

Canon's own EDSDK is Windows/macOS only, so libgphoto2 is the whole field on
Linux. It handles the 5D Mk II's PTP capture, configuration and live view.
Measured on this rig: connect in ~0.4 s, live view at 1024×680 and ~14 fps.

## Running it

```bash
nix develop            # dev shell, runs from source
python -m microscope_control

nix run .              # or build and run the packaged app
```

## NixOS setup

Two things bite before anything works, both independent of this app.

**1. Device permissions.** libgphoto2 ships udev rules, but they set
`GROUP="camera"` and NixOS does not create that group — so the rules land on a
device node that stays `root:root`. Granting the logged-in seat user access via
logind is simpler and needs no group management. In `nix-config`:

```nix
services.udev.extraRules = ''
  # Canon (04a9) over PTP: hand the device to the active seat user.
  SUBSYSTEM=="usb", ATTR{idVendor}=="04a9", TAG+="uaccess"
'';
```

**2. gvfs steals the camera.** If gvfs is running, its photo volume monitor
claims the device the moment it enumerates, and libgphoto2 gets
`Could not claim the USB device`. The app detects this specific failure and
says so, but the fix is outside it:

```bash
pkill -f gvfs-gphoto2-volume-monitor
```

Close any file manager showing the camera first, or it will just respawn.

### On the camera itself

Two 5D Mk II behaviours cost real debugging time, so they are worth knowing.

**Put the mode dial on M.** In a Basic Zone position — the green rectangle —
the body reports exactly *one* choice for ISO, shutter and white balance
("Auto"), and hides `imageformat` from the PTP config tree entirely. The
settings panel fills with dead single-entry combo boxes and nothing explains
why. In Manual the same widgets return 20, 55, 9 and 27 choices. The app can
set the mode over PTP, and exposure mode is the first row in the panel for
exactly this reason, but the PTP change is session-scoped: power-cycle the body
and it returns to whatever the physical dial says.

**Auto power-off drops the camera off the USB bus**, it does not merely sleep.
The default is 60 seconds, and when it fires the PTP session dies and the app
reports "no camera found" — indistinguishable from a cable fault. Racking focus
on a microscope is exactly the workload that trips it, since minutes can pass
with no PTP traffic. The app disables it on connect, so this only bites between
sessions; raising it in the camera menu makes it stop entirely.

**Turn "Auto rotate" off** (Set-up 1). The body tags every frame from this rig
as rotated, because its orientation sensor reads gravity and the camera is
aimed straight down the phototube -- there is no lateral component to resolve,
so it guesses. Viewers honour the tag and a landscape 5616x3744 capture is
displayed as a portrait "phone" photo. The app rewrites the tag to normal on
download regardless, so this is belt-and-braces.

Live view shooting must also be enabled in the menu, and the mode dial must not
be on a movie position.

### Exposure on a microscope

Daylight exposures are nowhere near enough. Measured on this rig, ISO 200 at
1/125 gives a brightfield frame averaging 33-70 of 255 -- dark but recoverable
-- and a darkfield frame averaging **1**, with a peak pixel of 10. Darkfield
excludes the direct light by design, so it needs on the order of 100-1000x more
exposure than brightfield.

| | ISO | shutter |
|---|---|---|
| Brightfield | 400 | 1/30 - 1/60 |
| Darkfield | 800-1600 | 1/4 s - several seconds |

Aperture is meaningless here (no lens); the f-number in the EXIF is whatever
the body invents. Image format is worth checking too -- "Medium" JPEG gives
4080x2720 rather than the full 5616x3744.

**Do not trust live view brightness.** The camera gains its live view display
up to keep it viewable, and only simulates the real exposure within a limited
range. Several stops past that the preview still looks perfectly normal while
the sensor is receiving essentially nothing. This is not a corner case: a
session here produced three darkfield frames averaging 1/255 from a live view
that looked correctly exposed throughout.

So the app measures exposure in two places, and only one of them is
authoritative:

  Preview level   Camera panel, live. A guide to what you are looking at.
                  Inherits the camera's gain, so it cannot predict a capture.
  Captured frame  Review bar, after the shutter. Measured from the downloaded
                  image, so it cannot be fooled. A frame with no signal in it
                  is called out in red before you decide whether to keep it.

`gphoto2 --auto-detect` is the quickest way to confirm the plumbing works before
blaming the app. `gphoto2 --list-config` shows the widget names this particular
body exposes.

## Performance

The tablet is the target, so the per-frame cost was profiled rather than
guessed. Analysis runs at half preview resolution with an integer L1 Sobel and
a single-multiply overlay:

| stage | full res | half res (default) |
|---|---|---|
| JPEG decode | 4.5 ms | 4.5 ms |
| greyscale | 8.1 ms | 2.3 ms |
| focus score | 0.8 ms | 0.3 ms |
| gradient | 25.1 ms | 1.0 ms |
| peaking overlay | 13.8 ms | 2.0 ms |
| **total** | **52 ms** | **10 ms** |

That moves analysis from being the bottleneck — below the camera's own ~14 fps
— to a seventh of the frame budget, at a measured 0.99 correlation with the
full-resolution focus score. Turning peaking off saves the last two rows;
unticking "Analyse frames" leaves only the decode.

## Using it

1. **It connects and starts live view by itself** on launch — arming live view
   is what flips the mirror up. The Connect and Start buttons are there for
   reconnecting after the camera has been unplugged or has powered off.
2. **Set exposure** so the image is neither clipped nor crushed. The focus
   score is computed from image contrast, so a blown-out or black frame gives it
   nothing to work with.
3. **Focus.** The ROI box marks the region actually being scored — put it on
   something with detail. Turn the knob and watch the trace: rising means keep
   going, falling means you passed it, and the dashed line is the best score
   held so far. Green means within 5% of that peak.
4. **Reset peak** after moving to a new field or changing objective. Scores from
   different scenes are not comparable, and a stale peak is an unreachable
   target.
5. **Capture, then keep or delete.** A capture takes over the viewport for
   review, with the same zoom and pan available, so you can look at actual
   pixels before deciding. **Delete** removes the frame from disk *and* from
   the camera's card; **Keep** returns to live view. Live frames keep arriving
   underneath and are simply not drawn while you decide.

The meter shows `finding range...` for its first dozen frames. That is
deliberate: the first frame is trivially also the best frame seen, so an
unsettled meter would report 100% of peak while you are still badly out of
focus.

**Zoom** with the wheel (anchored on the cursor), drag to pan, double-click or
`Ctrl+0` to fit. The view renders nearest-neighbour on purpose — smooth scaling
interpolates an edge gradient that reads as sharpness, which is exactly the
judgement you are here to make.

**On the tablet**, drag the live view to pan and flick the device panel to
scroll it. Controls are sized for a fingertip (40px minimum, oversized slider
handles) because the on-screen keyboard covers half the display and there is
usually no mouse at the bench.

**Focus peaking** outlines edges sharp enough to count as resolved. It thins out
as the image softens, so it blooms as you come into focus.

## How it works

```
ui/            Qt widgets. Bind to controllers, never to hardware.
  liveview.py    viewport: zoom, pan, ROI, overlay compositing
  panels/        one panel per device
devices/       hardware. Each device owns a thread.
  base.py        DeviceWorker / DeviceController pattern
  camera.py      libgphoto2 session, preview loop, config tree
core/          pure logic, no hardware
  focus.py       sharpness metrics + peak-hold  (numpy only, no Qt)
  imaging.py     QImage <-> numpy, peaking overlay
```

**The threading rule.** libgphoto2 is not thread-safe and every call blocks on
USB for tens of milliseconds. So each device's handle lives on a `DeviceWorker`
moved onto its own `QThread`, every hardware entry point is a slot, and results
come back as immutable signals. The GUI thread never touches hardware and never
blocks. Preview decode, greyscale and focus analysis all happen on the device
thread too — that is enough work to visibly stutter the UI otherwise.

**Adding a device** (stage, nosepiece, LED, injector): subclass `DeviceWorker`
with the hardware slots, subclass `DeviceController` to own its thread and
forward signals, add a panel that binds only to the controller, drop it into the
dock in `main_window.py`. No existing device needs to change.

`core/focus.py` deliberately imports nothing but numpy, so the metrics can be
driven headlessly — which is what a Z-stage autofocus sweep will want.

### Notes on the focus maths

Both metrics are divided by the ROI's mean intensity squared, so changing the
illumination does not read as a change in focus. That matters more once the LED
is under software control.

Peaking thresholds on absolute gradient strength with a noise-derived floor, not
on a percentile. The reasoning — and the two wrong implementations that came
first, both of which painted *more* as focus got worse — is written up in
`core/imaging.py`. `tests/test_peaking.py` pins the behaviour, because nothing
about a broken overlay looks broken.

## Tests

```bash
nix develop --command python -m pytest tests/ -q
```

They cover the focus metrics and the peaking threshold — the parts that fail
quietly, where a wrong answer still looks like a plausible number. The Qt layer
fails loudly enough not to need them. The nix build runs them.

## Packaging into pkgsnix

Deferred until this is its own git repo, since `pkgsnix` is consumed as
`github:Ryzzen/pkgsnix` and cannot reference a local path.

`nix/package.nix` is already written with a `callPackage` signature so it can be
moved as-is. Once the repo is pushed:

1. Copy `nix/package.nix` to `pkgsnix/microscope-control/default.nix`.
2. Replace the `src` argument with a fetch:

```nix
{ lib, python3Packages, qt6, fetchFromGitHub, version ? "0.1.0" }:
python3Packages.buildPythonApplication {
  pname = "microscope-control";
  inherit version;
  src = fetchFromGitHub {
    owner = "Ryzzen";
    repo = "MicroscopeControl";
    rev = "v${version}";
    hash = lib.fakeHash;   # replace with the real hash after first build
  };
  # ... rest unchanged
```

3. Add to `pkgsnix/default.nix`:

```nix
microscope-control = pkgs.callPackage ./microscope-control { };
```

## Roadmap

- **Stage** — Marlin over serial: XYZ jog, position readout, saved positions.
  Motorised Z is what turns the focus meter into real autofocus and makes focus
  stacking possible, which is the fix for the shallow depth of field that comes
  with any decent objective.
- **Nosepiece** — objective selection, with per-objective calibration. Objective
  changes must reset the focus peak; the hook is already there.
- **Illumination** — LED intensity under software control. The metrics are
  already illumination-normalised in anticipation.
- **Sessions** — structured capture: every frame recorded with its settings and
  stage coordinates. Waits for the stage, when a filename finally has
  coordinates worth recording.
- **Fault injection** — laser control and trigger timing, last.
