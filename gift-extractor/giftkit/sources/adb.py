"""Optional ADB source, for a physical device or emulator you already trust.

Not needed for the default workflow - the webcast source gets the same artwork
over HTTPS. Kept because pulling the app's own cache is the only way to see
assets that never appear in the public gift panel, and because a rooted test
device is sometimes simply the fastest route.

Requires ``adb`` on PATH and a device where the app data directory is readable
(``run-as`` on a debuggable build, or root). Without that, ``adb pull`` of
``/data/data/...`` fails with "Permission denied" - that is a device
restriction, not a bug here.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from ..manifest import Manifest
from ..util import LOG, ensure_dir
from . import BaseSource
from .localdir import LocalDirSource

#: Known cache roots, newest package name first.
DEFAULT_REMOTE_PATHS = (
    "/data/data/com.zhiliaoapp.musically/files/live/",
    "/data/data/com.ss.android.ugc.trill/files/live/",
    "/sdcard/Android/data/com.zhiliaoapp.musically/cache/",
)


class AdbSource(BaseSource):
    name = "adb"

    def __init__(self, *, path: Path | str, serial: str | None = None,
                 remote_paths=DEFAULT_REMOTE_PATHS, **_ignored):
        self.dest = ensure_dir(path)
        self.serial = serial
        self.remote_paths = tuple(remote_paths)

    def _adb(self, *args: str) -> subprocess.CompletedProcess:
        if not shutil.which("adb"):
            raise RuntimeError("adb not found on PATH; install platform-tools")
        cmd = ["adb"] + (["-s", self.serial] if self.serial else []) + list(args)
        LOG.debug("adb: %s", " ".join(cmd))
        return subprocess.run(cmd, capture_output=True, text=True)

    def fetch(self) -> Manifest:
        pulled = 0
        for remote in self.remote_paths:
            target = self.dest / remote.strip("/").replace("/", "_")
            ensure_dir(target)
            proc = self._adb("pull", remote, str(target))
            if proc.returncode == 0:
                pulled += 1
                LOG.info("pulled %s", remote)
            else:
                LOG.warning("adb pull %s failed: %s", remote, (proc.stderr or "").strip())
        if not pulled:
            raise RuntimeError(
                "no cache directory could be pulled. The app's private data is only "
                "readable on a rooted or debuggable device; use the webcast source instead."
            )
        return LocalDirSource(path=self.dest).fetch()
