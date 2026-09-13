"""The hand-off between decoders and the encoder.

A decoder returns a :class:`FrameSource`, which knows how to present itself to
ffmpeg. Two flavours exist because some formats are cheapest to decode in
Python (animated WebP, SVGA) while others should never leave ffmpeg (VAP mp4,
where a PNG round-trip would cost minutes and gigabytes for no gain).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class FrameSource:
    fps: float = 30.0
    width: int = 0
    height: int = 0
    frame_count: int = 0
    has_alpha: bool = True
    notes: list[str] = field(default_factory=list)

    def input_args(self) -> list[str]:  # pragma: no cover - interface
        raise NotImplementedError

    def filter_args(self) -> list[str]:
        return []

    def map_args(self) -> list[str]:
        return []


@dataclass
class PngSequence(FrameSource):
    """A directory of zero-padded RGBA PNGs."""

    directory: Path = Path()
    pattern: str = "%05d.png"
    start_number: int = 0

    def input_args(self) -> list[str]:
        return [
            "-framerate", f"{self.fps:.6f}",
            "-start_number", str(self.start_number),
            "-i", str(self.directory / self.pattern),
        ]


@dataclass
class FilteredVideo(FrameSource):
    """A video file plus a filter graph that produces an RGBA stream."""

    path: Path = Path()
    filter_complex: str = ""
    out_label: str = "out"

    def input_args(self) -> list[str]:
        return ["-i", str(self.path)]

    def filter_args(self) -> list[str]:
        return ["-filter_complex", self.filter_complex] if self.filter_complex else []

    def map_args(self) -> list[str]:
        return ["-map", f"[{self.out_label}]"] if self.filter_complex else []
