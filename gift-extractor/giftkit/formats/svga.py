"""Native SVGA 2.x decoder and renderer.

An ``.svga`` file is a gzip-compressed protobuf ``MovieEntity``: a bag of PNG
sprites plus, per frame, an affine transform and alpha for each sprite. That is
little enough that rendering it here - with a ~100 line protobuf wire reader and
Pillow - beats the usual advice of driving a headless browser: no Node, no
Puppeteer, no canvas capture, and it is deterministic.

Limitations, reported as notes rather than silently ignored:

* vector ``shapes`` layers (field 5 of a frame) are not rasterised; sprite
  layers, which carry the artwork in practically every gift, are;
* ``clipPath`` masks and ``matteKey`` mattes are skipped;
* audio tracks are dropped (these are overlay assets, not clips).
"""
from __future__ import annotations

import gzip
import io
import struct
from dataclasses import dataclass, field
from pathlib import Path

from ..util import LOG
from .frames import PngSequence

FRAME_PATTERN = "%05d.png"
WIRE_VARINT, WIRE_64, WIRE_LEN, WIRE_32 = 0, 1, 2, 5


# -- protobuf wire reader --------------------------------------------------

def read_fields(data: bytes) -> dict[int, list]:
    """Decode protobuf wire format into ``{field_number: [values]}``.

    Values are ``int`` for varints, ``bytes`` for length-delimited fields and
    ``float`` for 32-bit fields. Unknown fields simply land in the dict.
    """
    out: dict[int, list] = {}
    pos, size = 0, len(data)
    while pos < size:
        key, pos = _read_varint(data, pos)
        field_no, wire = key >> 3, key & 0x07
        if wire == WIRE_VARINT:
            value, pos = _read_varint(data, pos)
        elif wire == WIRE_LEN:
            length, pos = _read_varint(data, pos)
            if pos + length > size:
                raise ValueError("truncated length-delimited field")
            value, pos = data[pos:pos + length], pos + length
        elif wire == WIRE_32:
            if pos + 4 > size:
                raise ValueError("truncated 32-bit field")
            value = struct.unpack("<f", data[pos:pos + 4])[0]
            pos += 4
        elif wire == WIRE_64:
            if pos + 8 > size:
                raise ValueError("truncated 64-bit field")
            value = struct.unpack("<d", data[pos:pos + 8])[0]
            pos += 8
        else:
            raise ValueError(f"unsupported wire type {wire}")
        out.setdefault(field_no, []).append(value)
    return out


