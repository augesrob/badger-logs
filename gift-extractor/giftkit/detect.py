"""Content-sniffing for gift assets.

Dumped gift assets are frequently extensionless (CDN cache blobs, files named by
md5), so every decision here is made from the bytes rather than the filename.
"""
from __future__ import annotations

import json
import struct
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from .util import LOG

SNIFF_BYTES = 64 * 1024


class Kind(str, Enum):
    ANIMATED_WEBP = "animated-webp"
    STATIC_WEBP = "static-webp"
    APNG = "apng"
    PNG = "png"
    JPEG = "jpeg"
    GIF = "gif"
    VAP_MP4 = "vap-mp4"          # mp4 carrying a `vapc` config box
    ALPHA_SPLIT_MP4 = "split-mp4"  # mp4 whose frame is [rgb | alpha], no vapc box
    MP4 = "mp4"                  # ordinary opaque video
    PAG = "pag"
    SVGA = "svga"
    LOTTIE = "lottie"
    ZIP = "zip"
    UNKNOWN = "unknown"

    @property
    def is_animation(self) -> bool:
        return self in {
            Kind.ANIMATED_WEBP, Kind.APNG, Kind.GIF, Kind.VAP_MP4,
            Kind.ALPHA_SPLIT_MP4, Kind.MP4, Kind.PAG, Kind.SVGA, Kind.LOTTIE,
        }


@dataclass
class Detection:
    kind: Kind
    #: ``vapc`` payload for VAP files, or the sniffed geometry for split videos.
    vap: dict[str, Any] | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def alpha_layout(self) -> str | None:
        """``left-right``/``right-left``/``top-bottom``/``bottom-top`` when known."""
        if not self.vap:
            return None
        return self.vap.get("layout")


def _read_head(path: Path, size: int = SNIFF_BYTES) -> bytes:
    with open(path, "rb") as handle:
        return handle.read(size)


def _webp_kind(head: bytes) -> Kind:
    # RIFF container: chunks follow the 12-byte header. An `ANIM` chunk (only
    # present in extended VP8X files) means it is an animation.
    if b"ANIM" in head[:4096] or b"ANMF" in head[:4096]:
        return Kind.ANIMATED_WEBP
    return Kind.STATIC_WEBP


def _png_kind(head: bytes) -> Kind:
    # `acTL` must appear before the first `IDAT` for a valid APNG.
    actl = head.find(b"acTL")
    idat = head.find(b"IDAT")
    if actl != -1 and (idat == -1 or actl < idat):
        return Kind.APNG
    return Kind.PNG


def iter_mp4_boxes(data: bytes, start: int = 0, end: int | None = None, depth: int = 0):
    """Yield ``(type, payload_start, payload_end)`` for ISO-BMFF boxes.

    Recurses into the handful of container boxes that can hold `vapc`.
    """
    containers = {b"moov", b"udta", b"trak", b"mdia", b"minf", b"stbl", b"meta"}
    end = len(data) if end is None else end
    offset = start
    while offset + 8 <= end:
        (size,) = struct.unpack(">I", data[offset:offset + 4])
        btype = data[offset + 4:offset + 8]
        header = 8
        if size == 1:  # 64-bit extended size
            if offset + 16 > end:
                return
            (size,) = struct.unpack(">Q", data[offset + 8:offset + 16])
            header = 16
        elif size == 0:  # box runs to the end of file
            size = end - offset
        if size < header or offset + size > end:
            return
        yield btype.decode("latin-1"), offset + header, offset + size
        if btype in containers and depth < 6:
            skip = 4 if btype == b"meta" else 0  # `meta` has a version/flags word
            yield from iter_mp4_boxes(data, offset + header + skip, offset + size, depth + 1)
        offset += size


def parse_vapc(data: bytes) -> dict[str, Any] | None:
    """Extract and normalise the VAP (Video Animation Player) config box.

    VAP muxes a JSON blob into a `vapc` box describing where the colour and
    alpha rectangles live inside each frame. Using it beats guessing a 50/50
    split: plenty of VAP files pad, scale or letterbox the alpha plane.
    """
    for btype, begin, finish in iter_mp4_boxes(data):
        if btype != "vapc":
            continue
        payload = data[begin:finish]
        brace = payload.find(b"{")
        if brace == -1:
            continue
        try:
            config = json.loads(payload[brace:].decode("utf-8", "replace").rstrip("\x00"))
        except json.JSONDecodeError as exc:
            LOG.debug("vapc box present but unparsable: %s", exc)
            continue
        info = config.get("info") or {}
        rgb = info.get("rgbFrame")
        alpha = info.get("aFrame")
        if not (isinstance(rgb, list) and isinstance(alpha, list)):
            continue
        return {
            "rgb_rect": [int(v) for v in rgb[:4]],
            "alpha_rect": [int(v) for v in alpha[:4]],
            "width": int(info.get("w") or rgb[2]),
            "height": int(info.get("h") or rgb[3]),
            "video_width": int(info.get("videoW") or 0),
            "video_height": int(info.get("videoH") or 0),
            "fps": float(info.get("fps") or 0) or None,
            "frames": int(info.get("f") or 0),
            "source": "vapc",
            "layout": _layout_from_rects(rgb, alpha),
            "vapx": bool(info.get("isVapx")),
        }
    return None


def _layout_from_rects(rgb: list[int], alpha: list[int]) -> str:
    if alpha[0] >= rgb[0] + rgb[2]:
        return "left-right"
    if rgb[0] >= alpha[0] + alpha[2]:
        return "right-left"
    if alpha[1] >= rgb[1] + rgb[3]:
        return "top-bottom"
    return "bottom-top"


