"""Command line interface.

    giftkit fetch    # gift catalogue + raw assets  (no device, no emulator)
    giftkit convert  # raw assets -> transparent WebM + library.json
    giftkit build    # fetch, then convert
    giftkit serve    # overlay server for an OBS browser source
    giftkit inspect  # what is this file, really?
    giftkit doctor   # is this machine set up to convert everything?
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from urllib.parse import urlparse

from . import ffmpeg as ff
from . import library as library_mod
from .convert import ConvertOptions, convert_manifest
from .detect import Kind, detect, sniff_alpha_layout
from .encode import TARGETS
from .manifest import Manifest
from .net import Session, download_many
from .sources import get_source
from .util import LOG, ensure_dir, setup_logging, slugify

DEFAULT_OUT = Path("gifts")
LAYOUTS = ("left-right", "right-left", "top-bottom", "bottom-top")


# -- shared helpers --------------------------------------------------------

def raw_root_for(manifest: Manifest, out_dir: Path) -> Path:
    """Where this manifest's raw files live (in-place for local dumps)."""
    configured = manifest.context.get("raw_root")
    if configured:
        return Path(configured)
    if manifest.source.startswith("dir:"):
        return Path(manifest.context["path"])
    return out_dir / "raw"


def local_path_for(gift, asset, index: int) -> str:
    """Stable, readable relative path for a downloaded asset."""
    name = Path(urlparse(asset.url or "").path).name or f"{index}"
    name = slugify(name, fallback=f"asset-{index}")
    if "." not in name[-6:]:
        name = f"{name}.bin"   # extensionless CDN blob; detection is by content
    return f"{gift.slug}-{gift.id}/{asset.role}-{name}"


# -- commands --------------------------------------------------------------

def cmd_fetch(args: argparse.Namespace) -> int:
    out_dir = ensure_dir(args.out)
    if args.source in {"dir", "manifest", "adb"} and not args.path:
        LOG.error("--source %s needs --path", args.source)
        return 1
    if args.source == "webcast" and not (args.user or args.room_id):
        LOG.error("pass --user <account that is live> or --room-id <id>")
        return 1
    source_cls = get_source(args.source)
    kwargs = dict(
        room_id=args.room_id,
        username=args.user,
        roles=args.roles.split(",") if args.roles else None,
        path=args.path,
        with_effects=args.with_effects,
        serial=args.serial,
    )
    if args.source == "webcast":
        kwargs["session"] = Session(timeout=args.timeout)
        kwargs["extra_params"] = dict(
            pair.split("=", 1) for pair in (args.param or [])
        )
    source = source_cls(**{k: v for k, v in kwargs.items() if v is not None})
    manifest = source.fetch()

    if args.limit:
        manifest.gifts = manifest.gifts[: args.limit]
    if not manifest.gifts:
        LOG.error("no gifts found - nothing to download")
        return 1

    if manifest.source.startswith(("dir:", "adb")):
        LOG.info("%d local assets catalogued", sum(len(g.assets) for g in manifest.gifts))
    else:
        _download_manifest(manifest, out_dir, args)

    manifest.context["raw_root"] = str(raw_root_for(manifest, out_dir))
    path = manifest.save(out_dir / "manifest.json")
    total = sum(len(g.assets) for g in manifest.gifts)
    LOG.info("%d gifts / %d assets -> %s", len(manifest.gifts), total, path)
    return 0


def _download_manifest(manifest: Manifest, out_dir: Path, args: argparse.Namespace) -> None:
    raw_dir = ensure_dir(out_dir / "raw")
    session = Session(timeout=args.timeout)
    jobs = []
    for gift, asset in manifest.all_assets():
        if not asset.urls:
            continue
        asset.local = local_path_for(gift, asset, len(jobs))
        jobs.append((asset.urls, raw_dir / asset.local))

    done = {"n": 0}

    def progress(dest: Path, error: Exception | None) -> None:
        done["n"] += 1
        if error:
            LOG.warning("download failed: %s (%s)", dest.name, error)
        elif done["n"] % 25 == 0 or done["n"] == len(jobs):
            LOG.info("downloaded %d/%d", done["n"], len(jobs))

    LOG.info("downloading %d assets to %s", len(jobs), raw_dir)
    outcomes = download_many(session, jobs, workers=args.workers,
                             overwrite=args.overwrite, on_done=progress)

    for _gift, asset in manifest.all_assets():
        if not asset.local:
            continue
        path = raw_dir / asset.local
        error = outcomes.get(path)
        if error or not path.is_file():
            asset.error = str(error) if error else "missing after download"
            continue
        asset.bytes = path.stat().st_size
        from .util import sha256_file

        asset.sha256 = sha256_file(path)


