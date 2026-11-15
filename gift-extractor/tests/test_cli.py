"""Whole-pipeline tests driven through the command line."""
import json

import pytest

import fixtures
from giftkit import library as library_mod
from giftkit.cli import main

pytestmark = pytest.mark.skipif(not fixtures.ffmpeg_available(), reason="ffmpeg required")


@pytest.fixture
def dump(tmp_path):
    """A directory shaped like an app cache dump: mixed formats, odd names."""
    raw = tmp_path / "raw_gifts"
    raw.mkdir()
    fixtures.make_animated_webp(raw / "rose.webp", frames=5, size=(48, 48))
    fixtures.make_svga(raw / "lion.svga", size=(64, 64), frames=4)
    video = fixtures.make_split_alpha_mp4(raw / "universe.mp4", layout="left-right")
    fixtures.add_vapc_box(video, rgb_rect=[0, 0, 64, 64], alpha_rect=[64, 0, 64, 64])
    fixtures.make_png(raw / "panel_icon.png", size=(48, 48))
    # An extensionless CDN blob, which is how cached assets usually look.
    (raw / "9f8e7d6c5b4a").write_bytes((raw / "rose.webp").read_bytes())
    return raw


def test_fetch_then_convert_then_lookup(tmp_path, dump):
    out = tmp_path / "gifts"

    assert main(["fetch", "--source", "dir", "--path", str(dump), "--out", str(out)]) == 0
    manifest = json.loads((out / "manifest.json").read_text())
    assert len(manifest["gifts"]) == 5

    assert main(["convert", "--out", str(out), "--crf", "40", "--workers", "2"]) == 0
    library = json.loads((out / "library.json").read_text())

    kinds = {asset["kind"] for asset in library["assets"]}
    assert {"animated-webp", "svga", "vap-mp4"} <= kinds
    assert library["counts"]["gifts"] >= 4

    animations = [a for a in library["assets"] if a["frames"] > 1]
    assert len(animations) >= 3
    for asset in animations:
        assert (out / asset["file"]).is_file()
        assert asset["has_alpha"]
        assert asset["fps"] > 0
        assert asset["duration"] > 0

    # The duplicate blob must reuse the first conversion, not re-encode.
    webms = {a["file"] for a in library["assets"] if a["file"].endswith(".webm")}
    assert len(webms) == len(list((out / "webm").glob("*.webm")))

    found = library_mod.lookup(library, "lion")
    assert found is not None
    assert found["kind"] == "svga"
    assert library_mod.lookup(library, "no-such-gift") is None


def test_still_images_are_kept_separately(tmp_path, dump):
    out = tmp_path / "gifts"
    main(["fetch", "--source", "dir", "--path", str(dump), "--out", str(out)])
    main(["convert", "--out", str(out), "--crf", "40"])
    assert (out / "stills" / "panel_icon.png").is_file()


def test_convert_without_a_manifest_fails_cleanly(tmp_path):
    assert main(["convert", "--out", str(tmp_path / "nothing")]) == 1


def test_fetch_requires_a_target(tmp_path):
    assert main(["fetch", "--out", str(tmp_path / "o")]) == 1
    assert main(["fetch", "--source", "dir", "--out", str(tmp_path / "o")]) == 1


def test_inspect_reports_the_alpha_geometry(tmp_path, capsys):
    video = fixtures.make_split_alpha_mp4(tmp_path / "v.mp4", layout="top-bottom")
    fixtures.add_vapc_box(video, rgb_rect=[0, 0, 64, 64], alpha_rect=[0, 64, 64, 64])
    assert main(["inspect", str(video)]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["kind"] == "vap-mp4"
    assert report["vap"]["layout"] == "top-bottom"


def test_inspect_sniffs_a_bare_split_video(tmp_path, capsys):
    video = fixtures.make_split_alpha_mp4(tmp_path / "b.mp4", layout="right-left")
    assert main(["inspect", str(video)]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["kind"] == "mp4"
    assert report["sniffed_alpha"]["layout"] == "right-left"


def test_doctor_runs(capsys):
    assert main(["doctor"]) == 0
    assert "ffmpeg" in capsys.readouterr().out


def test_library_records_why_things_were_skipped(tmp_path):
    raw = tmp_path / "raw"
    raw.mkdir()
    (raw / "mystery.bin").write_bytes(b"\x7f\x45\x4c\x46" + b"\x00" * 2048)
    out = tmp_path / "gifts"
    main(["fetch", "--source", "dir", "--path", str(raw), "--out", str(out)])
    main(["convert", "--out", str(out)])
    library = json.loads((out / "library.json").read_text())
    assert library["counts"]["assets"] == 0
    assert len(library["skipped"]) == 1
    assert library["skipped"][0]["status"] == "skipped"
    assert library["skipped"][0]["reason"]
