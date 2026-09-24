"""End-to-end: real files in, real alpha video out."""
import pytest

import fixtures
from giftkit.convert import ConvertOptions, convert_file
from giftkit.detect import Kind
from giftkit.ffmpeg import probe

pytestmark = pytest.mark.skipif(not fixtures.ffmpeg_available(), reason="ffmpeg required")


def options(tmp_path, **overrides):
    defaults = dict(out_dir=tmp_path / "out", target="webm", crf=32, workers=1)
    defaults.update(overrides)
    return ConvertOptions(**defaults)


def assert_alpha_webm(path, *, width=None, height=None):
    """Check the output really is transparent.

    Note we do not assert on the reported pixel format: VP9 keeps alpha in a
    side channel, so ffmpeg's default decoder shows `yuv420p` for a file that
    is genuinely transparent. The only honest test is to decode a frame with
    libvpx-vp9 and look at the alpha channel.
    """
    info = probe(path)
    assert info.codec in {"vp9", "vp8"}
    if width:
        assert info.width == width
    if height:
        assert info.height == height
    alpha = fixtures.decode_rgba_frame(path, 0).getchannel("A")
    low, high = alpha.getextrema()
    assert low < 32, f"nothing is transparent (alpha range {low}-{high})"
    assert high > 200, f"nothing is opaque (alpha range {low}-{high})"


def test_animated_webp_becomes_transparent_webm(tmp_path):
    src = fixtures.make_animated_webp(tmp_path / "gift.webp", frames=6, size=(64, 64))
    (result,) = convert_file(src, tmp_path / "out" / "gift", options(tmp_path))

    assert result.status == "converted", result.reason
    assert result.kind == Kind.ANIMATED_WEBP.value
    assert result.frames == 6
    assert result.has_alpha
    assert_alpha_webm(result.output, width=64, height=64)

    frame = fixtures.decode_rgba_frame(result.output, 0)
    assert frame.getpixel((63, 63))[3] < 32, "corner should be see-through"
    assert frame.getpixel((32, 32))[3] > 200, "subject should be opaque"


def test_vap_mp4_is_recombined_into_alpha(tmp_path):
    video = fixtures.make_split_alpha_mp4(tmp_path / "v.mp4", layout="top-bottom")
    fixtures.add_vapc_box(video, rgb_rect=[0, 0, 64, 64], alpha_rect=[0, 64, 64, 64])
    (result,) = convert_file(video, tmp_path / "out" / "v", options(tmp_path))

    assert result.status == "converted", result.reason
    assert result.kind == Kind.VAP_MP4.value
    # The output is one half of the source: the matte is now the alpha channel.
    assert_alpha_webm(result.output, width=64, height=64)
    frame = fixtures.decode_rgba_frame(result.output, 0)
    assert frame.getpixel((63, 63))[3] < 48
    assert frame.getpixel((20, 32))[3] > 180


def test_split_mp4_without_a_config_box_is_sniffed(tmp_path):
    video = fixtures.make_split_alpha_mp4(tmp_path / "s.mp4", layout="left-right")
    (result,) = convert_file(video, tmp_path / "out" / "s", options(tmp_path))
    assert result.status == "converted", result.reason
    assert result.kind == Kind.ALPHA_SPLIT_MP4.value
    assert_alpha_webm(result.output, width=64, height=64)


def test_forced_layout_overrides_the_heuristic(tmp_path):
    video = fixtures.make_split_alpha_mp4(tmp_path / "f.mp4", layout="bottom-top")
    (result,) = convert_file(video, tmp_path / "out" / "f",
                             options(tmp_path, force_layout="bottom-top"))
    assert result.status == "converted", result.reason
    assert_alpha_webm(result.output, width=64, height=64)


def test_opaque_video_converts_without_alpha_or_is_skipped(tmp_path):
    video = fixtures.make_opaque_mp4(tmp_path / "plain.mp4")

    (kept,) = convert_file(video, tmp_path / "out" / "plain", options(tmp_path))
    assert kept.status == "converted"
    assert kept.has_alpha is False
    assert "no alpha channel detected; output will be opaque" in kept.notes

    (skipped,) = convert_file(video, tmp_path / "out" / "plain2",
                              options(tmp_path, skip_opaque=True))
    assert skipped.status == "skipped"


def test_svga_renders_to_webm(tmp_path):
    src = fixtures.make_svga(tmp_path / "m.svga", size=(80, 80), frames=5, fps=20)
    (result,) = convert_file(src, tmp_path / "out" / "m", options(tmp_path))
    assert result.status == "converted", result.reason
    assert result.kind == Kind.SVGA.value
    assert result.frames == 5
    assert_alpha_webm(result.output, width=80, height=80)


