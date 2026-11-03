"""Media synthesis and inspection.

Two jobs, both deliberately dependency-free (Pillow is not installed and adding a
binary dependency for a mock provider would be disproportionate):

* :func:`encode_png` — build a real, valid PNG byte stream from a deterministic
  palette pattern. The mock creative provider uses this so its "generated" bytes
  are actual decodable images rather than random bytes with an image MIME type.
* :func:`inspect_media` — derive the true media type, dimensions and kind from
  the bytes themselves. Client-supplied MIME types are never trusted: a declared
  ``image/png`` that is really a ZIP is reported as such.

Nothing here claims visual understanding. It parses headers; that is all.
"""

from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass, field
from typing import Any

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

#: Aspect ratios the studio can request, as ``(width, height)`` pairs of the ratio.
ASPECT_RATIOS: dict[str, tuple[int, int]] = {
    "1:1": (1, 1),
    "4:5": (4, 5),
    "9:16": (9, 16),
    "16:9": (16, 9),
    "1.91:1": (191, 100),
    "3:2": (3, 2),
    "2:3": (2, 3),
}

MIME_BY_KIND = {
    "png": "image/png",
    "jpeg": "image/jpeg",
    "gif": "image/gif",
    "webp": "image/webp",
    "mp4": "video/mp4",
    "webm": "video/webm",
    "pdf": "application/pdf",
}

EXTENSION_BY_MIME = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "video/mp4": ".mp4",
    "video/webm": ".webm",
    "application/pdf": ".pdf",
}

KIND_BY_MIME = {
    "image/png": "image",
    "image/jpeg": "image",
    "image/gif": "image",
    "image/webp": "image",
    "video/mp4": "video",
    "video/webm": "video",
    "application/pdf": "document",
}

#: The studio's own accepted types. Anything else is refused on the strict path.
ALLOWED_MIME = set(MIME_BY_KIND.values())


# --------------------------------------------------------------------------- #
# PNG encoding
# --------------------------------------------------------------------------- #
def _chunk(tag: bytes, payload: bytes) -> bytes:
    return (
        struct.pack(">I", len(payload))
        + tag
        + payload
        + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)
    )


def encode_png(width: int, height: int, pixels: bytes) -> bytes:
    """Encode 8-bit RGB ``pixels`` (``width*height*3`` bytes) as a PNG."""
    if len(pixels) != width * height * 3:
        raise ValueError("pixel buffer does not match the given dimensions")
    raw = bytearray()
    stride = width * 3
    for row in range(height):
        raw.append(0)  # filter type: none
        start = row * stride
        raw.extend(pixels[start : start + stride])
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        PNG_SIGNATURE
        + _chunk(b"IHDR", ihdr)
        + _chunk(b"IDAT", zlib.compress(bytes(raw), 9))
        + _chunk(b"IEND", b"")
    )


def _clamp(value: int) -> int:
    return 0 if value < 0 else 255 if value > 255 else value


def dimensions_for(aspect_ratio: str, max_dimension: int = 960) -> tuple[int, int]:
    """Largest whole-pixel size for ``aspect_ratio`` that fits ``max_dimension``."""
    ratio = ASPECT_RATIOS.get(aspect_ratio.strip(), ASPECT_RATIOS["4:5"])
    rw, rh = ratio
    if rw >= rh:
        width = max_dimension
        height = max(1, round(max_dimension * rh / rw))
    else:
        height = max_dimension
        width = max(1, round(max_dimension * rw / rh))
    return width, height


