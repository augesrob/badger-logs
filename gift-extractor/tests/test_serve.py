"""The overlay server: trigger in, server-sent event out."""
import json
import threading
import urllib.request
from http.client import HTTPConnection

import pytest

from giftkit.serve import make_server

LIBRARY = {
    "version": 1,
    "counts": {"gifts": 2, "assets": 2, "skipped": 0},
    "assets": [
        {"gift_id": "5655", "name": "Rose", "slug": "rose", "role": "image",
         "file": "webm/rose-5655-image.webm", "kind": "animated-webp",
         "width": 64, "height": 64, "fps": 25, "frames": 10, "duration": 0.4,
         "has_alpha": True},
        {"gift_id": "5654", "name": "Lion", "slug": "lion", "role": "image",
         "file": "webm/lion-5654-image.webm", "kind": "vap-mp4",
         "width": 720, "height": 1280, "fps": 30, "frames": 90, "duration": 3.0,
         "has_alpha": True},
    ],
    "skipped": [],
}


@pytest.fixture
def server(tmp_path):
    root = tmp_path / "gifts"
    (root / "webm").mkdir(parents=True)
    for asset in LIBRARY["assets"]:
        (root / asset["file"]).write_bytes(b"fake webm bytes")
    library_path = root / "library.json"
    library_path.write_text(json.dumps(LIBRARY))

    httpd = make_server(root, library_path, host="127.0.0.1", port=0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


def get_json(base, path):
    with urllib.request.urlopen(base + path, timeout=5) as response:
        return response.status, json.loads(response.read())


def test_serves_the_overlay_page_and_library(server):
    with urllib.request.urlopen(server + "/", timeout=5) as response:
        body = response.read().decode()
    assert "EventSource('/events')" in body

    status, library = get_json(server, "/library.json")
    assert status == 200
    assert library["counts"]["assets"] == 2


def test_serves_converted_files(server):
    with urllib.request.urlopen(server + "/webm/rose-5655-image.webm", timeout=5) as resp:
        assert resp.read() == b"fake webm bytes"


def test_trigger_resolves_by_name_and_by_id(server):
    status, payload = get_json(server, "/trigger?gift=Rose")
    assert status == 200 and payload["ok"]
    assert payload["asset"]["src"] == "/webm/rose-5655-image.webm"

    _status, by_id = get_json(server, "/trigger?gift=5654")
    assert by_id["asset"]["gift"] == "Lion"
    assert by_id["asset"]["duration"] == 3.0


def test_unknown_gift_is_a_404(server):
    with pytest.raises(urllib.error.HTTPError) as raised:
        urllib.request.urlopen(server + "/trigger?gift=Nope", timeout=5)
    assert raised.value.code == 404


def test_missing_parameter_is_a_400(server):
    with pytest.raises(urllib.error.HTTPError) as raised:
        urllib.request.urlopen(server + "/trigger", timeout=5)
    assert raised.value.code == 400


def test_event_stream_receives_the_trigger(server):
    host, port = server.removeprefix("http://").split(":")
    connection = HTTPConnection(host, int(port), timeout=10)
    connection.request("GET", "/events")
    response = connection.getresponse()
    assert response.status == 200
    assert response.getheader("Content-Type") == "text/event-stream"
    assert response.readline().startswith(b": connected")

    status, payload = get_json(server, "/trigger?gift=Lion&repeat=2")
    assert payload["delivered"] == 1, "the connected overlay should have been notified"

    line = response.readline()
    while line in (b"\n", b"") or line.startswith(b":"):
        line = response.readline()
    assert line.startswith(b"data: ")
    event = json.loads(line[len(b"data: "):])
    assert event == {
        "type": "play", "gift": "Lion", "gift_id": "5654",
        "src": "/webm/lion-5654-image.webm", "duration": 3.0, "repeat": 2,
    }
    connection.close()


def test_trigger_accepts_json_post(server):
    request = urllib.request.Request(
        server + "/trigger", data=json.dumps({"gift": "rose"}).encode(),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        payload = json.loads(response.read())
    assert payload["ok"] and payload["asset"]["gift_id"] == "5655"
