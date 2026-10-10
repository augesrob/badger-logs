"""Data model shared by the fetch and convert stages."""
from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable

MANIFEST_VERSION = 1


@dataclass
class AssetRef:
    """One downloadable file belonging to a gift.

    ``urls`` keeps every mirror TikTok advertised for the asset; the downloader
    walks the list until one responds.
    """

    role: str = "unknown"          # icon | image | preview | effect | bundle | file
    urls: list[str] = field(default_factory=list)
    local: str | None = None       # path relative to the raw directory
    sha256: str | None = None
    bytes: int | None = None
    kind: str | None = None        # filled in by the convert stage (see detect.Kind)
    error: str | None = None

    @property
    def url(self) -> str | None:
        return self.urls[0] if self.urls else None


@dataclass
class Gift:
    id: str
    name: str = ""
    diamond_count: int | None = None
    describe: str | None = None
    type: int | None = None
    duration_ms: int | None = None
    is_box_gift: bool = False
    primary_effect_id: str | None = None
    assets: list[AssetRef] = field(default_factory=list)

    @property
    def slug(self) -> str:
        from .util import slugify

        return slugify(self.name or f"gift-{self.id}", fallback=f"gift-{self.id}")


@dataclass
class Manifest:
    source: str = ""
    created_at: float = field(default_factory=time.time)
    room_id: str | None = None
    context: dict[str, Any] = field(default_factory=dict)
    gifts: list[Gift] = field(default_factory=list)
    version: int = MANIFEST_VERSION

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    def save(self, path: Path | str) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_json(), indent=2, ensure_ascii=False), "utf-8")
        return path

    @classmethod
    def load(cls, path: Path | str) -> "Manifest":
        payload = json.loads(Path(path).read_text("utf-8"))
        return cls.from_json(payload)

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> "Manifest":
        gifts = []
        for raw_gift in payload.get("gifts", []):
            assets = [AssetRef(**a) for a in raw_gift.get("assets", [])]
            fields = {k: v for k, v in raw_gift.items() if k != "assets"}
            fields.setdefault("id", "")
            gifts.append(Gift(assets=assets, **fields))
        return cls(
            source=payload.get("source", ""),
            created_at=payload.get("created_at", time.time()),
            room_id=payload.get("room_id"),
            context=payload.get("context", {}),
            gifts=gifts,
            version=payload.get("version", MANIFEST_VERSION),
        )

    def all_assets(self) -> Iterable[tuple[Gift, AssetRef]]:
        for gift in self.gifts:
            for asset in gift.assets:
                yield gift, asset
