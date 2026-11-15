"""Locating and driving the ffmpeg/ffprobe binaries."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Sequence

from .util import LOG


class FFmpegError(RuntimeError):
    """Raised when ffmpeg exits non-zero; carries the tail of its stderr."""


class FFmpegMissing(RuntimeError):
    pass


@lru_cache(maxsize=None)
def ffmpeg_bin() -> str:
    """Path to ffmpeg.

    Order: ``GIFTKIT_FFMPEG`` env var, ``$PATH``, then the binary bundled with
    ``imageio-ffmpeg`` if that happens to be installed (handy on machines where
    the user never installed a system ffmpeg).
    """
    explicit = os.environ.get("GIFTKIT_FFMPEG")
    if explicit:
        return explicit
    found = shutil.which("ffmpeg")
    if found:
        return found
    try:  # optional dependency, only used as a fallback
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:  # pragma: no cover - depends on the host
        raise FFmpegMissing(
            "ffmpeg not found. Install it (brew install ffmpeg / apt install ffmpeg), "
            "or set GIFTKIT_FFMPEG=/path/to/ffmpeg, or `pip install imageio-ffmpeg`."
        )


@lru_cache(maxsize=None)
def ffprobe_bin() -> str | None:
    explicit = os.environ.get("GIFTKIT_FFPROBE")
    if explicit:
        return explicit
    return shutil.which("ffprobe")


def run(args: Sequence[str], *, desc: str = "ffmpeg") -> str:
    """Run ffmpeg (or ffprobe) and return stderr; raise ``FFmpegError`` on failure."""
    LOG.debug("%s: %s", desc, " ".join(str(a) for a in args))
    proc = subprocess.run(
        [str(a) for a in args], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )
    if proc.returncode != 0:
        tail = "\n".join((proc.stderr or "").strip().splitlines()[-15:])
        raise FFmpegError(f"{desc} failed (exit {proc.returncode}):\n{tail}")
    return proc.stdout or proc.stderr


@lru_cache(maxsize=None)
def has_encoder(name: str) -> bool:
    try:
        out = subprocess.run(
            [ffmpeg_bin(), "-hide_banner", "-encoders"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        ).stdout
    except Exception:  # pragma: no cover
        return False
    return bool(re.search(rf"^\s*\S+\s+{re.escape(name)}\b", out, re.MULTILINE))


@dataclass
class MediaInfo:
    width: int = 0
    height: int = 0
    fps: float = 0.0
    duration: float = 0.0
    frames: int = 0
    codec: str = ""
    pix_fmt: str = ""

    @property
    def has_alpha_pix_fmt(self) -> bool:
        """Whether the *decoded* pixel format carries alpha.

        Careful: a VP9 WebM stores alpha in a per-block side channel, so
        ffmpeg's default vp9 decoder reports plain ``yuv420p`` for a file that
        really is transparent. Decode with ``-c:v libvpx-vp9`` to see it.
        """
        return self.pix_fmt.startswith(
            ("yuva", "rgba", "bgra", "argb", "abgr", "gbrap", "ya")
        )


def _parse_rate(value: str | None) -> float:
    if not value or value in {"0/0", "N/A"}:
        return 0.0
    if "/" in value:
        num, _, den = value.partition("/")
        try:
            return float(num) / float(den) if float(den) else 0.0
        except ValueError:
            return 0.0
    try:
        return float(value)
    except ValueError:
        return 0.0


def probe(path: os.PathLike | str) -> MediaInfo:
    """Best-effort media probe. Uses ffprobe when present, else parses ffmpeg -i."""
    probe_bin = ffprobe_bin()
    if probe_bin:
        try:
            out = subprocess.run(
                [
                    probe_bin, "-v", "error", "-show_streams", "-select_streams", "v:0",
                    "-of", "json", str(path),
                ],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            ).stdout
            streams = json.loads(out or "{}").get("streams") or []
            if streams:
                s = streams[0]
                return MediaInfo(
                    width=int(s.get("width") or 0),
                    height=int(s.get("height") or 0),
                    fps=_parse_rate(s.get("avg_frame_rate") or s.get("r_frame_rate")),
                    duration=float(s.get("duration") or 0.0),
                    frames=int(s.get("nb_frames") or 0),
                    codec=s.get("codec_name") or "",
                    pix_fmt=s.get("pix_fmt") or "",
                )
        except Exception as exc:  # pragma: no cover - fall through to ffmpeg
            LOG.debug("ffprobe failed on %s (%s); falling back to ffmpeg -i", path, exc)
    return _probe_via_ffmpeg(path)


# ffmpeg's human-readable stream line, e.g.
#   Stream #0:0: Video: vp9 (Profile 0), yuv420p(tv, progressive), 80x80, 20 fps
# The pixel format sits in an unpredictable position, so match it by shape
# rather than by comma position.
_VIDEO_LINE_RE = re.compile(r"Stream #\d+:\d+.*?: Video: (?P<codec>[\w]+)")
_PIX_RE = re.compile(
    r"\b(yuva?\d{3}p(?:\d{1,2}(?:le|be))?|yuvj\d{3}p|rgba|bgra|argb|abgr|rgb24|bgr24|"
    r"gbrap?\d*(?:le|be)?|gray\w*|pal8|nv12|nv21|monob)\b"
)
_SIZE_RE = re.compile(r"\b(\d{2,5})x(\d{2,5})\b")
_FPS_RE = re.compile(r"([\d.]+) fps")
_DUR_RE = re.compile(r"Duration: (\d+):(\d+):(\d+\.\d+)")


def _probe_via_ffmpeg(path: os.PathLike | str) -> MediaInfo:
    proc = subprocess.run(
        [ffmpeg_bin(), "-hide_banner", "-i", str(path)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    text = proc.stderr or ""
    info = MediaInfo()
    line = next((ln for ln in text.splitlines() if "Video:" in ln), "")
    if line:
        codec = _VIDEO_LINE_RE.search(line)
        if codec:
            info.codec = codec.group("codec")
        pix = _PIX_RE.search(line)
        if pix:
            info.pix_fmt = pix.group(1)
        size = _SIZE_RE.search(line)
        if size:
            info.width, info.height = int(size.group(1)), int(size.group(2))
        fps = _FPS_RE.search(line)
        if fps:
            info.fps = float(fps.group(1))
    dur = _DUR_RE.search(text)
    if dur:
        info.duration = int(dur.group(1)) * 3600 + int(dur.group(2)) * 60 + float(dur.group(3))
    return info


def extract_frame(src: os.PathLike | str, dest: os.PathLike | str, at: float = 0.0) -> Path:
    """Pull a single frame out of a video as PNG (used for alpha-layout sniffing)."""
    args = [ffmpeg_bin(), "-y", "-v", "error"]
    if at > 0:
        args += ["-ss", f"{at:.3f}"]
    args += ["-i", str(src), "-frames:v", "1", str(dest)]
    run(args, desc="ffmpeg extract-frame")
    return Path(dest)