def cmd_convert(args: argparse.Namespace) -> int:
    out_dir = ensure_dir(args.out)
    manifest_path = args.manifest or (out_dir / "manifest.json")
    if not Path(manifest_path).is_file():
        LOG.error("no manifest at %s - run `giftkit fetch` first", manifest_path)
        return 1
    manifest = Manifest.load(manifest_path)
    raw_root = Path(args.raw) if args.raw else raw_root_for(manifest, out_dir)
    if not raw_root.is_dir():
        LOG.error("raw asset directory %s does not exist", raw_root)
        return 1

    options = ConvertOptions(
        out_dir=out_dir,
        target=args.target,
        crf=args.crf,
        fps=args.fps,
        max_width=args.max_width,
        max_frames=args.max_frames,
        overwrite=args.overwrite,
        keep_stills=not args.no_stills,
        skip_opaque=args.skip_opaque,
        force_layout=args.layout,
        pag_cmd=args.pag_cmd,
        workers=args.workers,
    )

    def progress(index: int, total: int, results) -> None:
        if index % 10 == 0 or index == total:
            LOG.info("converted %d/%d", index, total)

    results = convert_manifest(manifest, raw_root, options, on_progress=progress)
    flat = [r for group in results.values() for r in group]
    library = library_mod.build(manifest, results, out_dir, options.target)
    library_path = library_mod.save(library, out_dir / "library.json")

    counts = library_mod.summarise(flat)
    LOG.info("done: %s", ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    LOG.info("%d playable assets -> %s", library["counts"]["assets"], library_path)
    for entry in library["skipped"][:5]:
        LOG.info("skipped %s (%s): %s", entry["name"] or entry["gift_id"],
                 entry["kind"], entry["reason"])
    if len(library["skipped"]) > 5:
        LOG.info("... and %d more (see library.json)", len(library["skipped"]) - 5)
    return 0 if counts.get("failed", 0) == 0 else 2


def cmd_build(args: argparse.Namespace) -> int:
    code = cmd_fetch(args)
    if code != 0:
        return code
    return cmd_convert(args)


def cmd_serve(args: argparse.Namespace) -> int:
    from .serve import serve

    out_dir = Path(args.out)
    library_path = out_dir / "library.json"
    if not library_path.is_file():
        LOG.error("no library.json in %s - run `giftkit convert` first", out_dir)
        return 1
    serve(out_dir, library_path, host=args.host, port=args.port)
    return 0


def cmd_inspect(args: argparse.Namespace) -> int:
    for target in args.files:
        path = Path(target)
        if not path.is_file():
            print(f"{path}: not a file")
            continue
        detection = detect(path)
        report = {
            "file": str(path),
            "bytes": path.stat().st_size,
            "kind": detection.kind.value,
            "notes": detection.notes,
        }
        if detection.vap:
            report["vap"] = detection.vap
        elif detection.kind is Kind.MP4:
            sniffed = sniff_alpha_layout(path)
            report["sniffed_alpha"] = sniffed or "none (looks like an ordinary video)"
        if detection.kind.is_animation and detection.kind in {Kind.MP4, Kind.VAP_MP4}:
            info = ff.probe(path)
            report["video"] = {
                "size": f"{info.width}x{info.height}", "fps": round(info.fps, 3),
                "codec": info.codec, "pix_fmt": info.pix_fmt,
                "duration": round(info.duration, 3),
            }
        if detection.kind is Kind.SVGA:
            from .formats.svga import parse

            movie = parse(path)
            report["svga"] = {
                "version": movie.version, "size": f"{movie.width}x{movie.height}",
                "fps": movie.fps, "frames": movie.frames,
                "sprites": len(movie.sprites), "images": len(movie.images),
                "vector_shapes": movie.has_vector_shapes,
            }
        print(json.dumps(report, indent=2))
    return 0


def cmd_doctor(_args: argparse.Namespace) -> int:
    from .formats.pag import NODE_RENDERER, node_renderer_ready

    checks: list[tuple[str, bool, str]] = []
    try:
        binary = ff.ffmpeg_bin()
        checks.append(("ffmpeg", True, binary))
        checks.append(("VP9 alpha (libvpx-vp9)", ff.has_encoder("libvpx-vp9"),
                       "required for the default WebM target"))
        checks.append(("VP8 alpha (libvpx)", ff.has_encoder("libvpx"), "fallback target"))
        checks.append(("ProRes 4444 (prores_ks)", ff.has_encoder("prores_ks"),
                       "optional, for --target mov"))
    except ff.FFmpegMissing as exc:
        checks.append(("ffmpeg", False, str(exc)))
    checks.append(("ffprobe", ff.ffprobe_bin() is not None,
                   "optional; ffmpeg -i is used when missing"))
    try:
        import PIL

        checks.append(("Pillow", True, f"{PIL.__version__} (WebP/GIF/APNG/SVGA rendering)"))
    except ImportError:
        checks.append(("Pillow", False, "pip install pillow"))
    import importlib.util

    checks.append((
        "requests",
        importlib.util.find_spec("requests") is not None,
        "optional; urllib is used when missing",
    ))
    checks.append(("PAG renderer", node_renderer_ready(),
                   f"optional; `npm install` in {NODE_RENDERER.parent}"))

    width = max(len(name) for name, _, _ in checks)
    failures = 0
    for name, ok, detail in checks:
        mark = "ok  " if ok else "MISS"
        if not ok and name in {"ffmpeg", "Pillow"}:
            failures += 1
        print(f"[{mark}] {name.ljust(width)}  {detail}")
    return 1 if failures else 0


def cmd_trigger(args: argparse.Namespace) -> int:
    """Convenience wrapper so you can test the overlay without curl."""
    import urllib.request

    url = f"http://{args.host}:{args.port}/trigger?gift={args.gift}"
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            print(response.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        LOG.error("trigger failed: %s", exc)
        return 1
    return 0


# -- argument parsing ------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="giftkit",
        description="Collect TikTok LIVE gift animations and convert them to "
                    "transparent video for OBS overlays.",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    subparsers = parser.add_subparsers(dest="command", required=True)

    def add_common(sub: argparse.ArgumentParser) -> None:
        sub.add_argument("--out", type=Path, default=DEFAULT_OUT,
                         help="output directory (default: ./gifts)")
        sub.add_argument("--workers", type=int, default=8)
        sub.add_argument("--overwrite", action="store_true")

    def add_fetch_args(sub: argparse.ArgumentParser) -> None:
        sub.add_argument("--source", choices=["webcast", "dir", "manifest", "adb"],
                         default="webcast")
        sub.add_argument("--user", help="a TikTok account that is live right now")
        sub.add_argument("--room-id", help="live room id, if you already have one")
        sub.add_argument("--path", type=Path,
                         help="directory or JSON file for --source dir/manifest/adb")
        sub.add_argument("--serial", help="adb device serial for --source adb")
        sub.add_argument("--roles", help="comma-separated asset roles to keep "
                                         "(icon,image,preview,banner,effect)")
        sub.add_argument("--with-effects", action="store_true",
                         help="also query the effect/asset endpoint (best effort)")
        sub.add_argument("--limit", type=int, help="only the first N gifts")
        sub.add_argument("--timeout", type=float, default=30.0)
        sub.add_argument("--param", action="append",
                         help="extra webcast query parameter, key=value (repeatable)")

    def add_convert_args(sub: argparse.ArgumentParser) -> None:
        sub.add_argument("--target", choices=list(TARGETS), default="webm")
        sub.add_argument("--crf", type=int, default=28, help="VP9 quality, lower is better")
        sub.add_argument("--fps", type=float, help="force an output frame rate")
        sub.add_argument("--max-width", type=int, help="downscale anything wider")
        sub.add_argument("--max-frames", type=int, help="cap frames per animation")
        sub.add_argument("--layout", choices=list(LAYOUTS),
                         help="force the split-alpha layout instead of sniffing it")
        sub.add_argument("--no-stills", action="store_true",
                         help="skip single-frame images")
        sub.add_argument("--skip-opaque", action="store_true",
                         help="skip videos with no recoverable alpha")
        sub.add_argument("--pag-cmd", help="external .pag renderer, e.g. "
                                           "'pag2png {input} {outdir}'")
        sub.add_argument("--manifest", type=Path)
        sub.add_argument("--raw", type=Path, help="raw asset directory override")

    fetch = subparsers.add_parser("fetch", help="download the gift catalogue")
    add_common(fetch)
    add_fetch_args(fetch)
    fetch.set_defaults(func=cmd_fetch)

    convert = subparsers.add_parser("convert", help="convert raw assets to alpha video")
    add_common(convert)
    add_convert_args(convert)
    convert.set_defaults(func=cmd_convert)

    build = subparsers.add_parser("build", help="fetch + convert in one go")
    add_common(build)
    add_fetch_args(build)
    add_convert_args(build)
    build.set_defaults(func=cmd_build)

    serve_cmd = subparsers.add_parser("serve", help="run the OBS overlay server")
    serve_cmd.add_argument("--out", type=Path, default=DEFAULT_OUT)
    serve_cmd.add_argument("--host", default="127.0.0.1")
    serve_cmd.add_argument("--port", type=int, default=8722)
    serve_cmd.set_defaults(func=cmd_serve)

    trigger = subparsers.add_parser("trigger", help="play a gift on a running overlay")
    trigger.add_argument("gift")
    trigger.add_argument("--host", default="127.0.0.1")
    trigger.add_argument("--port", type=int, default=8722)
    trigger.set_defaults(func=cmd_trigger)

    inspect = subparsers.add_parser("inspect", help="identify an asset file")
    inspect.add_argument("files", nargs="+")
    inspect.set_defaults(func=cmd_inspect)

    doctor = subparsers.add_parser("doctor", help="check this machine's toolchain")
    doctor.set_defaults(func=cmd_doctor)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    setup_logging(args.verbose)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        LOG.warning("interrupted")
        return 130
    except Exception as exc:  # noqa: BLE001 - CLI boundary
        if args.verbose:
            raise
        LOG.error("%s: %s", type(exc).__name__, exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
