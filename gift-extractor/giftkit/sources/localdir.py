"""Adopt an existing directory of dumped assets (ADB pull, zip, manual export).

Filenames in app caches are usually md5 hashes with no extension, so the
convert stage sniffs each file's bytes; this source only has to enumerate them.
"""
from __future__ import annotations

from pathlib import Path

from ..manifest import AssetRef, Gift, Manifest
from ..util import iter_files, slugify
from . import BaseSource

SKIP_NAMES = {".DS_Store", "Thumbs.db"}
SKIP_DIRS = {".git", "__MACOSX"}


class LocalDirSource(BaseSource):
    name = "dir"

    def __init__(self, *, path: Path | str, min_bytes: int = 128, **_ignored):
        self.path = Path(path)
        self.min_bytes = min_bytes

    def fetch(self) -> Manifest:
        if not self.path.is_dir():
            raise NotADirectoryError(f"{self.path} is not a directory")
        gifts: list[Gift] = []
        for file_path in sorted(iter_files(self.path, skip_dirs=SKIP_DIRS)):
            if file_path.name in SKIP_NAMES or file_path.name.endswith(".part"):
                continue
            try:
                size = file_path.stat().st_size
            except OSError:
                continue
            if size < self.min_bytes:
                continue
            relative = file_path.relative_to(self.path)
            stem = relative.as_posix().rsplit("/", 1)[-1]
            gift_id = slugify(relative.as_posix().rsplit(".", 1)[0], fallback=stem)
            gifts.append(
                Gift(
                    id=gift_id,
                    name=Path(stem).stem,
                    assets=[AssetRef(role="file", local=relative.as_posix(), bytes=size)],
                )
            )
        return Manifest(
            source=f"dir:{self.path}",
            context={"path": str(self.path.resolve())},
            gifts=gifts,
        )
