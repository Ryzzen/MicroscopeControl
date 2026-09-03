"""Normalising the EXIF orientation of downloaded captures.

Every frame off this rig arrives with EXIF Orientation = 8 ("rotate 270"), so
image viewers dutifully turn a landscape 5616x3744 file on its side and it
looks like a phone photo. The pixels are fine; only the tag is wrong.

The cause is that the tag comes from the body's gravity-based orientation
sensor, and on a microscope that sensor is measuring something meaningless.
The camera points straight down the phototube, so there is no lateral gravity
component to resolve and it settles on whichever rotation the noise favours.
There is no "up" to detect: up is defined by the stage and the objective, not
by the earth.

So the tag is not information, it is an artefact, and the honest thing to store
is Orientation = 1. This patches the two bytes in place rather than re-encoding
anything -- the tag value for a SHORT lives inline in the IFD entry, so the
pixel data is never touched and the file is bit-identical apart from those two
bytes.

(The camera can also be told to stop writing it: Set-up menu, "Auto rotate",
set to Off. This runs regardless, so it works whether or not that was done.)
"""

from __future__ import annotations

import struct
from pathlib import Path

# EXIF tag 0x0112, TIFF SHORT, one value.
_ORIENTATION_TAG = 0x0112
_TYPE_SHORT = 3
_NORMAL = 1

# EXIF lives in the APP1 segment right after SOI, so there is no need to read a
# 20 MB file to find it.
_HEADER_BYTES = 128 * 1024


def find_orientation(data: bytes) -> tuple[int, int] | None:
    """Return (file_offset_of_value, current_value), or None if absent."""
    marker = data.find(b"Exif\x00\x00")
    if marker < 0:
        return None
    tiff = marker + 6
    if len(data) < tiff + 8:
        return None

    byte_order = data[tiff : tiff + 2]
    if byte_order == b"MM":
        endian = ">"
    elif byte_order == b"II":
        endian = "<"
    else:
        return None

    def u16(offset: int) -> int:
        return struct.unpack(endian + "H", data[offset : offset + 2])[0]

    def u32(offset: int) -> int:
        return struct.unpack(endian + "I", data[offset : offset + 4])[0]

    ifd0 = tiff + u32(tiff + 4)
    if len(data) < ifd0 + 2:
        return None
    for index in range(u16(ifd0)):
        entry = ifd0 + 2 + index * 12
        if len(data) < entry + 12:
            return None
        if u16(entry) != _ORIENTATION_TAG:
            continue
        if u16(entry + 2) != _TYPE_SHORT or u32(entry + 4) != 1:
            return None
        # A single SHORT is stored inline in the value field, big- or
        # little-endian according to the TIFF header, left-aligned in 4 bytes.
        return entry + 8, u16(entry + 8)
    return None


def normalise_orientation(path: str | Path) -> int | None:
    """Rewrite a capture's orientation tag to "normal".

    Returns the value that was replaced, or None if there was nothing to do.
    Never raises on a malformed or unreadable file: a failed tag patch must not
    cost the user the frame they just took.
    """
    path = Path(path)
    try:
        with open(path, "r+b") as handle:
            head = handle.read(_HEADER_BYTES)
            found = find_orientation(head)
            if found is None:
                return None
            offset, current = found
            if current == _NORMAL:
                return None
            endian = ">" if head[head.find(b"Exif\x00\x00") + 6 : ][:2] == b"MM" else "<"
            handle.seek(offset)
            handle.write(struct.pack(endian + "H", _NORMAL))
            return current
    except OSError:
        return None
