"""TikTok LIVE webcast source - the emulator-free path.

TikTok's web LIVE player asks ``webcast.tiktok.com`` for the gift panel over
plain HTTPS, and the response carries CDN URLs for every gift's artwork
(animated WebP icons, preview images, and for some gifts the full-screen effect
bundles). That is the same data an instrumented app would cache on disk, so
there is no reason to drive an emulator to get at it.

Schema note: the webcast payload is large and its shape drifts between
releases, so rather than hard-coding paths we walk the JSON and pick out
anything that looks like a gift or like a CDN URL list. That survives the
renames that break most scrapers.
"""
from __future__ import annotations

import json
import re
from typing import Any, Iterable, Iterator
from urllib.parse import urlparse

from ..manifest import AssetRef, Gift, Manifest
from ..net import HttpError, Session
from ..util import LOG
from . import BaseSource

GIFT_LIST_URL = "https://webcast.tiktok.com/webcast/gift/list/"
ASSET_LIST_URL = "https://webcast.tiktok.com/webcast/asset/list/"
LIVE_PAGE_URL = "https://www.tiktok.com/@{username}/live"

#: Parameters the TikTok web client sends on every webcast call.
BASE_PARAMS: dict[str, str] = {
    "aid": "1988",
    "app_language": "en-US",
    "app_name": "tiktok_web",
    "browser_language": "en-US",
    "browser_name": "Mozilla",
    "browser_online": "true",
    "browser_platform": "Win32",
    "browser_version": "5.0 (Windows)",
    "cookie_enabled": "true",
    "device_platform": "web",
    "focus_state": "true",
    "from_page": "user",
    "history_len": "4",
    "is_fullscreen": "false",
    "is_page_visible": "true",
    "did_rule": "3",
    "fetch_rule": "1",
    "identity": "audience",
    "last_rtt": "0",
    "live_id": "12",
    "resp_content_type": "json",
    "screen_height": "1080",
    "screen_width": "1920",
    "tz_name": "America/New_York",
    "referer": "https://www.tiktok.com/",
    "root_referer": "https://www.tiktok.com/",
    "version_code": "270000",
    "webcast_sdk_version": "1.3.0",
    "update_version_code": "1.3.0",
}

_ROOM_ID_PATTERNS = (
    re.compile(r'"roomId"\s*:\s*"(\d{6,})"'),
    re.compile(r'"room_id"\s*:\s*"?(\d{6,})"?'),
    re.compile(r'room_id=(\d{6,})'),
)

#: Keys whose ``url_list`` we care about, mapped to a stable role name.
ROLE_ALIASES = {
    "icon": "icon",
    "image": "image",
    "preview_image": "preview",
    "gift_panel_banner": "banner",
    "gift_label_icon": "label",
    "dynamic_img": "image",
    "resource": "effect",
    "effect": "effect",
    "animation": "effect",
}

ASSET_EXTENSIONS = (
    ".webp", ".png", ".gif", ".apng", ".jpeg", ".jpg",
    ".mp4", ".mov", ".webm", ".pag", ".svga", ".zip",
)


class WebcastSource(BaseSource):
    name = "webcast"

    def __init__(
        self,
        *,
        session: Session | None = None,
        room_id: str | None = None,
        username: str | None = None,
        roles: Iterable[str] | None = None,
        extra_params: dict[str, str] | None = None,
        with_effects: bool = False,
    ):
        self.session = session or Session()
        self.room_id = room_id
        self.username = username
        self.roles = set(roles) if roles else None
        self.extra_params = dict(extra_params or {})
        self.with_effects = with_effects

    # -- room resolution ---------------------------------------------------
    def resolve_room_id(self) -> str | None:
        """Find a live room id, which the gift panel is keyed against.

        Any live room will do - the gift catalogue is regional, not
        per-streamer - so pointing this at any account that is currently live
        is enough.
        """
        if self.room_id:
            return self.room_id
        if not self.username:
            return None
        url = LIVE_PAGE_URL.format(username=self.username.lstrip("@"))
        LOG.info("resolving room id from %s", url)
        html = self.session.get_text(url)
        for pattern in _ROOM_ID_PATTERNS:
            match = pattern.search(html)
            if match:
                self.room_id = match.group(1)
                LOG.info("room id %s (@%s)", self.room_id, self.username)
                return self.room_id
        if "user-not-live" in html or "LiveRoomOffline" in html:
            raise HttpError(f"@{self.username} is not live right now; pick a live account")
        raise HttpError(f"could not find a room id on {url}")

    # -- fetching ----------------------------------------------------------
    def params(self) -> dict[str, str]:
        params = dict(BASE_PARAMS)
        if self.room_id:
            params["room_id"] = self.room_id
        params.update(self.extra_params)
        return params

    def fetch_gift_payload(self) -> dict[str, Any]:
        self.resolve_room_id()
        payload = self.session.get_json(GIFT_LIST_URL, self.params())
        if not isinstance(payload, dict):
            raise HttpError("unexpected gift list payload (not an object)")
        status = payload.get("status_code", payload.get("statusCode", 0))
        if status not in (0, None):
            message = payload.get("data", {}).get("message") or payload.get("message")
            raise HttpError(f"webcast returned status_code={status} ({message})")
        return payload

    def fetch(self) -> Manifest:
        payload = self.fetch_gift_payload()
        gifts = parse_gifts(payload, roles=self.roles)
        LOG.info("webcast returned %d gifts", len(gifts))
        if self.with_effects:
            self._attach_effect_assets(gifts)
        return Manifest(
            source="webcast",
            room_id=self.room_id,
            context={"username": self.username, "with_effects": self.with_effects},
            gifts=gifts,
        )

    def _attach_effect_assets(self, gifts: list[Gift]) -> None:
        """Best-effort: ask the asset endpoint for full-screen effect bundles.

        Availability varies by region and app version. Failures are logged and
        skipped rather than aborting the run - the artwork from the gift list
        is still perfectly usable on its own.
        """
        effect_ids = [g.primary_effect_id for g in gifts if g.primary_effect_id]
        if not effect_ids:
            LOG.info("no primary_effect_id values in this payload; skipping effect lookup")
            return
        by_effect: dict[str, Gift] = {
            g.primary_effect_id: g for g in gifts if g.primary_effect_id
        }
        for batch in _chunks(effect_ids, 20):
            params = self.params()
            params["asset_ids"] = json.dumps(batch, separators=(",", ":"))
            try:
                payload = self.session.get_json(ASSET_LIST_URL, params)
            except HttpError as exc:
                LOG.warning("effect lookup failed for %d ids: %s", len(batch), exc)
                continue
            for asset in _walk_dicts(payload):
                asset_id = str(asset.get("id") or asset.get("asset_id") or "")
                gift = by_effect.get(asset_id)
                if gift is None:
                    continue
                for ref in _asset_refs(asset, roles=None):
                    ref.role = "effect"
                    gift.assets.append(ref)


