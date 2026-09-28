"""Pillow-backed decoding of animated WebP / GIF / APNG (and stills).

Animated WebP is what TikTok's gift panel actually serves for most gift
artwork, and ffmpeg's native WebP decoder only learned to demux animations
recently. Going through Pillow means one code path that works on every ffmpeg
build and composites partial frames and disposal correctly.
"""
from __future__ import annotations

from pathlib import Path

from ..util import LOG
from .frames import PngSequence

DEFAULT_FPS = 25.0
FRAME_PATTERN = "%05d.png"


def decode(src: Path, workdir: Path, *, max_frames: int | None = None,
           fps_override: float | None = None) -> PngSequence:
    from PIL import Image, ImageSequence

    workdir.mkdir(parents=True, exist_ok=True)
    durations: list[float] = []
    count = 0
    width = height = 0

    with Image.open(src) as img:
        for index, frame in enumerate(ImageSequence.Iterator(img)):
            if max_frames and index >= max_frames:
                LOG.debug("%s: stopping at max_frames=%d", src.name, max_frames)
                break
            rgba = frame.convert("RGBA")
            width, height = rgba.size
            rgba.save(workdir / (FRAME_PATTERN % index), "PNG")
            duration = frame.info.get("duration") or img.info.get("duration") or 0
            durations.append(float(duration))
            count = index + 1

    if count == 0:
        raise ValueError(f"no frames decoded from {src}")

    fps = fps_override or _fps_from_durations(durations)
    return PngSequence(
        fps=fps,
        width=width,
        height=height,
        frame_count=count,
        has_alpha=True,
        directory=workdir,
        pattern=FRAME_PATTERN,
        start_number=0,
    )


def _fps_from_durations(durations: list[float]) -> float:
    """Average frame duration -> fps, ignoring the zeros browsers treat as 100ms."""
    usable = [d for d in durations if d and d > 0]
    if not usable:
        return DEFAULT_FPS
    mean_ms = sum(usable) / len(usable)
    if mean_ms <= 0:
        return DEFAULT_FPS
    fps = 1000.0 / mean_ms
    return max(1.0, min(fps, 60.0))
