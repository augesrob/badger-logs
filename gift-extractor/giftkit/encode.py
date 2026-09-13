"""Encoding a decoded gift into a delivery format.

The default target is VP9 WebM with a real alpha plane (``yuva420p``), which
Chromium - and therefore an OBS browser source - composites natively. ProRes
4444 is offered for people dropping the assets into an NLE, where VP9 alpha
support is patchy.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from . import ffmpeg as ff
from .formats.frames import FrameSource
from .util import LOG

TARGETS = ("webm", "webm-vp8", "mov", "apng", "gif", "png-seq")


@dataclass
class EncodeOptions:
    target: str = "webm"
    crf: int = 28
    fps: float | None = None        # None keeps the source rate
    max_width: int | None = None    # downscale guard for 2048px full-screen effects
    lossless: bool = False
    threads: int = 0

    def suffix(self) -> str:
        return {
            "webm": ".webm",
            "webm-vp8": ".webm",
            "mov": ".mov",
            "apng": ".png",
            "gif": ".gif",
            "png-seq": "",
        }[self.target]


def _scale_filter(source: FrameSource, options: EncodeOptions) -> str | None:
    if not options.max_width or not source.width:
        return None
    if source.width <= options.max_width:
        return None
    # -2 keeps the height even, which yuva420p requires.
    return f"scale={options.max_width}:-2:flags=lanczos"


def encode(source: FrameSource, dest: Path, options: EncodeOptions) -> Path:
    """Run ffmpeg for one asset and return the written path."""
    if options.target == "png-seq":
        return _copy_png_sequence(source, dest)

    dest.parent.mkdir(parents=True, exist_ok=True)
    args: list[str] = [ff.ffmpeg_bin(), "-y", "-v", "error", "-nostdin"]
    args += source.input_args()

    scale = _scale_filter(source, options)
    filter_args = source.filter_args()
    map_args = source.map_args()
    if filter_args and scale:
        # Append the scale to the existing graph so we keep a single output label.
        graph = filter_args[1]
        label = source.map_args()[1].strip("[]") if map_args else "out"
        filter_args = ["-filter_complex", f"{graph};[{label}]{scale}[scaled]"]
        map_args = ["-map", "[scaled]"]
    elif scale:
        filter_args = ["-vf", scale]
    args += filter_args + map_args

    if options.fps:
        args += ["-r", f"{options.fps:.6f}"]
    args += _codec_args(options)
    if options.threads:
        args += ["-threads", str(options.threads)]
    args.append(str(dest))

    ff.run(args, desc=f"encode {dest.name}")
    if not dest.exists() or dest.stat().st_size == 0:
        raise ff.FFmpegError(f"encoder produced no output for {dest}")
    return dest


def _codec_args(options: EncodeOptions) -> list[str]:
    if options.target == "webm":
        args = [
            "-c:v", "libvpx-vp9",
            "-pix_fmt", "yuva420p",
            # Alt-ref frames are incompatible with alpha in libvpx; leaving this
            # on is the classic cause of "my WebM has a black background".
            "-auto-alt-ref", "0",
            "-row-mt", "1",
            "-deadline", "good",
            "-cpu-used", "2",
            "-an",
        ]
        if options.lossless:
            return args + ["-lossless", "1"]
        return args + ["-b:v", "0", "-crf", str(options.crf)]
    if options.target == "webm-vp8":
        return [
            "-c:v", "libvpx", "-pix_fmt", "yuva420p", "-auto-alt-ref", "0",
            "-b:v", "0", "-crf", str(options.crf), "-qmin", "4", "-qmax", "48", "-an",
        ]
    if options.target == "mov":
        return [
            "-c:v", "prores_ks", "-profile:v", "4444",
            "-pix_fmt", "yuva444p10le", "-alpha_bits", "16", "-an",
        ]
    if options.target == "apng":
        return ["-c:v", "apng", "-plays", "0", "-pix_fmt", "rgba", "-an"]
    if options.target == "gif":
        # GIF has 1-bit alpha; fine for previews, lossy for real overlays.
        return ["-c:v", "gif", "-loop", "0", "-an"]
    raise ValueError(f"unknown target {options.target!r}")


def _copy_png_sequence(source: FrameSource, dest: Path) -> Path:
    from .formats.frames import PngSequence

    if not isinstance(source, PngSequence):
        raise ValueError("png-seq output requires a Python-decoded format")
    dest.mkdir(parents=True, exist_ok=True)
    count = 0
    for frame in sorted(source.directory.glob("*.png")):
        (dest / frame.name).write_bytes(frame.read_bytes())
        count += 1
    LOG.debug("copied %d frames to %s", count, dest)
    return dest


def preferred_target(requested: str) -> str:
    """Fall back gracefully when the local ffmpeg lacks the ideal encoder."""
    if requested == "webm" and not ff.has_encoder("libvpx-vp9"):
        if ff.has_encoder("libvpx"):
            LOG.warning("libvpx-vp9 unavailable; falling back to VP8 alpha WebM")
            return "webm-vp8"
        raise ff.FFmpegMissing("this ffmpeg has no VP9/VP8 encoder; rebuild with libvpx")
    if requested == "mov" and not ff.has_encoder("prores_ks"):
        raise ff.FFmpegMissing("this ffmpeg has no prores_ks encoder")
    return requested
