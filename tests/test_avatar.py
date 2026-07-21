"""Tests for the avatar loading/normalization helpers."""
from __future__ import annotations

import hashlib
import io

import pytest
from PIL import Image

from j2i.xmpp.avatar import Avatar, AvatarError


def _img_bytes(fmt: str, size=(200, 120), color=(10, 120, 200, 255)) -> bytes:
    """Encode a solid-color image in the given Pillow format."""
    mode = "RGBA" if fmt == "PNG" else "RGB"
    img = Image.new(mode, size, color if mode == "RGBA" else color[:3])
    buf = io.BytesIO()
    img.save(buf, format=fmt)
    return buf.getvalue()


@pytest.mark.parametrize("fmt", ["PNG", "JPEG", "GIF", "WEBP", "BMP"])
def test_decodes_common_formats_to_png(fmt):
    av = Avatar.from_bytes(_img_bytes(fmt))
    assert av.mime == "image/png"
    assert av.data.startswith(b"\x89PNG\r\n")
    assert av.sha1 == hashlib.sha1(av.data).hexdigest()


def test_downscales_to_target_px():
    av = Avatar.from_bytes(_img_bytes("PNG", size=(1024, 512)), target_px=96)
    # Longest side clamped to the target, aspect preserved.
    assert max(av.width, av.height) == 96
    assert (av.width, av.height) == (96, 48)


def test_no_upscale_of_small_images():
    av = Avatar.from_bytes(_img_bytes("PNG", size=(40, 40)), target_px=96)
    assert (av.width, av.height) == (40, 40)


def test_result_fits_byte_cap():
    # A noisy image resists PNG compression; a tiny cap forces the JPEG path.
    import os

    noise = Image.frombytes("RGB", (400, 400), os.urandom(400 * 400 * 3))
    buf = io.BytesIO()
    noise.save(buf, format="PNG")
    av = Avatar.from_bytes(buf.getvalue(), byte_cap=4 * 1024, target_px=96)
    assert len(av.data) <= 4 * 1024
    assert av.mime in ("image/png", "image/jpeg")


def test_rejects_non_image():
    with pytest.raises(AvatarError):
        Avatar.from_bytes(b"this is definitely not an image")


def test_rejects_oversized_pixels(monkeypatch):
    # Lower the decompression-bomb guard so a normal test image trips it
    # cleanly (valid image, rejected on declared pixel count before decode).
    monkeypatch.setattr("j2i.xmpp.avatar._MAX_PIXELS", 100)
    with pytest.raises(AvatarError, match="pixels"):
        Avatar.from_bytes(_img_bytes("PNG", size=(200, 120)))


def test_photo_update_element_carries_hash():
    av = Avatar.from_bytes(_img_bytes("PNG"))
    x = av.photo_update_element()
    assert x.tag == "{vcard-temp:x:update}x"
    photo = x.find("{vcard-temp:x:update}photo")
    assert photo is not None
    assert photo.text == av.sha1


def test_load_from_file(tmp_path):
    p = tmp_path / "avatar.png"
    p.write_bytes(_img_bytes("PNG"))
    av = Avatar.load(str(p))
    assert av.mime == "image/png"
    assert av.width and av.height
