"""Format detection -> decode -> encode, for one asset or a whole manifest."""
from __future__ import annotations

import shutil
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from . import ffmpeg as ff
from .detect import Detection, Kind, detect, sniff_alpha_layout
from .encode import EncodeOptions, encode, preferred_target
from .formats import imgseq, pag, plainvideo, svga, vap
from .util import LOG, ensure_dir, sha256_file, unique_path

STILL_KINDS = {Kind.PNG, Kind.STATIC_WEBP, Kind.JPEG}


@dataclass
class ConvertOptions:
    out_dir: Path = Path("out")
    target: str = "webm"
    crf: int = 28
    fps: float | None = None
    max_width: int | None = None
    max_frames: int | None = None
    overwrite: bool = False
    keep_stills: bool = True
    skip_opaque: bool = False
    force_layout: str | None = None   # left-right | right-left | top-bottom | bottom-top
    pag_cmd: str | None = None
    workers: int = 4

    def encode_options(self) -> EncodeOptions:
        return EncodeOptions(
            target=self.target, crf=self.crf, fps=self.fps,
            max_width=self.max_width,
        )


@dataclass
class ConvertResult:
    source: Path
    status: str = "converted"        # converted | still | skipped | failed | duplicate
    kind: str = Kind.UNKNOWN.value
    output: Path | None = None
    width: int = 0
    height: int = 0
    fps: float = 0.0
    frames: int = 0
    has_alpha: bool = False
    sha256: str | None = None
    reason: str | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status in {"converted", "still", "duplicate"}


def convert_file(src: Path, dest_stem: Path, options: ConvertOptions,
                 detection: Detection | None = None) -> list[ConvertResult]:
    """Convert one asset. Returns a list because archives expand to many."""
    src = Path(src)
    try:
        detection = detection or detect(src)
    except OSError as exc:
        return [ConvertResult(src, status="failed", reason=f"unreadable: {exc}")]

    if detection.kind is Kind.ZIP:
        return _convert_archive(src, dest_stem, options)

    result = ConvertResult(src, kind=detection.kind.value, notes=list(detection.notes))
    try:
        with tempfile.TemporaryDirectory(prefix="giftkit-") as tmp:
            work = Path(tmp)
            source = _decode(src, detection, work, options, result)
            if source is None:
                return [result]
            if isinstance(source, ConvertResult):
                return [source]

            dest = dest_stem.with_suffix(options.encode_options().suffix())
            if options.target == "png-seq":
                dest = dest_stem
            if dest.exists() and not options.overwrite:
                dest = unique_path(dest)
            ensure_dir(dest.parent)
            encode(source, dest, options.encode_options())

            result.output = dest
            result.width, result.height = source.width, source.height
            result.fps, result.frames = round(source.fps, 3), source.frame_count
            result.has_alpha = source.has_alpha
            result.notes.extend(source.notes)
            result.status = "converted"
            _fill_from_output(result, dest)
    except pag.RendererUnavailable as exc:
        result.status, result.reason = "skipped", str(exc)
    except (ff.FFmpegError, ff.FFmpegMissing) as exc:
        result.status, result.reason = "failed", str(exc)
    except Exception as exc:  # noqa: BLE001 - one bad asset must not kill the run
        LOG.debug("convert %s failed", src, exc_info=True)
        result.status, result.reason = "failed", f"{type(exc).__name__}: {exc}"
    return [result]