def test_gif_converts(tmp_path):
    src = fixtures.make_gif(tmp_path / "g.gif", frames=4, size=(48, 48))
    (result,) = convert_file(src, tmp_path / "out" / "g", options(tmp_path))
    assert result.status == "converted", result.reason
    assert result.frames == 4


def test_still_images_land_in_the_stills_folder(tmp_path):
    src = fixtures.make_png(tmp_path / "icon.png")
    (result,) = convert_file(src, tmp_path / "out" / "icon", options(tmp_path))
    assert result.status == "still"
    assert result.output.parent.name == "stills"
    assert result.output.suffix == ".png"

    (skipped,) = convert_file(src, tmp_path / "out" / "icon2",
                              options(tmp_path, keep_stills=False))
    assert skipped.status == "skipped"


def test_archives_are_expanded(tmp_path):
    webp = fixtures.make_animated_webp(tmp_path / "inner.webp", frames=3)
    png = fixtures.make_png(tmp_path / "inner.png")
    bundle = fixtures.make_zip_bundle(tmp_path / "b.zip",
                                      {"a/inner.webp": webp, "a/inner.png": png})
    results = convert_file(bundle, tmp_path / "out" / "b", options(tmp_path))
    assert len(results) == 2
    assert {r.status for r in results} == {"converted", "still"}
    assert all("from archive entry" in " ".join(r.notes) for r in results)


def test_zip_slip_entries_are_refused(tmp_path):
    import zipfile

    bundle = tmp_path / "evil.zip"
    with zipfile.ZipFile(bundle, "w") as archive:
        archive.writestr("../escaped.webp", b"not really a webp")
    (result,) = convert_file(bundle, tmp_path / "out" / "evil", options(tmp_path))
    assert result.status == "skipped"
    assert not (tmp_path / "escaped.webp").exists()


def test_max_frames_and_max_width_are_honoured(tmp_path):
    src = fixtures.make_animated_webp(tmp_path / "big.webp", frames=10, size=(128, 128))
    (result,) = convert_file(src, tmp_path / "out" / "big",
                             options(tmp_path, max_frames=4, max_width=64))
    assert result.status == "converted", result.reason
    assert result.frames == 4
    assert probe(result.output).width == 64


def test_prores_target(tmp_path):
    src = fixtures.make_animated_webp(tmp_path / "p.webp", frames=3, size=(32, 32))
    (result,) = convert_file(src, tmp_path / "out" / "p", options(tmp_path, target="mov"))
    assert result.status == "converted", result.reason
    assert result.output.suffix == ".mov"
    assert probe(result.output).codec == "prores"


def test_png_sequence_target(tmp_path):
    src = fixtures.make_animated_webp(tmp_path / "q.webp", frames=3, size=(32, 32))
    (result,) = convert_file(src, tmp_path / "out" / "q", options(tmp_path, target="png-seq"))
    assert result.status == "converted", result.reason
    assert len(list(result.output.glob("*.png"))) == 3


def test_unreadable_asset_is_reported_not_raised(tmp_path):
    junk = tmp_path / "junk.bin"
    junk.write_bytes(b"\x00\x11\x22\x33" * 200)
    (result,) = convert_file(junk, tmp_path / "out" / "junk", options(tmp_path))
    assert result.status == "skipped"
    assert result.reason


def test_max_width_also_applies_to_the_video_filter_path(tmp_path):
    """The scale must be appended to the alphamerge graph, not fight with it."""
    video = fixtures.make_split_alpha_mp4(tmp_path / "wide.mp4", layout="top-bottom",
                                          half=(128, 128))
    fixtures.add_vapc_box(video, rgb_rect=[0, 0, 128, 128], alpha_rect=[0, 128, 128, 128])
    (result,) = convert_file(video, tmp_path / "out" / "wide",
                             options(tmp_path, max_width=64))
    assert result.status == "converted", result.reason
    assert probe(result.output).width == 64
    assert_alpha_webm(result.output, width=64)


def test_frame_count_is_backfilled_from_the_output(tmp_path):
    """mp4 containers rarely declare nb_frames; the library must not show 0."""
    video = fixtures.make_split_alpha_mp4(tmp_path / "count.mp4", layout="left-right",
                                          frames=12, fps=12)
    (result,) = convert_file(video, tmp_path / "out" / "count", options(tmp_path))
    assert result.status == "converted", result.reason
    assert result.frames > 0
