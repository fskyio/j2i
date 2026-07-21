"""Avatar loading and normalization for XEP-0153 vCard-based avatars.

slixmpp performs no validation on avatar bytes (no size, dimension, or format
checks), so everything here is our responsibility.  The binding constraint on
the wire is the server's max stanza size: the image travels base64-encoded
inside a vcard-temp IQ, and an oversized stanza gets the connection dropped
rather than cleanly rejected.  We therefore decode with Pillow, downscale to a
sane avatar size, strip metadata (EXIF) by re-encoding, and guarantee the
output fits under a byte budget.

Reused by Phase 2 (bridging IRC users' avatars): the same decode-and-fit path
hardens untrusted remote images, and the pixel guard defends against
decompression bombs.
"""
from __future__ import annotations

import hashlib
import io
import logging
import urllib.request
from base64 import b64encode
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from xml.etree.ElementTree import Element, SubElement

from PIL import Image

log = logging.getLogger(__name__)

_NS_VCARD_UPDATE = "vcard-temp:x:update"

# Reject images whose declared pixel count is absurd before we decode the full
# buffer.  Guards against decompression bombs from untrusted sources (Phase 2).
_MAX_PIXELS = 8192 * 8192  # 64 megapixels

# Formats we are willing to decode as avatar input.
_ALLOWED_INPUT_FORMATS = {"PNG", "JPEG", "GIF", "WEBP", "BMP"}

# Cap for reading operator-provided local files.  These are trusted and are
# meant to be downscaled here, so this only exists to avoid decoding something
# pathological.  Network fetches use the caller's (much smaller) max_download.
_MAX_LOCAL_BYTES = 16 * 1024 * 1024


class AvatarError(Exception):
    """Raised when an avatar source cannot be loaded into a valid image."""


@dataclass(frozen=True)
class Avatar:
    data: bytes  # canonical, re-encoded image bytes
    mime: str  # "image/png" or "image/jpeg"
    sha1: str  # hex SHA-1 of data — the XEP-0153 photo hash
    width: int
    height: int

    @property
    def b64(self) -> str:
        return b64encode(self.data).decode("ascii")

    @classmethod
    def load(
        cls,
        source: str,
        *,
        max_download: int = 256 * 1024,
        byte_cap: int = 64 * 1024,
        target_px: int = 96,
    ) -> "Avatar":
        """Load an avatar from a filesystem path or an http(s) URL.

        Raises AvatarError on any failure (unreachable, not an image, cannot be
        shrunk under byte_cap).  The caller is expected to log and continue
        without an avatar rather than treat this as fatal.
        """
        raw = _read_source(source, max_download)
        return cls.from_bytes(raw, byte_cap=byte_cap, target_px=target_px)

    @classmethod
    def from_bytes(
        cls, raw: bytes, *, byte_cap: int = 64 * 1024, target_px: int = 96
    ) -> "Avatar":
        try:
            img = Image.open(io.BytesIO(raw))
            # size is available without a full decode; check before img.load()
            if img.width * img.height > _MAX_PIXELS:
                raise AvatarError(
                    f"image too large: {img.width}x{img.height} pixels"
                )
            fmt = (img.format or "").upper()
            if fmt not in _ALLOWED_INPUT_FORMATS:
                raise AvatarError(f"unsupported image format: {fmt or 'unknown'}")
            img.load()
            img = img.convert("RGBA")
        except AvatarError:
            raise
        except Exception as e:
            # Untrusted image decoders raise a wide, version-dependent set of
            # exception types (UnidentifiedImageError, OSError, SyntaxError,
            # ValueError, ...); treat any of them as "not a usable image".
            raise AvatarError(f"not a decodable image: {e}") from e

        data, mime, width, height = _fit(img, byte_cap, target_px)
        return cls(
            data=data,
            mime=mime,
            sha1=hashlib.sha1(data).hexdigest(),
            width=width,
            height=height,
        )

    def photo_update_element(self) -> Element:
        """Build the <x xmlns='vcard-temp:x:update'><photo>hash</photo></x>.

        Stamped onto MUC presence so clients know to (re)fetch the vCard.
        """
        x = Element(f"{{{_NS_VCARD_UPDATE}}}x")
        SubElement(x, f"{{{_NS_VCARD_UPDATE}}}photo").text = self.sha1
        return x


def default_avatar_path() -> str | None:
    """Path to the bundled default bot avatar, or None if not packaged."""
    try:
        p = files("j2i") / "data" / "default-avatar.png"
        if p.is_file():
            return str(p)
    except (ModuleNotFoundError, FileNotFoundError, OSError):
        pass
    return None


def _read_source(source: str, max_download: int) -> bytes:
    s = str(source)
    if s.startswith(("http://", "https://")):
        return _download(s, max_download)
    data = Path(s).read_bytes()
    if len(data) > _MAX_LOCAL_BYTES:
        raise AvatarError(f"avatar file exceeds {_MAX_LOCAL_BYTES} bytes")
    return data


def _download(url: str, max_bytes: int) -> bytes:
    # NOTE (Phase 2): for untrusted, IRC-user-supplied URLs this needs SSRF
    # protection (block redirects to internal hosts, restrict schemes/ports).
    # For Phase 1 the URL is operator-controlled, so a size + time cap suffices.
    req = urllib.request.Request(url, headers={"User-Agent": "j2i-avatar"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310
            chunks: list[bytes] = []
            total = 0
            while True:
                chunk = resp.read(16384)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise AvatarError(f"remote avatar exceeds {max_bytes} bytes")
                chunks.append(chunk)
    except AvatarError:
        raise
    except Exception as e:
        raise AvatarError(f"could not fetch avatar: {e}") from e
    return b"".join(chunks)


def _scaled(img: Image.Image, longest_side: int) -> Image.Image:
    w, h = img.size
    if max(w, h) <= longest_side:
        return img
    scale = longest_side / max(w, h)
    return img.resize(
        (max(1, round(w * scale)), max(1, round(h * scale))), Image.LANCZOS
    )


def _encode_png(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def _encode_jpeg(img: Image.Image, quality: int) -> bytes:
    buf = io.BytesIO()
    # JPEG has no alpha; flatten onto white.
    rgb = Image.new("RGB", img.size, (255, 255, 255))
    rgb.paste(img, mask=img.split()[-1] if img.mode == "RGBA" else None)
    rgb.save(buf, format="JPEG", quality=quality, optimize=True)
    return buf.getvalue()


def _fit(
    img: Image.Image, byte_cap: int, target_px: int
) -> tuple[bytes, str, int, int]:
    """Downscale + encode so the result fits under byte_cap.

    PNG is preferred (keeps transparency); for the rare avatar that will not
    shrink enough as PNG we fall back to progressively lower-quality JPEG.
    """
    img = _scaled(img, target_px)
    data = _encode_png(img)
    if len(data) <= byte_cap:
        return data, "image/png", img.width, img.height

    for quality in (85, 70, 55, 40):
        data = _encode_jpeg(img, quality)
        if len(data) <= byte_cap:
            return data, "image/jpeg", img.width, img.height

    raise AvatarError(
        f"cannot fit avatar under {byte_cap} bytes even as JPEG"
    )
