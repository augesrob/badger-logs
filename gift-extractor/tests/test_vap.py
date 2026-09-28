import pytest

import fixtures
from giftkit.formats import vap

pytestmark = pytest.mark.skipif(not fixtures.ffmpeg_available(), reason="ffmpeg required")


def test_filter_uses_the_declared_rectangles():
    graph = vap.build_filter({"rgb_rect": [0, 0, 64, 64], "alpha_rect": [0, 64, 64, 64]})
    assert "crop=64:64:0:0" in graph
    assert "crop=64:64:0:64" in graph
    assert graph.endswith("[rgb][alpha]alphamerge[out]")
    assert "scale=" not in graph


def test_half_resolution_matte_is_scaled_up():
    graph = vap.build_filter({"rgb_rect": [0, 0, 64, 64], "alpha_rect": [0, 64, 32, 32]})
    assert "crop=32:32:0:64,format=gray,scale=64:64" in graph


def test_odd_dimensions_are_rounded_to_even():
    # yuva420p subsamples chroma, so an odd crop would make ffmpeg pad or fail.
    graph = vap.build_filter({"rgb_rect": [0, 0, 65, 33], "alpha_rect": [65, 0, 65, 33]})
    assert "crop=64:32:0:0" in graph


def test_decode_reports_geometry_from_the_config(tmp_path):
    video = fixtures.make_split_alpha_mp4(tmp_path / "v.mp4", frames=8, fps=10)
    fixtures.add_vapc_box(video, rgb_rect=[0, 0, 64, 64], alpha_rect=[0, 64, 64, 64],
                          fps=10, frames=8)
    from giftkit.detect import detect

    source = vap.decode(video, detect(video).vap)
    assert (source.width, source.height) == (64, 64)
    assert source.has_alpha
    assert source.fps == 10
    assert source.frame_count == 8
