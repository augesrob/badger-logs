import pytest

import fixtures
from giftkit.detect import Kind, detect, parse_vapc, sniff_alpha_layout

pytestmark = pytest.mark.skipif(not fixtures.ffmpeg_available(), reason="ffmpeg required")


def test_detects_image_formats(tmp_path):
    assert detect(fixtures.make_animated_webp(tmp_path / "a.webp")).kind is Kind.ANIMATED_WEBP
    assert detect(fixtures.make_gif(tmp_path / "a.gif")).kind is Kind.GIF
    assert detect(fixtures.make_png(tmp_path / "a.png")).kind is Kind.PNG
    assert detect(fixtures.make_svga(tmp_path / "a.svga")).kind is Kind.SVGA


def test_static_webp_is_not_reported_as_animated(tmp_path):
    path = tmp_path / "still.webp"
    fixtures.rgba_frame((32, 32), 0).save(path, "WEBP", lossless=True)
    assert detect(path).kind is Kind.STATIC_WEBP


def test_vapc_box_gives_exact_rectangles(tmp_path):
    video = fixtures.make_split_alpha_mp4(tmp_path / "v.mp4", layout="top-bottom")
    fixtures.add_vapc_box(video, rgb_rect=[0, 0, 64, 64], alpha_rect=[0, 64, 64, 64])
    detection = detect(video)
    assert detection.kind is Kind.VAP_MP4
    assert detection.vap["rgb_rect"] == [0, 0, 64, 64]
    assert detection.vap["alpha_rect"] == [0, 64, 64, 64]
    assert detection.vap["layout"] == "top-bottom"
    assert detection.vap["source"] == "vapc"


def test_vapc_survives_a_scaled_matte(tmp_path):
    """A half-resolution matte must round-trip through the parser untouched."""
    video = fixtures.make_split_alpha_mp4(tmp_path / "v2.mp4")
    fixtures.add_vapc_box(video, rgb_rect=[0, 0, 64, 64], alpha_rect=[0, 64, 32, 32])
    vap = parse_vapc(video.read_bytes())
    assert vap["alpha_rect"] == [0, 64, 32, 32]
    assert vap["layout"] == "top-bottom"


@pytest.mark.parametrize("layout", ["top-bottom", "bottom-top", "left-right", "right-left"])
def test_sniffing_recovers_every_layout_without_a_vapc_box(tmp_path, layout):
    video = fixtures.make_split_alpha_mp4(tmp_path / f"{layout}.mp4", layout=layout)
    assert detect(video).kind is Kind.MP4  # no config box to read
    sniffed = sniff_alpha_layout(video)
    assert sniffed is not None, "heuristic failed to spot the matte"
    assert sniffed["layout"] == layout
    assert sniffed["source"] == "sniffed"


def test_opaque_video_is_not_mistaken_for_split_alpha(tmp_path):
    video = fixtures.make_opaque_mp4(tmp_path / "plain.mp4")
    assert detect(video).kind is Kind.MP4
    assert sniff_alpha_layout(video) is None


def test_zip_and_garbage(tmp_path):
    webp = fixtures.make_animated_webp(tmp_path / "inner.webp")
    bundle = fixtures.make_zip_bundle(tmp_path / "bundle.zip", {"inner.webp": webp})
    assert detect(bundle).kind is Kind.ZIP

    junk = tmp_path / "junk.bin"
    junk.write_bytes(b"\x00\x01\x02\x03" * 64)
    assert detect(junk).kind is Kind.UNKNOWN

    empty = tmp_path / "empty.bin"
    empty.write_bytes(b"")
    assert detect(empty).kind is Kind.UNKNOWN


def test_gzip_that_is_not_svga_is_not_claimed(tmp_path):
    import gzip

    path = tmp_path / "notes.txt.gz"
    path.write_bytes(gzip.compress(b"just some text, definitely not a movie entity"))
    assert detect(path).kind is Kind.UNKNOWN