def detect(path: Path | str) -> Detection:
    """Identify a gift asset from its magic bytes."""
    path = Path(path)
    head = _read_head(path)
    if not head:
        return Detection(Kind.UNKNOWN, notes=["empty file"])

    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return Detection(_webp_kind(head))
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        return Detection(_png_kind(head))
    if head[:3] == b"GIF":
        return Detection(Kind.GIF)
    if head[:3] == b"\xff\xd8\xff":
        return Detection(Kind.JPEG)
    if head[:4] == b"PK\x03\x04":
        return Detection(Kind.ZIP, notes=["archive: unpack and re-scan"])
    if head[:3] == b"PAG":
        return Detection(Kind.PAG)
    if head[:2] == b"\x1f\x8b":
        # SVGA 2.x is a gzip-wrapped protobuf. Confirm rather than assume.
        from .formats import svga as svga_mod

        if svga_mod.looks_like_svga(path):
            return Detection(Kind.SVGA)
        return Detection(Kind.UNKNOWN, notes=["gzip payload, not SVGA"])
    if head[4:8] in (b"ftyp", b"styp") or head[4:8] == b"moov":
        return _detect_mp4(path)
    stripped = head.lstrip()[:1]
    if stripped in (b"{", b"["):
        return _detect_json(path)
    return Detection(Kind.UNKNOWN, notes=[f"unrecognised magic {head[:12]!r}"])


def _detect_json(path: Path) -> Detection:
    try:
        payload = json.loads(path.read_text("utf-8", "replace"))
    except (json.JSONDecodeError, OSError):
        return Detection(Kind.UNKNOWN, notes=["unparsable json"])
    if isinstance(payload, dict) and {"v", "layers"} <= set(payload):
        return Detection(Kind.LOTTIE)
    return Detection(Kind.UNKNOWN, notes=["json, not a known animation format"])


def _detect_mp4(path: Path) -> Detection:
    data = path.read_bytes()
    vap = parse_vapc(data)
    if vap:
        return Detection(Kind.VAP_MP4, vap=vap)
    return Detection(Kind.MP4, notes=["no vapc box; alpha layout must be sniffed"])


def sniff_alpha_layout(path: Path | str, samples: int = 3) -> dict[str, Any] | None:
    """Guess the split-alpha layout of a bare mp4 by looking at the pixels.

    A VAP-style frame has one half that is a greyscale matte (R==G==B for
    effectively every pixel) next to a colour half. We score both the
    horizontal and the vertical split and keep the winner, provided the two
    halves are clearly different — an ordinary opaque video scores low on both.
    """
    from PIL import Image  # imported lazily: only videos need it

    from . import ffmpeg as ff

    path = Path(path)
    info = ff.probe(path)
    if not info.width or not info.height:
        return None

    import tempfile

    timestamps = [0.0]
    if info.duration:
        timestamps = [info.duration * frac for frac in (0.15, 0.5, 0.8)][:samples]

    scores: dict[str, list[float]] = {}
    with tempfile.TemporaryDirectory() as tmp:
        for index, when in enumerate(timestamps):
            frame_path = Path(tmp) / f"f{index}.png"
            try:
                ff.extract_frame(path, frame_path, at=when)
            except ff.FFmpegError:
                continue
            if not frame_path.exists():
                continue
            with Image.open(frame_path) as img:
                frame = img.convert("RGB")
            for layout, (a, b) in _split_candidates(frame).items():
                # A matte is grey; the colour plate is not.
                scores.setdefault(layout, []).append(_greyness(b) - _greyness(a))

    if not scores:
        return None
    layout, score = max(
        ((name, sum(vals) / len(vals)) for name, vals in scores.items() if vals),
        key=lambda item: item[1],
    )
    if score < 0.25:  # not convincingly split; treat as a normal opaque video
        return None
    vertical = layout in {"top-bottom", "bottom-top"}
    half_w = info.width // 2 if not vertical else info.width
    half_h = info.height // 2 if vertical else info.height
    rgb_first = layout in {"left-right", "top-bottom"}
    rgb_xy = (0, 0) if rgb_first else ((0, half_h) if vertical else (half_w, 0))
    alpha_xy = ((0, half_h) if vertical else (half_w, 0)) if rgb_first else (0, 0)
    return {
        "rgb_rect": [rgb_xy[0], rgb_xy[1], half_w, half_h],
        "alpha_rect": [alpha_xy[0], alpha_xy[1], half_w, half_h],
        "width": half_w,
        "height": half_h,
        "video_width": info.width,
        "video_height": info.height,
        "fps": info.fps or None,
        "frames": info.frames,
        "source": "sniffed",
        "layout": layout,
        "confidence": round(score, 3),
    }


def _split_candidates(frame):
    w, h = frame.size
    left, right = frame.crop((0, 0, w // 2, h)), frame.crop((w // 2, 0, w, h))
    top, bottom = frame.crop((0, 0, w, h // 2)), frame.crop((0, h // 2, w, h))
    return {
        "left-right": (left, right),   # rgb left, alpha right
        "right-left": (right, left),
        "top-bottom": (top, bottom),
        "bottom-top": (bottom, top),
    }


def _greyness(image) -> float:
    """Fraction-ish score in [0,1]: 1.0 when every pixel has R==G==B."""
    small = image.resize((64, 64))
    raw = small.tobytes()  # tight RGB triples; cheaper than per-pixel access
    count = len(raw) // 3
    if not count:
        return 0.0
    grey = 0
    for offset in range(0, count * 3, 3):
        r, g, b = raw[offset], raw[offset + 1], raw[offset + 2]
        if max(r, g, b) - min(r, g, b) <= 6:
            grey += 1
    return grey / count
