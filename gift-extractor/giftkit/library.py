"""The output index: what was produced, for which gift, and how to play it."""
from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .convert import ConvertResult
from .manifest import Manifest
from .util import slugify

LIBRARY_VERSION = 1


@dataclass
class LibraryEntry:
    gift_id: str
    name: str
    slug: str
    role: str = "file"
    file: str = ""                 # path relative to the library root
    kind: str = ""
    diamond_count: int | None = None
    width: int = 0
    height: int = 0
    fps: float = 0.0
    frames: int = 0
    duration: float = 0.0
    has_alpha: bool = False
    bytes: int = 0
    sha256: str | None = None
    source_url: str | None = None
    notes: list[str] = field(default_factory=list)


def build(manifest: Manifest, results: dict[str, list[ConvertResult]], root: Path,
          target: str) -> dict[str, Any]:
    """Assemble ``library.json`` from a manifest and the convert results.

    ``results`` is keyed by the asset's local path (relative to the raw dir),
    which is how the convert stage tracks what came from where.
    """
    entries: list[LibraryEntry] = []
    skipped: list[dict[str, Any]] = []

    for gift, asset in manifest.all_assets():
        for result in results.get(asset.local or "", []):
            if not result.ok or result.output is None:
                if result.status in {"skipped", "failed"}:
                    skipped.append({
                        "gift_id": gift.id,
                        "name": gift.name,
                        "role": asset.role,
                        "kind": result.kind,
                        "status": result.status,
                        "reason": result.reason,
                    })
                continue
            try:
                relative = result.output.resolve().relative_to(root.resolve()).as_posix()
            except ValueError:
                relative = result.output.name
            size = result.output.stat().st_size if result.output.is_file() else 0
            duration = result.frames / result.fps if result.fps and result.frames else 0.0
            entries.append(
                LibraryEntry(
                    gift_id=gift.id,
                    name=gift.name,
                    slug=gift.slug,
                    role=asset.role,
                    file=relative,
                    kind=result.kind,
                    diamond_count=gift.diamond_count,
                    width=result.width,
                    height=result.height,
                    fps=result.fps,
                    frames=result.frames,
                    duration=round(duration, 3),
                    has_alpha=result.has_alpha,
                    bytes=size,
                    sha256=result.sha256,
                    source_url=asset.url,
                    notes=result.notes,
                )
            )

    return {
        "version": LIBRARY_VERSION,
        "created_at": time.time(),
        "source": manifest.source,
        "room_id": manifest.room_id,
        "target": target,
        "counts": {
            "gifts": len({e.gift_id for e in entries}),
            "assets": len(entries),
            "skipped": len(skipped),
        },
        "assets": [asdict(e) for e in entries],
        "skipped": skipped,
    }


def save(library: dict[str, Any], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(library, indent=2, ensure_ascii=False), "utf-8")
    return path


def load(path: Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text("utf-8"))


def lookup(library: dict[str, Any], query: str, *, prefer_animated: bool = True):
    """Resolve a gift id, exact name or slug to the best matching asset."""
    query_norm = str(query).strip().lower()
    slug = slugify(query_norm)
    candidates = [
        asset for asset in library.get("assets", [])
        if query_norm in (str(asset.get("gift_id", "")).lower(), asset.get("name", "").lower())
        or slug and asset.get("slug") == slug
    ]
    if not candidates:
        return None
    if prefer_animated:
        animated = [a for a in candidates if a.get("frames", 0) > 1]
        if animated:
            candidates = animated
    # Prefer the richest asset: alpha first, then pixel area.
    candidates.sort(key=lambda a: (a.get("has_alpha", False), a.get("width", 0) * a.get("height", 0)),
                    reverse=True)
    return candidates[0]


def summarise(results: Iterable[ConvertResult]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for result in results:
        counts[result.status] = counts.get(result.status, 0) + 1
    return counts
