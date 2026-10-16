"""Synthetic gift assets.

Real gift files cannot be committed, so the suite builds byte-accurate stand-ins
for each format the pipeline claims to support: an animated WebP, a VAP mp4
(with and without a ``vapc`` box), an opaque control video, an SVGA movie
encoded by a miniature protobuf writer, and a zip bundle.
"""
from __future__ import annotations

import io
import json
import struct
import subprocess
import tempfile
import zipfile
from pathlib import Path

from PIL import Image, ImageDraw

from giftkit import ffmpeg as ff


# -- images ----------------------------------------------------------------

def colour_frame(size: tuple[int, int], index: int) -> Image.Image:
    """A frame that is unmistakably colourful (used as the RGB plate)."""
    image = Image.new("RGB", size, (10, 10, 30))
    draw = ImageDraw.Draw(image)
    offset = (index * 7) % max(1, size[0])
    draw.ellipse([offset, 4, offset + size[0] // 2, size[1] - 4], fill=(230, 40, 90))
    draw.rectangle([0, 0, size[0] // 4, size[1] // 4], fill=(40, 200, 120))
    return image


def matte_frame(size: tuple[int, int], index: int) -> Image.Image:
    """A grey matte matching ``colour_frame``'s ellipse."""
    image = Image.new("RGB", size, (0, 0, 0))
    draw = ImageDraw.Draw(image)
    offset = (index * 7) % max(1, size[0])
    draw.ellipse([offset, 4, offset + size[0] // 2, size[1] - 4], fill=(255, 255, 255))
    return image


def rgba_frame(size: tuple[int, int], index: int) -> Image.Image:
    rgba = colour_frame(size, index).convert("RGBA")
    rgba.putalpha(matte_frame(size, index).convert("L"))
    return rgba


def make_animated_webp(path: Path, frames: int = 6, size=(64, 64), duration=40) -> Path:
    images = [rgba_frame(size, i) for i in range(frames)]
    images[0].save(path, "WEBP", save_all=True, append_images=images[1:],
                   duration=duration, loop=0, lossless=True)
    return path


def make_gif(path: Path, frames: int = 4, size=(48, 48)) -> Path:
    images = [rgba_frame(size, i) for i in range(frames)]
    images[0].save(path, "GIF", save_all=True, append_images=images[1:],
                   duration=100, loop=0, transparency=0, disposal=2)
    return path


def make_png(path: Path, size=(32, 32)) -> Path:
    rgba_frame(size, 0).save(path, "PNG")
    return path


# -- video -----------------------------------------------------------------

def _encode_png_dir(frame_dir: Path, dest: Path, fps: int = 10) -> Path:
    ff.run(
        [ff.ffmpeg_bin(), "-y", "-v", "error", "-framerate", str(fps),
         "-i", str(frame_dir / "%05d.png"), "-c:v", "libx264",
         "-pix_fmt", "yuv420p", "-crf", "18", str(dest)],
        desc="fixture encode",
    )
    return dest


def make_split_alpha_mp4(path: Path, *, layout: str = "top-bottom", frames: int = 8,
                         half=(64, 64), fps: int = 10) -> Path:
    """Build a VAP-style mp4 with colour and matte packed into one frame."""
    vertical = layout in {"top-bottom", "bottom-top"}
    canvas = (half[0], half[1] * 2) if vertical else (half[0] * 2, half[1])
    with tempfile.TemporaryDirectory() as tmp:
        frame_dir = Path(tmp)
        for index in range(frames):
            canvas_image = Image.new("RGB", canvas, (0, 0, 0))
            colour, matte = colour_frame(half, index), matte_frame(half, index)
            first, second = (colour, matte) if layout in {"top-bottom", "left-right"} \
                else (matte, colour)
            canvas_image.paste(first, (0, 0))
            canvas_image.paste(second, (0, half[1]) if vertical else (half[0], 0))
            canvas_image.save(frame_dir / f"{index:05d}.png")
        return _encode_png_dir(frame_dir, path, fps=fps)


def make_opaque_mp4(path: Path, frames: int = 8, size=(64, 128), fps: int = 10) -> Path:
    """A control video: colourful on both halves, so it must not look split."""
    with tempfile.TemporaryDirectory() as tmp:
        frame_dir = Path(tmp)
        for index in range(frames):
            colour_frame(size, index).save(frame_dir / f"{index:05d}.png")
        return _encode_png_dir(frame_dir, path, fps=fps)


def add_vapc_box(path: Path, *, rgb_rect, alpha_rect, fps: int = 10,
                 frames: int = 8) -> Path:
    """Append a VAP config box describing the alpha layout.

    VAP writes this into the container; appending a top-level box leaves the
    file playable while giving the detector something real to parse.
    """
    width, height = rgb_rect[2], rgb_rect[3]
    config = {
        "info": {
            "v": 2, "f": frames, "w": width, "h": height, "fps": fps,
            "videoW": max(rgb_rect[0] + rgb_rect[2], alpha_rect[0] + alpha_rect[2]),
            "videoH": max(rgb_rect[1] + rgb_rect[3], alpha_rect[1] + alpha_rect[3]),
            "orien": 0, "rgbFrame": list(rgb_rect), "aFrame": list(alpha_rect),
            "isVapx": 0,
        },
        "src": [], "frame": [],
    }
    payload = json.dumps(config).encode("utf-8")
    box = struct.pack(">I", len(payload) + 8) + b"vapc" + payload
    with open(path, "ab") as handle:
        handle.write(box)
    return path


# -- svga ------------------------------------------------------------------

def _varint(value: int) -> bytes:
    out = b""
    while True:
        byte = value & 0x7F
        value >>= 7
        out += bytes([byte | (0x80 if value else 0)])
        if not value:
            return out


def _tag(field: int, wire: int) -> bytes:
    return _varint((field << 3) | wire)


def _len_field(field: int, payload: bytes) -> bytes:
    return _tag(field, 2) + _varint(len(payload)) + payload


def _float_field(field: int, value: float) -> bytes:
    return _tag(field, 5) + struct.pack("<f", value)


def _varint_field(field: int, value: int) -> bytes:
    return _tag(field, 0) + _varint(value)


def make_svga(path: Path, *, size=(80, 80), frames: int = 5, fps: int = 20,
              sprite_size=(20, 20)) -> Path:
    """Encode a minimal but valid SVGA 2.x movie: one sprite sliding across."""
    sprite = Image.new("RGBA", sprite_size, (255, 0, 0, 255))
    ImageDraw.Draw(sprite).ellipse([0, 0, sprite_size[0] - 1, sprite_size[1] - 1],
                                   fill=(0, 128, 255, 255))
    buffer = io.BytesIO()
    sprite.save(buffer, "PNG")
    image_bytes = buffer.getvalue()

    params = (_float_field(1, size[0]) + _float_field(2, size[1])
              + _varint_field(3, fps) + _varint_field(4, frames))

    image_entry = _len_field(1, b"ball") + _len_field(2, image_bytes)

    frame_blobs = b""
    for index in range(frames):
        tx = index * (size[0] - sprite_size[0]) / max(1, frames - 1)
        transform = (_float_field(1, 1.0) + _float_field(2, 0.0) + _float_field(3, 0.0)
                     + _float_field(4, 1.0) + _float_field(5, tx) + _float_field(6, 10.0))
        layout = (_float_field(1, 0.0) + _float_field(2, 0.0)
                  + _float_field(3, sprite_size[0]) + _float_field(4, sprite_size[1]))
        frame = _float_field(1, 1.0) + _len_field(2, layout) + _len_field(3, transform)
        frame_blobs += _len_field(2, frame)
    sprite_blob = _len_field(1, b"ball") + frame_blobs

    movie = (_len_field(1, b"2.0.0") + _len_field(2, params)
             + _len_field(3, image_entry) + _len_field(4, sprite_blob))

    import gzip

    path.write_bytes(gzip.compress(movie))
    return path


# -- archives --------------------------------------------------------------

def make_zip_bundle(path: Path, members: dict[str, Path]) -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        for name, source in members.items():
            archive.write(source, arcname=name)
    return path


def ffmpeg_available() -> bool:
    try:
        ff.ffmpeg_bin()
    except Exception:
        return False
    return subprocess.run([ff.ffmpeg_bin(), "-version"], capture_output=True).returncode == 0


def decode_rgba_frame(video: Path, index: int = 0) -> "Image.Image":
    """Decode one frame of a converted file back to RGBA, alpha intact.

    libvpx-vp9 is named explicitly: the default vp9 decoder drops the alpha
    side channel, which is exactly the trap this helper exists to avoid.
    """
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "frame.png"
        ff.run(
            [ff.ffmpeg_bin(), "-y", "-v", "error", "-c:v", "libvpx-vp9", "-i", str(video),
             "-vf", f"select=eq(n\\,{index})", "-vsync", "0", "-frames:v", "1",
             "-pix_fmt", "rgba", "-c:v", "png", str(out)],
            desc="fixture decode",
        )
        with Image.open(out) as image:
            return image.convert("RGBA")