def _read_varint(data: bytes, pos: int) -> tuple[int, int]:
    result = shift = 0
    while True:
        if pos >= len(data):
            raise ValueError("truncated varint")
        byte = data[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, pos
        shift += 7
        if shift > 63:
            raise ValueError("varint too long")


def _one(fields: dict[int, list], number: int, default=None):
    values = fields.get(number)
    return values[0] if values else default


def _f(fields: dict[int, list], number: int, default: float = 0.0) -> float:
    value = _one(fields, number)
    return float(value) if isinstance(value, (int, float)) else default


def _s(fields: dict[int, list], number: int, default: str = "") -> str:
    value = _one(fields, number)
    return value.decode("utf-8", "replace") if isinstance(value, bytes) else default


# -- model -----------------------------------------------------------------

@dataclass
class SvgaFrame:
    alpha: float = 1.0
    layout: tuple[float, float, float, float] | None = None
    transform: tuple[float, float, float, float, float, float] | None = None
    has_shapes: bool = False
    has_clip: bool = False


@dataclass
class SvgaSprite:
    image_key: str = ""
    matte_key: str = ""
    frames: list[SvgaFrame] = field(default_factory=list)


@dataclass
class SvgaMovie:
    version: str = ""
    width: int = 0
    height: int = 0
    fps: float = 20.0
    frames: int = 0
    images: dict[str, bytes] = field(default_factory=dict)
    sprites: list[SvgaSprite] = field(default_factory=list)

    @property
    def has_vector_shapes(self) -> bool:
        return any(f.has_shapes for s in self.sprites for f in s.frames)


def parse(path: Path | str) -> SvgaMovie:
    raw = gzip.decompress(Path(path).read_bytes())
    return parse_bytes(raw)


def parse_bytes(raw: bytes) -> SvgaMovie:
    root = read_fields(raw)
    params = read_fields(_one(root, 2, b"") or b"")
    movie = SvgaMovie(
        version=_s(root, 1),
        width=int(_f(params, 1)),
        height=int(_f(params, 2)),
        fps=float(_one(params, 3, 20) or 20),
        frames=int(_one(params, 4, 0) or 0),
    )
    for entry in root.get(3, []):  # map<string, bytes> images
        pair = read_fields(entry)
        key = _s(pair, 1)
        value = _one(pair, 2, b"")
        if key and isinstance(value, bytes):
            movie.images[key] = value
    for entry in root.get(4, []):  # repeated SpriteEntity
        movie.sprites.append(_parse_sprite(entry))
    if not movie.frames:
        movie.frames = max((len(s.frames) for s in movie.sprites), default=0)
    return movie


def _parse_sprite(blob: bytes) -> SvgaSprite:
    fields = read_fields(blob)
    sprite = SvgaSprite(image_key=_s(fields, 1), matte_key=_s(fields, 3))
    for frame_blob in fields.get(2, []):
        sprite.frames.append(_parse_frame(frame_blob))
    return sprite


def _parse_frame(blob: bytes) -> SvgaFrame:
    fields = read_fields(blob)
    frame = SvgaFrame(alpha=_f(fields, 1, 0.0))
    layout = _one(fields, 2)
    if isinstance(layout, bytes):
        lf = read_fields(layout)
        frame.layout = (_f(lf, 1), _f(lf, 2), _f(lf, 3), _f(lf, 4))
    transform = _one(fields, 3)
    if isinstance(transform, bytes):
        tf = read_fields(transform)
        frame.transform = (
            _f(tf, 1, 1.0), _f(tf, 2, 0.0), _f(tf, 3, 0.0),
            _f(tf, 4, 1.0), _f(tf, 5, 0.0), _f(tf, 6, 0.0),
        )
    frame.has_clip = bool(_s(fields, 4))
    frame.has_shapes = bool(fields.get(5))
    return frame


def looks_like_svga(path: Path | str) -> bool:
    """Cheap validation used by the sniffer: gzip alone is not proof."""
    try:
        with gzip.open(path, "rb") as handle:
            head = handle.read(1 << 16)
        movie = parse_bytes(head) if len(head) < (1 << 16) else None
    except Exception:
        return False
    if movie is not None:
        return bool(movie.version and movie.width and movie.height)
    # Large file: only the header was read, so re-parse the full payload.
    try:
        movie = parse(path)
    except Exception:
        return False
    return bool(movie.version and movie.width and movie.height)


# -- rendering -------------------------------------------------------------

def render(movie: SvgaMovie, workdir: Path, *, max_frames: int | None = None) -> PngSequence:
    from PIL import Image

    workdir.mkdir(parents=True, exist_ok=True)
    if not movie.width or not movie.height:
        raise ValueError("SVGA has no viewBox size")
    canvas_size = (movie.width, movie.height)

    bitmaps: dict[str, Image.Image] = {}
    for key, blob in movie.images.items():
        try:
            bitmaps[key] = Image.open(io.BytesIO(blob)).convert("RGBA")
        except Exception as exc:  # noqa: BLE001 - a non-image entry is survivable
            LOG.debug("svga image %s not decodable: %s", key, exc)

    total = movie.frames or max((len(s.frames) for s in movie.sprites), default=0)
    if max_frames:
        total = min(total, max_frames)
    if total <= 0:
        raise ValueError("SVGA declares no frames")

    for index in range(total):
        canvas = Image.new("RGBA", canvas_size, (0, 0, 0, 0))
        for sprite in movie.sprites:
            if index >= len(sprite.frames):
                continue
            frame = sprite.frames[index]
            if frame.alpha <= 0.01 or frame.transform is None:
                continue
            bitmap = bitmaps.get(sprite.image_key) or bitmaps.get(f"{sprite.image_key}.png")
            if bitmap is None:
                continue
            layer = _place(bitmap, frame, canvas_size)
            if layer is not None:
                canvas = Image.alpha_composite(canvas, layer)
        canvas.save(workdir / (FRAME_PATTERN % index), "PNG")

    notes = []
    if movie.has_vector_shapes:
        notes.append("contains vector shape layers, which are not rasterised")
    if any(s.matte_key for s in movie.sprites):
        notes.append("contains matte layers, which are ignored")
    return PngSequence(
        fps=movie.fps or 20.0,
        width=movie.width,
        height=movie.height,
        frame_count=total,
        has_alpha=True,
        notes=notes,
        directory=workdir,
        pattern=FRAME_PATTERN,
        start_number=0,
    )


def _place(bitmap, frame: SvgaFrame, canvas_size: tuple[int, int]):
    """Apply a frame's layout + affine transform, returning a canvas-sized layer."""
    from PIL import Image

    source = bitmap
    if frame.layout and frame.layout[2] > 0 and frame.layout[3] > 0:
        lw, lh = int(round(frame.layout[2])), int(round(frame.layout[3]))
        if (lw, lh) != source.size:
            source = source.resize((max(1, lw), max(1, lh)), Image.LANCZOS)

    a, b, c, d, tx, ty = frame.transform
    determinant = a * d - b * c
    if abs(determinant) < 1e-9:  # degenerate (fully collapsed) frame
        return None
    # PIL maps destination -> source, so feed it the inverse matrix.
    inv_a, inv_c = d / determinant, -c / determinant
    inv_b, inv_d = -b / determinant, a / determinant
    inv_tx = (c * ty - d * tx) / determinant
    inv_ty = (b * tx - a * ty) / determinant

    layer = source.transform(
        canvas_size,
        Image.AFFINE,
        (inv_a, inv_c, inv_tx, inv_b, inv_d, inv_ty),
        resample=Image.BILINEAR,
    )
    if frame.alpha < 0.999:
        alpha = layer.getchannel("A").point(lambda v: int(v * frame.alpha))
        layer.putalpha(alpha)
    return layer


def decode(src: Path, workdir: Path, *, max_frames: int | None = None) -> PngSequence:
    return render(parse(src), workdir, max_frames=max_frames)