def render_brand_frame(
    *,
    width: int,
    height: int,
    seed: int = 0,
    palette: dict[str, str] | None = None,
    video_poster: bool = False,
) -> bytes:
    """Render the Crusor visual system: charcoal field, silver bar, purple glow.

    Deterministic for a given ``(width, height, seed)``. Abstract by design: it
    never draws a product UI, so it cannot imply a screenshot of real software.
    Rows are filled in bulk and only the glow band is computed per pixel, which
    keeps a 768x960 frame well under a tenth of a second.
    """
    palette = palette or {}
    background = _hex_to_rgb(palette.get("background", "#0A0A0B"))
    surface = _hex_to_rgb(palette.get("surface", "#17171B"))
    silver = _hex_to_rgb(palette.get("metallic", "#C0C0C8"))
    purple = _hex_to_rgb(palette.get("accent", "#8B5CF6"))

    glow_cx = int(width * (0.62 + 0.12 * ((seed % 3) - 1)))
    glow_cy = int(height * 0.30)
    glow_r = max(1, int(min(width, height) * 0.55))
    bar_y = int(height * (0.60 if not video_poster else 0.66))
    bar_h = max(2, height // 90)

    glow_x0 = max(0, glow_cx - glow_r)
    glow_x1 = min(width, glow_cx + glow_r)
    silver_bytes = bytes(silver)
    highlight = bytes(
        (_clamp(c + 26) for c in silver)
    )

    buf = bytearray()
    for y in range(height):
        blend = y / max(1, height - 1)
        row_rgb = (
            _clamp(int(background[0] + (surface[0] - background[0]) * blend)),
            _clamp(int(background[1] + (surface[1] - background[1]) * blend)),
            _clamp(int(background[2] + (surface[2] - background[2]) * blend)),
        )
        in_bar = bar_y <= y < bar_y + bar_h
        row = bytearray((silver_bytes if in_bar else bytes(row_rgb)) * width)

        dy = y - glow_cy
        dy2 = dy * dy
        if dy2 < glow_r * glow_r:
            radius2 = glow_r * glow_r
            tint = 0.72 if in_bar else 1.0
            for x in range(glow_x0, glow_x1):
                dx = x - glow_cx
                dist2 = dx * dx + dy2
                if dist2 >= radius2:
                    continue
                strength = (radius2 - dist2) / radius2
                factor = strength * strength * 0.55
                index = x * 3
                r = row[index]
                g = row[index + 1]
                b = row[index + 2]
                row[index] = _clamp(int(r + (purple[0] - r) * factor * tint))
                row[index + 1] = _clamp(int(g + (purple[1] - g) * factor * tint))
                row[index + 2] = _clamp(int(b + (purple[2] - b) * factor * tint))

        # sparse silver ticks: deterministic, and only a few per row
        if (y * 13 + seed * 31) % 11 == 0:
            for x in range((y * 7 + seed) % 40, width, 97):
                index = x * 3
                row[index : index + 3] = highlight

        buf.extend(row)

    if video_poster:
        for index in range(0, len(buf), 3):
            buf[index] = _clamp(int(buf[index] * 0.72))
            buf[index + 1] = _clamp(int(buf[index + 1] * 0.72))
            buf[index + 2] = _clamp(int(buf[index + 2] * 0.72))
        cx, cy = width // 2, height // 2
        size = max(10, min(width, height) // 6)
        for step in range(size):
            x = cx - size // 2 + step
            _put(buf, width, x, cy - size // 2 + step, silver)
            _put(buf, width, x, cy + size // 2 - step, silver)
        for step in range(size):
            span = size - step
            for dx in range(span):
                x = cx - size // 2 + dx + step
                _put(buf, width, x, cy - size // 2 + step, silver)
                _put(buf, width, x, cy + size // 2 - step, silver)

    return encode_png(width, height, bytes(buf))


def _put(buf: bytearray, width: int, x: int, y: int, rgb: tuple[int, int, int]) -> None:
    height = len(buf) // (width * 3)
    if 0 <= x < width and 0 <= y < height:
        index = (y * width + x) * 3
        buf[index], buf[index + 1], buf[index + 2] = rgb


def _hex_to_rgb(value: str) -> tuple[int, int, int]:
    text = value.strip().lstrip("#")
    if len(text) == 3:
        text = "".join(ch * 2 for ch in text)
    if len(text) != 6:
        return (10, 10, 11)
    try:
        return (int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16))
    except ValueError:
        return (10, 10, 11)


# --------------------------------------------------------------------------- #
# Inspection
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class MediaInfo:
    """What the bytes actually are — never what the client said they were."""

    mime_type: str | None
    extension: str | None
    kind: str | None
    width: int | None = None
    height: int | None = None
    duration_seconds: float | None = None
    recognized: bool = False
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "mime_type": self.mime_type,
            "extension": self.extension,
            "kind": self.kind,
            "width": self.width,
            "height": self.height,
            "duration_seconds": self.duration_seconds,
            "recognized": self.recognized,
            "notes": list(self.notes),
        }


def inspect_media(data: bytes) -> MediaInfo:
    """Identify ``data`` from its signature and parse dimensions where possible."""
    if not data:
        return MediaInfo(None, None, None, notes=["empty payload"])

    if data.startswith(PNG_SIGNATURE):
        width = height = None
        if len(data) >= 24 and data[12:16] == b"IHDR":
            width, height = struct.unpack(">II", data[16:24])
        notes = [] if width else ["PNG header truncated; dimensions unknown"]
        return MediaInfo(
            "image/png", ".png", "image", width, height, recognized=True, notes=notes
        )

    if data.startswith(b"\xff\xd8\xff"):
        width, height = _jpeg_dimensions(data)
        return MediaInfo("image/jpeg", ".jpg", "image", width, height)

    if data[:6] in (b"GIF87a", b"GIF89a"):
        width = height = None
        if len(data) >= 10:
            width, height = struct.unpack("<HH", data[6:10])
        return MediaInfo("image/gif", ".gif", "image", width, height)

    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return MediaInfo("image/webp", ".webp", "image")

    if len(data) >= 12 and data[4:8] == b"ftyp":
        return MediaInfo("video/mp4", ".mp4", "video")

    if data.startswith(b"\x1a\x45\xdf\xa3"):
        return MediaInfo("video/webm", ".webm", "video")

    if data.startswith(b"%PDF"):
        return MediaInfo("application/pdf", ".pdf", "document")

    return MediaInfo(None, None, None, notes=["unrecognised signature"])


def _jpeg_dimensions(data: bytes) -> tuple[int | None, int | None]:
    index = 2
    length = len(data)
    while index + 9 < length:
        if data[index] != 0xFF:
            index += 1
            continue
        marker = data[index + 1]
        if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
            index += 2
            continue
        segment_length = struct.unpack(">H", data[index + 2 : index + 4])[0]
        if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB):
            height, width = struct.unpack(">HH", data[index + 5 : index + 9])
            return width, height
        index += 2 + segment_length
    return None, None


def validate_upload(
    data: bytes,
    *,
    declared_mime: str | None,
    filename: str | None,
    max_bytes: int,
    strict: bool,
) -> tuple[MediaInfo, list[str], str | None]:
    """Validate uploaded or generated bytes.

    Returns ``(info, warnings, error)``. ``error`` is non-``None`` only when the
    payload must be refused. On the strict path (studio uploads and generated
    assets) an unrecognisable payload, a MIME mismatch or a wrong extension is
    fatal; the legacy Phase 1 route stays permissive but records the mismatch.
    """
    warnings: list[str] = []
    if not data:
        return inspect_media(data), warnings, "The file is empty."
    if len(data) > max_bytes:
        return (
            inspect_media(data),
            warnings,
            f"File exceeds the {max_bytes // (1024 * 1024)}MB limit.",
        )

    info = inspect_media(data)
    declared = (declared_mime or "").split(";")[0].strip().lower()
    suffix = ""
    if filename and "." in filename:
        suffix = "." + filename.rsplit(".", 1)[1].lower()

    if not info.recognized:
        if strict:
            return info, warnings, (
                "Unrecognised file content. Upload a PNG, JPEG, GIF, WebP, MP4, "
                "WebM or PDF file."
            )
        warnings.append("File type could not be verified from its content.")
        return info, warnings, None

    if declared and declared != info.mime_type:
        message = (
            f"Declared type '{declared}' does not match the file content "
            f"('{info.mime_type}')."
        )
        if strict:
            return info, warnings, message
        warnings.append(message)

    expected_suffix = info.extension or ""
    if suffix and expected_suffix and suffix not in {expected_suffix, ".jpeg" if expected_suffix == ".jpg" else expected_suffix}:
        message = f"Extension '{suffix}' does not match the file content ('{expected_suffix}')."
        if strict:
            return info, warnings, message
        warnings.append(message)

    return info, warnings, None


# --------------------------------------------------------------------------- #
# Pixel analysis (PNG only)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class PngStats:
    """Basic statistics from decoding a PNG frame.

    This is real pixel analysis, not metadata inspection — but it is limited to
    what arithmetic can tell you: mean luminance, how much of the frame is in the
    purple family, how much is near-metallic, and how many distinct colours appear
    in the sample. It says nothing about composition, beauty or brand fit beyond
    those numbers, and it cannot read text.
    """

    width: int
    height: int
    mean_luma: float
    dark_ratio: float
    purple_ratio: float
    metallic_ratio: float
    sampled_pixels: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "width": self.width,
            "height": self.height,
            "mean_luma": round(self.mean_luma, 2),
            "dark_ratio": round(self.dark_ratio, 3),
            "purple_ratio": round(self.purple_ratio, 3),
            "metallic_ratio": round(self.metallic_ratio, 3),
            "sampled_pixels": self.sampled_pixels,
        }


def _paeth(a: int, b: int, c: int) -> int:
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    if pb <= pc:
        return b
    return c


def decode_png_pixels(data: bytes) -> tuple[int, int, int, bytes]:
    """Decode an 8-bit RGB/RGBA non-interlaced PNG into raw RGB pixels.

    Supports filter types 0–4. Returns ``(width, height, channels, rgb_bytes)``.
    Raises ``ValueError`` for anything it cannot decode (palette, 16-bit,
    interlaced) — callers must treat that as "not analysed", never as "passed".
    """
    if not data.startswith(PNG_SIGNATURE):
        raise ValueError("not a PNG")
    pos = 8
    width = height = bit_depth = colour_type = interlace = 0
    idat = bytearray()
    while pos + 8 <= len(data):
        length = struct.unpack(">I", data[pos : pos + 4])[0]
        tag = data[pos + 4 : pos + 8]
        payload = data[pos + 8 : pos + 8 + length]
        pos += length + 12
        if tag == b"IHDR":
            width, height, bit_depth, colour_type, _comp, _filter, interlace = struct.unpack(
                ">IIBBBBB", payload
            )
        elif tag == b"IDAT":
            idat.extend(payload)
        elif tag == b"IEND":
            break
    if bit_depth != 8 or interlace != 0 or colour_type not in (2, 6):
        raise ValueError("unsupported PNG variant")
    channels = 3 if colour_type == 2 else 4
    raw = zlib.decompress(bytes(idat))
    stride = width * channels
    if len(raw) < height * (stride + 1):
        raise ValueError("truncated PNG data")
    out = bytearray(height * stride)
    previous = bytearray(stride)
    pos = 0
    for y in range(height):
        filter_type = raw[pos]
        pos += 1
        line = bytearray(raw[pos : pos + stride])
        pos += stride
        if filter_type == 1:
            for i in range(channels, stride):
                line[i] = (line[i] + line[i - channels]) & 0xFF
        elif filter_type == 2:
            for i in range(stride):
                line[i] = (line[i] + previous[i]) & 0xFF
        elif filter_type == 3:
            for i in range(stride):
                left = line[i - channels] if i >= channels else 0
                line[i] = (line[i] + ((left + previous[i]) >> 1)) & 0xFF
        elif filter_type == 4:
            for i in range(stride):
                left = line[i - channels] if i >= channels else 0
                up_left = previous[i - channels] if i >= channels else 0
                line[i] = (line[i] + _paeth(left, previous[i], up_left)) & 0xFF
        elif filter_type != 0:
            raise ValueError(f"unknown PNG filter type {filter_type}")
        out[y * stride : (y + 1) * stride] = line
        previous = line
    return width, height, channels, bytes(out)


def analyze_png(data: bytes, *, sample_step: int = 7) -> PngStats:
    """Compute colour statistics for a PNG frame."""
    width, height, channels, pixels = decode_png_pixels(data)
    total = 0
    luma_sum = 0.0
    dark = purple = metallic = 0
    stride = width * channels
    for y in range(0, height, sample_step):
        row = y * stride
        for x in range(0, width, sample_step):
            index = row + x * channels
            r = pixels[index]
            g = pixels[index + 1]
            b = pixels[index + 2]
            luma = 0.2126 * r + 0.7152 * g + 0.0722 * b
            luma_sum += luma
            total += 1
            if luma < 60:
                dark += 1
            if b > 90 and b > r + 20 and b > g + 10:
                purple += 1
            if abs(r - g) < 18 and abs(g - b) < 22 and 120 <= luma <= 220:
                metallic += 1
    if total == 0:
        raise ValueError("empty image")
    return PngStats(
        width=width,
        height=height,
        mean_luma=luma_sum / total,
        dark_ratio=dark / total,
        purple_ratio=purple / total,
        metallic_ratio=metallic / total,
        sampled_pixels=total,
    )
