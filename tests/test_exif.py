"""Tests for the EXIF orientation fix.

Every frame this rig produced carried Orientation = 8, so viewers rotated a
landscape capture into portrait and it looked like a phone photo. The tag is
noise -- it comes from a gravity sensor on a camera aimed straight down -- so
it gets rewritten to "normal" on download.

The tests build the EXIF header by hand rather than shipping a fixture: the
patch is byte-level surgery on a structure with two possible byte orders, and
constructing both is the only way to be sure the little-endian path is not
quietly broken.
"""

from __future__ import annotations

import struct

import pytest

from microscope_control.core.exif import find_orientation, normalise_orientation


def build_jpeg(orientation: int = 8, endian: str = ">") -> bytes:
    """A minimal JPEG carrying an APP1/EXIF block with one Orientation tag."""
    bo = b"MM" if endian == ">" else b"II"
    # TIFF header: byte order, magic 42, offset to IFD0 (immediately after).
    tiff = bo + struct.pack(endian + "HI", 42, 8)
    entry = struct.pack(endian + "HHI", 0x0112, 3, 1) + struct.pack(
        endian + "H", orientation
    ) + b"\x00\x00"
    ifd = struct.pack(endian + "H", 1) + entry + struct.pack(endian + "I", 0)
    exif_payload = b"Exif\x00\x00" + tiff + ifd
    app1 = b"\xff\xe1" + struct.pack(">H", len(exif_payload) + 2) + exif_payload
    # SOI + APP1 + a token SOF0 and EOI so it is at least JPEG-shaped.
    return b"\xff\xd8" + app1 + b"\xff\xc0\x00\x0b\x08\x0e\xa0\x15\xf0\x01\x01\x11\x00" + b"\xff\xd9"


@pytest.mark.parametrize("endian", [">", "<"])
def test_finds_and_rewrites_orientation(tmp_path, endian: str) -> None:
    path = tmp_path / "shot.jpg"
    original = build_jpeg(orientation=8, endian=endian)
    path.write_bytes(original)

    assert find_orientation(original)[1] == 8
    assert normalise_orientation(path) == 8
    assert find_orientation(path.read_bytes())[1] == 1
    # Only the tag may change; the pixel data must be untouched.
    assert len(path.read_bytes()) == len(original)


def test_is_idempotent(tmp_path) -> None:
    """Re-running must be a no-op, not a corruption."""
    path = tmp_path / "shot.jpg"
    path.write_bytes(build_jpeg(orientation=8))
    assert normalise_orientation(path) == 8
    assert normalise_orientation(path) is None
    assert find_orientation(path.read_bytes())[1] == 1


def test_already_normal_is_left_alone(tmp_path) -> None:
    path = tmp_path / "shot.jpg"
    path.write_bytes(build_jpeg(orientation=1))
    assert normalise_orientation(path) is None


def test_missing_exif_is_not_an_error(tmp_path) -> None:
    """A frame with no EXIF must not cost the user the capture."""
    path = tmp_path / "plain.jpg"
    path.write_bytes(b"\xff\xd8\xff\xd9")
    assert normalise_orientation(path) is None
    assert find_orientation(path.read_bytes()) is None


def test_unreadable_file_is_not_an_error(tmp_path) -> None:
    assert normalise_orientation(tmp_path / "nonexistent.jpg") is None