def _decode(src: Path, detection: Detection, work: Path, options: ConvertOptions,
            result: ConvertResult):
    """Return a FrameSource, ``None`` (result already populated), or a ConvertResult."""
    kind = detection.kind

    if kind in STILL_KINDS:
        if not options.keep_stills:
            result.status, result.reason = "skipped", "still image"
            return None
        return _save_still(src, options, result)

    if kind in {Kind.ANIMATED_WEBP, Kind.GIF, Kind.APNG}:
        return imgseq.decode(src, work / "frames", max_frames=options.max_frames,
                             fps_override=options.fps)

    if kind is Kind.SVGA:
        return svga.decode(src, work / "frames", max_frames=options.max_frames)

    if kind is Kind.PAG:
        return pag.decode(src, work / "frames", command=options.pag_cmd,
                          fps=options.fps or 30.0, max_frames=options.max_frames)

    if kind is Kind.VAP_MP4:
        return vap.decode(src, detection.vap)

    if kind is Kind.MP4:
        geometry = _split_geometry(src, options)
        if geometry:
            result.kind = Kind.ALPHA_SPLIT_MP4.value
            return vap.decode(src, geometry)
        if options.skip_opaque:
            result.status, result.reason = "skipped", "opaque video (--skip-opaque)"
            return None
        return plainvideo.decode(src)

    if kind is Kind.LOTTIE:
        result.status = "skipped"
        result.reason = ("Lottie/bodymovin JSON: render with lottie-web or "
                         "`npx @lottiefiles/lottie-cli` and re-run on the output")
        return None

    result.status, result.reason = "skipped", detection.notes[0] if detection.notes else \
        f"unsupported format: {kind.value}"
    return None


def _split_geometry(src: Path, options: ConvertOptions) -> dict | None:
    """Work out the alpha rectangles for an mp4 with no vapc box."""
    if options.force_layout:
        info = ff.probe(src)
        if not info.width or not info.height:
            return None
        return _geometry_for_layout(options.force_layout, info.width, info.height, info.fps,
                                    info.frames)
    try:
        return sniff_alpha_layout(src)
    except Exception as exc:  # noqa: BLE001 - heuristic only
        LOG.debug("alpha sniff failed for %s: %s", src.name, exc)
        return None


def _geometry_for_layout(layout: str, width: int, height: int, fps: float,
                         frames: int) -> dict:
    vertical = layout in {"top-bottom", "bottom-top"}
    half_w = width // 2 if not vertical else width
    half_h = height // 2 if vertical else height
    second = (0, half_h) if vertical else (half_w, 0)
    rgb_first = layout in {"left-right", "top-bottom"}
    rgb_xy = (0, 0) if rgb_first else second
    alpha_xy = second if rgb_first else (0, 0)
    return {
        "rgb_rect": [rgb_xy[0], rgb_xy[1], half_w, half_h],
        "alpha_rect": [alpha_xy[0], alpha_xy[1], half_w, half_h],
        "width": half_w, "height": half_h,
        "video_width": width, "video_height": height,
        "fps": fps or None, "frames": frames,
        "source": "forced", "layout": layout,
    }


def _save_still(src: Path, options: ConvertOptions, result: ConvertResult):
    from PIL import Image

    dest = options.out_dir / "stills" / (src.stem + ".png")
    ensure_dir(dest.parent)
    with Image.open(src) as img:
        rgba = img.convert("RGBA")
        rgba.save(dest, "PNG")
        result.width, result.height = rgba.size
    result.output = dest
    result.status = "still"
    result.has_alpha = True
    result.frames = 1
    return None


def _convert_archive(src: Path, dest_stem: Path, options: ConvertOptions) -> list[ConvertResult]:
    """Effect bundles ship as zips; unpack and convert whatever is inside."""
    results: list[ConvertResult] = []
    with tempfile.TemporaryDirectory(prefix="giftkit-zip-") as tmp:
        root = Path(tmp)
        try:
            with zipfile.ZipFile(src) as archive:
                for member in archive.namelist():
                    # Refuse absolute paths and ../ traversal from untrusted zips.
                    target = (root / member).resolve()
                    if not str(target).startswith(str(root.resolve())):
                        LOG.warning("skipping unsafe zip entry %s in %s", member, src.name)
                        continue
                    archive.extract(member, root)
        except zipfile.BadZipFile as exc:
            return [ConvertResult(src, status="failed", kind=Kind.ZIP.value, reason=str(exc))]

        members = [p for p in sorted(root.rglob("*")) if p.is_file()]
        LOG.debug("%s: %d entries in archive", src.name, len(members))
        for index, member in enumerate(members):
            stem = dest_stem if len(members) == 1 else dest_stem.with_name(
                f"{dest_stem.name}-{index:02d}-{member.stem}"
            )
            for result in convert_file(member, stem, options):
                result.source = src
                result.notes.append(f"from archive entry {member.name}")
                results.append(result)
    if not results:
        results.append(ConvertResult(src, status="skipped", kind=Kind.ZIP.value,
                                     reason="archive contained no convertible assets"))
    return results


