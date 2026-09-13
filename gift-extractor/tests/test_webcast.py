"""Parsing of the webcast gift payload, without touching the network."""
import json

import pytest

from giftkit.net import HttpError
from giftkit.sources.webcast import (
    BASE_PARAMS, WebcastSource, _is_asset_url, parse_gifts,
)

PAYLOAD = {
    "status_code": 0,
    "data": {
        "gifts": [
            {
                "id": 5655,
                "name": "Rose",
                "describe": "Send a Rose",
                "diamond_count": 1,
                "type": 1,
                "duration": 1000,
                "primary_effect_id": 0,
                "icon": {"url_list": [
                    "https://p19-webcast.tiktokcdn.com/img/rose~tplv-obj.image",
                    "https://p16-webcast.tiktokcdn.com/img/rose~tplv-obj.image",
                ]},
                "image": {"url_list": [
                    "https://p19-webcast.tiktokcdn.com/webcast-va/rose.webp",
                ]},
                "preview_image": {"url_list": [
                    "https://p19-webcast.tiktokcdn.com/webcast-va/rose_preview.png",
                ]},
            },
            {
                "id": "5654",
                "name": "Lion",
                "diamond_count": 29999,
                "type": 2,
                "primary_effect_id": 883939,
                "icon": {"url_list": ["https://p19-webcast.tiktokcdn.com/img/lion.webp"]},
                "image": {"url_list": ["https://p19-webcast.tiktokcdn.com/img/lion_big.webp"]},
                "gift_panel_banner": {"url_list": []},
            },
        ],
        # Gift panels are paginated in some regions; nested gifts must be found.
        "pages": [{"gifts": [{
            "id": 7934,
            "name": "TikTok Universe",
            "diamond_count": 44999,
            "icon": {"url_list": ["https://p19-webcast.tiktokcdn.com/img/universe.webp"]},
            "image": {"url_list": ["https://p19-webcast.tiktokcdn.com/img/universe.webp"]},
        }]}],
    },
}


def test_finds_gifts_at_any_depth():
    gifts = parse_gifts(PAYLOAD)
    by_id = {g.id: g for g in gifts}
    assert set(by_id) == {"5655", "5654", "7934"}
    assert by_id["5655"].name == "Rose"
    assert by_id["5655"].diamond_count == 1
    assert by_id["5654"].primary_effect_id == "883939"
    assert by_id["5655"].primary_effect_id is None   # 0 means "no effect"


def test_asset_roles_and_mirrors():
    rose = next(g for g in parse_gifts(PAYLOAD) if g.id == "5655")
    roles = {a.role for a in rose.assets}
    assert roles == {"icon", "image", "preview"}
    icon = next(a for a in rose.assets if a.role == "icon")
    assert len(icon.urls) == 2, "every CDN mirror should be kept as a fallback"


def test_role_filter():
    gifts = parse_gifts(PAYLOAD, roles={"image"})
    assert all(a.role == "image" for g in gifts for a in g.assets)
    assert all(g.assets for g in gifts)


def test_duplicate_urls_within_a_gift_are_collapsed():
    universe = next(g for g in parse_gifts(PAYLOAD) if g.id == "7934")
    # icon and image point at the same file; only one download is scheduled.
    assert len(universe.assets) == 1


def test_empty_url_lists_are_dropped():
    lion = next(g for g in parse_gifts(PAYLOAD) if g.id == "5654")
    assert "banner" not in {a.role for a in lion.assets}


def test_slug_is_filesystem_safe():
    gifts = parse_gifts({"data": {"gifts": [{
        "id": 1, "name": "🌹 Rosa/Rosé", "diamond_count": 1,
        "icon": {"url_list": ["https://p19-webcast.tiktokcdn.com/a.webp"]},
    }]}})
    assert gifts[0].slug == "rosa-rose"


@pytest.mark.parametrize("url,expected", [
    ("https://p19-webcast.tiktokcdn.com/img/x.webp", True),
    ("https://p19-webcast.tiktokcdn.com/obj/abc~tplv-obj.image", True),
    ("https://example.com/thing.mp4", True),
    ("https://example.com/page", False),
    ("not-a-url", False),
])
def test_asset_url_filter(url, expected):
    assert _is_asset_url(url) is expected


def test_room_id_is_pulled_out_of_the_live_page():
    class FakeSession:
        def get_text(self, url, params=None):
            assert "@someone/live" in url
            return '<script>{"roomId":"7361234567890123456","other":1}</script>'

    source = WebcastSource(session=FakeSession(), username="@someone")
    assert source.resolve_room_id() == "7361234567890123456"


def test_offline_account_gives_a_useful_error():
    class FakeSession:
        def get_text(self, url, params=None):
            return '<div class="user-not-live">offline</div>'

    source = WebcastSource(session=FakeSession(), username="someone")
    with pytest.raises(HttpError, match="not live"):
        source.resolve_room_id()


def test_non_zero_status_code_is_surfaced():
    class FakeSession:
        def get_json(self, url, params=None):
            return {"status_code": 4003110, "data": {"message": "room not found"}}

    source = WebcastSource(session=FakeSession(), room_id="123")
    with pytest.raises(HttpError, match="4003110"):
        source.fetch_gift_payload()


def test_fetch_builds_a_manifest():
    class FakeSession:
        def get_json(self, url, params=None):
            assert params["room_id"] == "123"
            assert params["aid"] == BASE_PARAMS["aid"]
            return json.loads(json.dumps(PAYLOAD))

    manifest = WebcastSource(session=FakeSession(), room_id="123").fetch()
    assert manifest.source == "webcast"
    assert manifest.room_id == "123"
    assert len(manifest.gifts) == 3


def test_manifest_round_trips(tmp_path):
    from giftkit.manifest import Manifest

    class FakeSession:
        def get_json(self, url, params=None):
            return PAYLOAD

    manifest = WebcastSource(session=FakeSession(), room_id="123").fetch()
    path = manifest.save(tmp_path / "manifest.json")
    reloaded = Manifest.load(path)
    assert [g.id for g in reloaded.gifts] == [g.id for g in manifest.gifts]
    assert reloaded.gifts[0].assets[0].urls == manifest.gifts[0].assets[0].urls
