import fixtures
from giftkit.formats import svga


def test_parses_a_movie_entity(tmp_path):
    path = fixtures.make_svga(tmp_path / "m.svga", size=(80, 80), frames=5, fps=20)
    movie = svga.parse(path)
    assert movie.version == "2.0.0"
    assert (movie.width, movie.height) == (80, 80)
    assert movie.fps == 20
    assert movie.frames == 5
    assert len(movie.sprites) == 1
    assert movie.sprites[0].image_key == "ball"
    assert len(movie.sprites[0].frames) == 5
    assert set(movie.images) == {"ball"}
    assert not movie.has_vector_shapes


def test_looks_like_svga_rejects_other_gzip(tmp_path):
    import gzip

    good = fixtures.make_svga(tmp_path / "good.svga")
    bad = tmp_path / "bad.gz"
    bad.write_bytes(gzip.compress(b"\x00" * 512))
    assert svga.looks_like_svga(good)
    assert not svga.looks_like_svga(bad)


def test_renders_transparent_frames_and_animates(tmp_path):
    from PIL import Image

    path = fixtures.make_svga(tmp_path / "m.svga", size=(80, 80), frames=5)
    sequence = svga.render(svga.parse(path), tmp_path / "frames")
    assert sequence.frame_count == 5
    assert (sequence.width, sequence.height) == (80, 80)

    rendered = sorted((tmp_path / "frames").glob("*.png"))
    assert len(rendered) == 5

    boxes = []
    for frame_path in rendered:
        with Image.open(frame_path) as image:
            assert image.mode == "RGBA"
            assert image.getpixel((79, 79))[3] == 0, "background must stay transparent"
            boxes.append(image.getchannel("A").getbbox())
    # The sprite is translated a little further right on every frame.
    assert boxes[0][0] < boxes[-1][0]
    assert all(box is not None for box in boxes)


def test_frame_alpha_is_applied(tmp_path):
    from PIL import Image

    path = fixtures.make_svga(tmp_path / "m.svga", frames=3)
    movie = svga.parse(path)
    for sprite in movie.sprites:
        for frame in sprite.frames:
            frame.alpha = 0.5
    svga.render(movie, tmp_path / "faded")
    with Image.open(tmp_path / "faded" / "00000.png") as image:
        alphas = [a for a in image.getchannel("A").tobytes() if a]
    assert alphas, "sprite disappeared entirely"
    assert max(alphas) <= 140, "50% frame alpha was not applied"


def test_truncated_payload_is_reported_not_crashed(tmp_path):
    import gzip

    import pytest

    path = tmp_path / "broken.svga"
    raw = gzip.decompress(fixtures.make_svga(tmp_path / "ok.svga").read_bytes())
    path.write_bytes(gzip.compress(raw[: len(raw) // 2]))
    with pytest.raises(Exception):
        svga.parse(path)
    assert not svga.looks_like_svga(path)
