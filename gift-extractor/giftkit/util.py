"""Small shared helpers: logging, hashing, filesystem hygiene."""
from __future__ import annotations

import hashlib
import logging
import os
import re
import unicodedata
from pathlib import Path
from typing import Iterable

LOG = logging.getLogger("giftkit")

_SLUG_STRIP = re.compile(r"[^a-zA-Z0-9._-]+")


def setup_logging(verbose: bool = False) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )
    # ``urllib3``/``requests`` are noisy at DEBUG and drown out our own output.
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def slugify(value: str, fallback: str = "gift") -> str:
    """Filesystem-safe, stable name. Keeps ASCII letters/digits/._- only.

    Gift names routinely contain emoji and CJK text, which we transliterate away
    rather than escape so the resulting filenames stay usable from OBS, shell
    scripts and URLs.
    """
    normalised = unicodedata.normalize("NFKD", value)
    ascii_only = normalised.encode("ascii", "ignore").decode("ascii")
    slug = _SLUG_STRIP.sub("-", ascii_only).strip("-._")
    slug = re.sub(r"-{2,}", "-", slug)
    return slug.lower() or fallback


def sha256_file(path: os.PathLike | str, chunk: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(chunk), b""):
            digest.update(block)
    return digest.hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def ensure_dir(path: os.PathLike | str) -> Path:
    directory = Path(path)
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def human_size(num_bytes: int) -> str:
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f}{unit}" if unit == "B" else f"{size:.1f}{unit}"
        size /= 1024
    return f"{size:.1f}GB"


def iter_files(root: os.PathLike | str, skip_dirs: Iterable[str] = ()) -> Iterable[Path]:
    skip = set(skip_dirs)
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in skip]
        for name in filenames:
            yield Path(dirpath) / name


def unique_path(path: Path) -> Path:
    """Return ``path`` or ``name-2.ext``/``name-3.ext`` if it is already taken."""
    if not path.exists():
        return path
    stem, suffix, parent = path.stem, path.suffix, path.parent
    counter = 2
    while True:
        candidate = parent / f"{stem}-{counter}{suffix}"
        if not candidate.exists():
            return candidate
        counter += 1
