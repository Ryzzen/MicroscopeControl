"""Common shape for every instrument on the rig.

Right now the only device is the camera, but the rig is going to grow a Marlin
motion controller, a motorised nosepiece and an LED driver, and every one of
them has the same awkward property: it talks to hardware over a link that
blocks, so it cannot be driven from the GUI thread. Fixing that per-device
would mean four slightly different threading bugs, so the pattern is fixed
here once.

The pattern is the standard Qt worker-object one, and it has exactly two rules:

  1. A `DeviceWorker` subclass owns the hardware handle and is moved onto its
     own QThread. Every method that touches hardware is a slot, so calling it
     from the GUI thread goes through the event loop and executes on the worker
     thread. Nothing else may touch the handle -- libgphoto2 contexts and
     pyserial ports are both single-threaded in practice.
  2. Results travel back as signals carrying immutable data. Never a live
     handle, never a mutable buffer.

A `DeviceController` is the GUI-thread face of one device: it owns the thread,
forwards requests in and signals out, and is what UI panels actually bind to.
"""

from __future__ import annotations

from enum import Enum, auto

from PySide6.QtCore import QObject, QThread, Signal


class DeviceState(Enum):
    """Lifecycle of a single instrument.

    BUSY exists separately from READY because several devices have operations
    that must not overlap -- a full-resolution capture, a stage move, a laser
    pulse -- and the UI needs to grey out controls for exactly that window.
    """

    DISCONNECTED = auto()
    CONNECTING = auto()
    READY = auto()
    BUSY = auto()
    ERROR = auto()

    @property
    def label(self) -> str:
        return {
            DeviceState.DISCONNECTED: "Disconnected",
            DeviceState.CONNECTING: "Connecting",
            DeviceState.READY: "Ready",
            DeviceState.BUSY: "Busy",
            DeviceState.ERROR: "Error",
        }[self]


class DeviceWorker(QObject):
    """Base for the object that lives on the device thread.

    Subclasses add hardware slots and must never be given a parent -- a QObject
    with a parent cannot be moved between threads.
    """

    stateChanged = Signal(object, str)  # DeviceState, human-readable detail
    error = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self._state = DeviceState.DISCONNECTED

    @property
    def state(self) -> DeviceState:
        return self._state

    def _set_state(self, state: DeviceState, detail: str = "") -> None:
        """Announce a state change, including repeats.

        Repeats are not filtered on purpose: the detail string carries progress
        text ("Downloading IMG_0042.CR2") that changes while the state itself
        stays BUSY, and swallowing those would freeze the status line.
        """
        self._state = state
        self.stateChanged.emit(state, detail)

    def _fail(self, message: str) -> None:
        self._set_state(DeviceState.ERROR, message)
        self.error.emit(message)


class DeviceController(QObject):
    """GUI-thread handle for one device, owning its worker thread.

    Subclasses build their worker, call `_start_worker`, and wire request
    signals to worker slots. Because the connections cross a thread boundary Qt
    makes them queued automatically, so a UI click becomes an event posted to
    the device thread and returns immediately.
    """

    stateChanged = Signal(object, str)
    error = Signal(str)

    def __init__(self, worker: DeviceWorker, thread_name: str) -> None:
        super().__init__()
        self._worker = worker
        self._thread = QThread()
        self._thread.setObjectName(thread_name)
        worker.moveToThread(self._thread)
        worker.stateChanged.connect(self.stateChanged)
        worker.error.connect(self.error)
        self._state = DeviceState.DISCONNECTED
        self.stateChanged.connect(self._remember_state)

    def _remember_state(self, state: DeviceState, _detail: str) -> None:
        self._state = state

    @property
    def state(self) -> DeviceState:
        return self._state

    def _start_worker(self) -> None:
        self._thread.start()

    def shutdown(self) -> None:
        """Stop the device thread, giving the worker a chance to close cleanly.

        Worth being careful here: a camera left with live view running holds the
        mirror up and keeps the sensor powered, and a stage killed mid-move
        loses its position reference. Subclasses override to release hardware
        first, then call up.
        """
        if self._thread.isRunning():
            self._thread.quit()
            # Bounded rather than infinite: a wedged USB read must not stop the
            # application from exiting.
            if not self._thread.wait(3000):
                self._thread.terminate()
                self._thread.wait(1000)