# -- payload parsing -------------------------------------------------------

def parse_gifts(payload: Any, roles: Iterable[str] | None = None) -> list[Gift]:
    """Pull every gift-shaped object out of an arbitrary webcast payload."""
    roles = set(roles) if roles else None
    gifts: dict[str, Gift] = {}
    for node in _walk_dicts(payload):
        if not _is_gift(node):
            continue
        gift_id = str(node.get("id"))
        if gift_id in gifts:
            continue
        gift = Gift(
            id=gift_id,
            name=str(node.get("name") or "").strip(),
            diamond_count=_as_int(node.get("diamond_count")),
            describe=node.get("describe") or None,
            type=_as_int(node.get("type")),
            duration_ms=_as_int(node.get("duration")),
            is_box_gift=bool(node.get("is_box_gift")),
            primary_effect_id=_as_str(node.get("primary_effect_id")),
            assets=_asset_refs(node, roles),
        )
        if gift.assets:
            gifts[gift_id] = gift
        else:
            LOG.debug("gift %s (%s) has no downloadable assets", gift_id, gift.name)
    return list(gifts.values())


def _is_gift(node: dict[str, Any]) -> bool:
    if "id" not in node:
        return False
    if not isinstance(node.get("id"), (str, int)):
        return False
    has_name = bool(node.get("name") or node.get("describe"))
    has_gift_marker = any(
        key in node for key in ("diamond_count", "gift_panel_banner", "primary_effect_id")
    ) or ("icon" in node and "image" in node)
    return has_name and has_gift_marker


def _asset_refs(node: dict[str, Any], roles: set[str] | None) -> list[AssetRef]:
    refs: list[AssetRef] = []
    seen: set[str] = set()
    for key, urls in _walk_url_lists(node):
        role = ROLE_ALIASES.get(key, key if key else "file")
        if roles and role not in roles:
            continue
        usable = [u for u in urls if _is_asset_url(u)]
        if not usable:
            continue
        primary = usable[0]
        fingerprint = _url_fingerprint(primary)
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        refs.append(AssetRef(role=role, urls=usable))
    return refs


def _walk_dicts(node: Any) -> Iterator[dict[str, Any]]:
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk_dicts(value)
    elif isinstance(node, list):
        for item in node:
            yield from _walk_dicts(item)


def _walk_url_lists(node: Any, key: str = "") -> Iterator[tuple[str, list[str]]]:
    """Yield ``(owning_key, urls)`` for every ``url_list``-style field."""
    if isinstance(node, dict):
        urls = node.get("url_list") or node.get("urlList") or node.get("url_lists")
        if isinstance(urls, list) and all(isinstance(u, str) for u in urls) and urls:
            yield key, urls
        for child_key, value in node.items():
            if child_key in {"url_list", "urlList", "url_lists"}:
                continue
            yield from _walk_url_lists(value, child_key)
    elif isinstance(node, list):
        for item in node:
            yield from _walk_url_lists(item, key)


def _is_asset_url(url: str) -> bool:
    if not url.startswith(("http://", "https://")):
        return False
    path = urlparse(url).path.lower()
    # TikTok CDN paths often end in `~tplv-...` transforms rather than a plain
    # extension, so accept either a known extension or an image CDN host.
    if path.endswith(ASSET_EXTENSIONS) or any(ext in path for ext in ASSET_EXTENSIONS):
        return True
    host = urlparse(url).netloc.lower()
    return any(marker in host for marker in ("tiktokcdn", "byteimg", "ibyteimg", "muscdn"))


def _url_fingerprint(url: str) -> str:
    """Identity of an asset ignoring CDN mirror host and query string."""
    parsed = urlparse(url)
    return parsed.path.rsplit("/", 1)[-1] or parsed.path


def _chunks(items: list[str], size: int) -> Iterator[list[str]]:
    for start in range(0, len(items), size):
        yield items[start:start + size]


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_str(value: Any) -> str | None:
    if value in (None, "", 0, "0"):
        return None
    return str(value)