def convert_manifest(manifest, raw_root: Path, options: ConvertOptions,
                     on_progress=None) -> dict[str, list[ConvertResult]]:
    """Convert every downloaded asset in a manifest.

    Identical files are converted once and shared: the gift panel reuses the
    same artwork across dozens of entries, and on a full catalogue that removes
    a third of the work.
    """
    options.target = preferred_target(options.target)
    raw_root = Path(raw_root)

    jobs: list[tuple[str, Path, Path]] = []      # (asset.local, src, dest_stem)
    results: dict[str, list[ConvertResult]] = {}
    for gift, asset in manifest.all_assets():
        if not asset.local:
            continue
        src = raw_root / asset.local
        if not src.is_file():
            results[asset.local] = [ConvertResult(src, status="failed",
                                                  reason="asset was never downloaded")]
            continue
        jobs.append((asset.local, src,
                     _dest_stem(options.out_dir, options.target, gift, asset.role)))

    # Group by content so duplicates ride along with the first conversion.
    by_digest: dict[str, list[tuple[str, Path, Path]]] = {}
    for local, src, stem in jobs:
        try:
            digest = sha256_file(src)
        except OSError as exc:
            results[local] = [ConvertResult(src, status="failed", reason=str(exc))]
            continue
        by_digest.setdefault(digest, []).append((local, src, stem))

    total = len(by_digest)
    LOG.info("converting %d unique assets (%d references)", total, len(jobs))

    from concurrent.futures import ThreadPoolExecutor

    def _work(item):
        digest, group = item
        local, src, stem = group[0]
        converted = convert_file(src, stem, options)
        for result in converted:
            result.sha256 = digest
        return digest, group, converted

    with ThreadPoolExecutor(max_workers=max(1, options.workers)) as pool:
        for index, (digest, group, converted) in enumerate(
                pool.map(_work, by_digest.items()), start=1):
            primary_local = group[0][0]
            results[primary_local] = converted
            for local, src, _stem in group[1:]:
                results[local] = [
                    ConvertResult(
                        src, status="duplicate" if result.ok else result.status,
                        kind=result.kind, output=result.output, width=result.width,
                        height=result.height, fps=result.fps, frames=result.frames,
                        has_alpha=result.has_alpha, sha256=digest,
                        reason=result.reason or "identical content",
                        notes=list(result.notes),
                    )
                    for result in converted
                ]
            if on_progress:
                on_progress(index, total, converted)
    return results


def _fill_from_output(result: ConvertResult, dest: Path) -> None:
    """Backfill geometry the decoder could not know.

    Container metadata for mp4 rarely includes a frame count, so a sniffed
    split-alpha video arrives here with ``frames == 0``. The encoded output
    does have a duration, which combined with the frame rate gives an honest
    number for the library index.
    """
    if not dest.is_file():
        return
    info = ff.probe(dest)
    if not result.width or not result.height:
        result.width, result.height = info.width, info.height
    if not result.fps and info.fps:
        result.fps = round(info.fps, 3)
    if not result.frames:
        if info.frames:
            result.frames = info.frames
        elif info.duration and result.fps:
            result.frames = max(1, round(info.duration * result.fps))


def _dest_stem(out_dir: Path, target: str, gift, role: str) -> Path:
    """Readable output name: ``<slug>-<id>-<role>``, minus redundant parts.

    Local dumps have no gift id or role worth repeating, so a file called
    ``lion.svga`` becomes ``lion.webm`` rather than ``lion-lion-file.webm``.
    """
    parts = [gift.slug]
    if gift.id and gift.id != gift.slug:
        parts.append(str(gift.id))
    if role and role != "file":
        parts.append(role)
    return out_dir / _target_subdir(target) / "-".join(parts)


def _target_subdir(target: str) -> str:
    return {"webm": "webm", "webm-vp8": "webm", "mov": "mov",
            "apng": "apng", "gif": "gif", "png-seq": "frames"}.get(target, target)


def copy_into(src: Path, dest: Path) -> Path:
    ensure_dir(dest.parent)
    shutil.copy2(src, dest)
    return dest
