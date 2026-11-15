"""PAG (Portable Animated Graphics) decoding via an external renderer.

PAG is Tencent's format and its only complete implementation is libpag, so
unlike SVGA there is no sane pure-Python path. Instead we shell out, with two
interchangeable strategies:

``--pag-cmd "<template>"``
    Any CLI that writes a PNG sequence. ``{input}`` and ``{outdir}`` are
    substituted, e.g. ``"pag2png {input} {outdir}"``.

bundled Node renderer (``tools/pag_render.mjs``)
    Uses the ``libpag`` WebAssembly build. Opt in by running
    ``npm install`` inside ``tools/`` once; ``giftkit doctor`` reports whether
    it is usable.

When neither is available the asset is recorded as skipped with this reason
rather than silently dropped.
"""
from __future__ import annotations

import shlex
import shutil
import subprocess
from pathlib import Path

from ..util import LOG
from .frames import PngSequence

FRAME_PATTERN = "%05d.png"
NODE_RENDERER = Path(__file__).resolve().parents[2] / "tools" / "pag_render.mjs"


class RendererUnavailable(RuntimeError):
    """No PAG renderer is configured on this machine."""


def node_renderer_ready() -> bool:
    if not NODE_RENDERER.exists() or not shutil.which("node"):
        return False
    return (NODE_RENDERER.parent / "node_modules" / "libpag").exists()


def decode(src: Path, workdir: Path, *, command: str | None = None,
           fps: float = 30.0, max_frames: int | None = None) -> PngSequence:
    workdir.mkdir(parents=True, exist_ok=True)
    if command:
        _run_template(command, src, workdir)
    elif node_renderer_ready():
        _run_node(src, workdir, fps=fps, max_frames=max_frames)
    else:
        raise RendererUnavailable(
            "no PAG renderer configured. Either pass --pag-cmd '<cli> {input} {outdir}', "
            f"or run `npm install` in {NODE_RENDERER.parent} to enable the bundled "
            "libpag renderer."
        )

    produced = sorted(workdir.glob("*.png"))
    if not produced:
        raise RendererUnavailable(f"PAG renderer wrote no frames for {src.name}")
    _renumber(produced, workdir)

    from PIL import Image

    with Image.open(workdir / (FRAME_PATTERN % 0)) as first:
        width, height = first.size
    return PngSequence(
        fps=fps,
        width=width,
        height=height,
        frame_count=len(produced),
        has_alpha=True,
        notes=["rendered with an external PAG renderer"],
        directory=workdir,
        pattern=FRAME_PATTERN,
        start_number=0,
    )


def _run_template(command: str, src: Path, workdir: Path) -> None:
    rendered = command.replace("{input}", shlex.quote(str(src))).replace(
        "{outdir}", shlex.quote(str(workdir))
    )
    LOG.debug("pag renderer: %s", rendered)
    proc = subprocess.run(rendered, shell=True, capture_output=True, text=True)
    if proc.returncode != 0:
        tail = "\n".join((proc.stderr or "").strip().splitlines()[-10:])
        raise RendererUnavailable(f"PAG renderer exited {proc.returncode}:\n{tail}")


def _run_node(src: Path, workdir: Path, *, fps: float, max_frames: int | None) -> None:
    args = ["node", str(NODE_RENDERER), "--input", str(src), "--outdir", str(workdir),
            "--fps", str(fps)]
    if max_frames:
        args += ["--max-frames", str(max_frames)]
    proc = subprocess.run(args, capture_output=True, text=True, cwd=NODE_RENDERER.parent)
    if proc.returncode != 0:
        tail = "\n".join((proc.stderr or "").strip().splitlines()[-10:])
        raise RendererUnavailable(f"bundled PAG renderer failed:\n{tail}")


def _renumber(produced: list[Path], workdir: Path) -> None:
    """Normalise whatever naming the renderer used into %05d.png."""
    for index, path in enumerate(produced):
        target = workdir / (FRAME_PATTERN % index)
        if path != target:
            path.replace(target)
