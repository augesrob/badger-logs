"""Gift sources: where the asset list comes from.

Every source produces a :class:`giftkit.manifest.Manifest`. The default
(``webcast``) talks to TikTok's public LIVE web API and needs no device and no
emulator; ``localdir``/``adb`` exist for people who already have a dump.
"""
from __future__ import annotations

from typing import Callable

from ..manifest import Manifest

SourceFactory = Callable[..., "BaseSource"]


class BaseSource:
    name = "base"

    def fetch(self) -> Manifest:  # pragma: no cover - interface
        raise NotImplementedError


def get_source(name: str):
    from . import localdir, manifest_file, webcast

    registry = {
        "webcast": webcast.WebcastSource,
        "manifest": manifest_file.ManifestFileSource,
        "dir": localdir.LocalDirSource,
    }
    if name == "adb":
        from . import adb

        registry["adb"] = adb.AdbSource
    if name not in registry:
        raise KeyError(f"unknown source {name!r}; choose from {sorted(registry)}")
    return registry[name]
