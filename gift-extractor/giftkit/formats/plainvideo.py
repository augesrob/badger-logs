"""Ordinary opaque video: no alpha to recover, just normalise and re-encode."""
from __future__ import annotations

from pathlib import Path

from .. import ffmpeg as ff
from .frames import FilteredVideo


def decode(src: Path) -> FilteredVideo:
    info = ff.probe(src)
    return FilteredVideo(
        fps=info.fps or 30.0,
        width=info.width,
        height=info.height,
        frame_count=info.frames,
        has_alpha=False,
        notes=["no alpha channel detected; output will be opaque"],
        path=src,
        filter_complex="[0:v]format=rgba[out]",
        out_label="out",
    )
