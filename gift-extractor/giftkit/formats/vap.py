"""VAP / split-alpha video decoding.

A VAP mp4 packs colour and matte side by side in one opaque h264 frame. We crop
both rectangles and recombine them with ``alphamerge``, which stays entirely
inside ffmpeg - no intermediate PNG sequence, so a 1080p full-screen effect
converts in seconds instead of minutes.

The rectangles come from the file's own ``vapc`` config box when present
(exact), and from a pixel heuristic otherwise (see ``detect.sniff_alpha_layout``).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from .. import ffmpeg as ff
from ..util import LOG
from .frames import FilteredVideo


def build_filter(vap: dict[str, Any]) -> str:
    rx, ry, rw, rh = vap["rgb_rect"]
    ax, ay, aw, ah = vap["alpha_rect"]
    # yuva420p needs even dimensions; crop to even so the encoder never pads.
    rw, rh = rw - (rw % 2), rh - (rh % 2)
    chain = [
        f"[0:v]crop={rw}:{rh}:{rx}:{ry},format=rgba[rgb]",
    ]
    alpha = f"[0:v]crop={aw}:{ah}:{ax}:{ay},format=gray"
    if (aw, ah) != (rw, rh):
        # Some encoders ship a half-resolution matte to save bitrate.
        alpha += f",scale={rw}:{rh}:flags=bilinear"
    chain.append(alpha + "[alpha]")
    chain.append("[rgb][alpha]alphamerge[out]")
    return ";".join(chain)


def decode(src: Path, vap: dict[str, Any]) -> FilteredVideo:
    info = ff.probe(src)
    rw, rh = vap["rgb_rect"][2], vap["rgb_rect"][3]
    notes = [f"alpha layout: {vap.get('layout')} ({vap.get('source')})"]
    if vap.get("vapx"):
        notes.append("VAPX file: dynamic text/image slots are not rendered")
    fps = vap.get("fps") or info.fps or 30.0
    LOG.debug("%s: vap %s rgb=%s alpha=%s", src.name, vap.get("source"),
              vap["rgb_rect"], vap["alpha_rect"])
    return FilteredVideo(
        fps=float(fps),
        width=rw - (rw % 2),
        height=rh - (rh % 2),
        frame_count=vap.get("frames") or info.frames,
        has_alpha=True,
        notes=notes,
        path=src,
        filter_complex=build_filter(vap),
        out_label="out",
    )
