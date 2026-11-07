"""Load gifts from a JSON file instead of the network.

Accepts three shapes, so you can feed it whatever you already have:

* a manifest previously written by ``giftkit fetch``;
* a raw ``/webcast/gift/list/`` response saved from devtools;
* a bare list of gift objects (e.g. ``TikTokLive``'s ``available_gifts``
  serialised to JSON).
"""
from __future__ import annotations

import json
from pathlib import Path

from ..manifest import Manifest
from . import BaseSource
from .webcast import parse_gifts


class ManifestFileSource(BaseSource):
    name = "manifest"

    def __init__(self, *, path: Path | str, roles=None, **_ignored):
        self.path = Path(path)
        self.roles = set(roles) if roles else None

    def fetch(self) -> Manifest:
        payload = json.loads(self.path.read_text("utf-8"))
        if isinstance(payload, dict) and "gifts" in payload and "version" in payload:
            return Manifest.from_json(payload)
        gifts = parse_gifts(payload, roles=self.roles)
        return Manifest(
            source=f"manifest:{self.path.name}",
            context={"path": str(self.path)},
            gifts=gifts,
        )
