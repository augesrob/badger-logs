"""Local overlay server for OBS.

Serves the converted library plus a browser-source page, and exposes a trigger
endpoint so anything that knows about gift events - a TikTokLive listener, a
Stream Deck button, a curl in a shell script - can make the overlay play a
gift. Server-sent events keep it to the standard library: no websocket
dependency, and reconnection is handled by the browser for free.
"""
from __future__ import annotations

import json
import queue
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import library as library_mod
from .util import LOG

OVERLAY_DIR = Path(__file__).resolve().parents[1] / "overlay"


class _Broadcaster:
    """Fan-out of trigger events to every connected browser source."""

    def __init__(self) -> None:
        self._clients: set[queue.Queue] = set()
        self._lock = threading.Lock()

    def subscribe(self) -> queue.Queue:
        client: queue.Queue = queue.Queue(maxsize=64)
        with self._lock:
            self._clients.add(client)
        return client

    def unsubscribe(self, client: queue.Queue) -> None:
        with self._lock:
            self._clients.discard(client)

    def publish(self, payload: dict) -> int:
        message = json.dumps(payload)
        with self._lock:
            clients = list(self._clients)
        delivered = 0
        for client in clients:
            try:
                client.put_nowait(message)
                delivered += 1
            except queue.Full:
                LOG.warning("overlay client is not keeping up; dropping event")
        return delivered

    @property
    def client_count(self) -> int:
        with self._lock:
            return len(self._clients)


class OverlayHandler(SimpleHTTPRequestHandler):
    broadcaster: _Broadcaster
    library_path: Path

    def __init__(self, *args, directory: str, **kwargs):
        super().__init__(*args, directory=directory, **kwargs)

    def log_message(self, fmt: str, *args) -> None:  # quieter than the default
        LOG.debug("http: " + fmt, *args)

    # -- routing -----------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802 - stdlib naming
        route = urlparse(self.path)
        if route.path in ("/", "/index.html"):
            return self._send_file(OVERLAY_DIR / "index.html", "text/html; charset=utf-8")
        if route.path == "/events":
            return self._stream_events()
        if route.path == "/trigger":
            params = {k: v[0] for k, v in parse_qs(route.query).items()}
            return self._trigger(params)
        if route.path == "/library.json":
            return self._send_file(self.library_path, "application/json; charset=utf-8")
        return super().do_GET()

    def do_POST(self) -> None:  # noqa: N802 - stdlib naming
        route = urlparse(self.path)
        if route.path != "/trigger":
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b"{}"
        try:
            params = json.loads(body or b"{}")
        except json.JSONDecodeError:
            params = {k: v[0] for k, v in parse_qs(body.decode("utf-8", "replace")).items()}
        self._trigger(params)

    # -- handlers ----------------------------------------------------------
    def _trigger(self, params: dict) -> None:
        gift = params.get("gift") or params.get("id") or params.get("name")
        if not gift:
            self._send_json({"ok": False, "error": "pass ?gift=<id|name>"}, status=400)
            return
        try:
            library = library_mod.load(self.library_path)
        except OSError as exc:
            self._send_json({"ok": False, "error": f"library unavailable: {exc}"}, status=500)
            return
        asset = library_mod.lookup(library, str(gift))
        if not asset:
            self._send_json({"ok": False, "error": f"no asset for {gift!r}"}, status=404)
            return
        payload = {
            "type": "play",
            "gift": asset["name"] or asset["gift_id"],
            "gift_id": asset["gift_id"],
            "src": "/" + asset["file"].lstrip("/"),
            "duration": asset.get("duration", 0),
            "repeat": int(params.get("repeat", 1) or 1),
        }
        delivered = self.broadcaster.publish(payload)
        self._send_json({"ok": True, "delivered": delivered, "asset": payload})

    def _stream_events(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        client = self.broadcaster.subscribe()
        try:
            self.wfile.write(b": connected\n\n")
            self.wfile.flush()
            while True:
                try:
                    message = client.get(timeout=15)
                    self.wfile.write(f"data: {message}\n\n".encode("utf-8"))
                except queue.Empty:
                    self.wfile.write(b": keepalive\n\n")  # keeps proxies from idling us out
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            LOG.debug("overlay client disconnected")
        finally:
            self.broadcaster.unsubscribe(client)

    def _send_file(self, path: Path, content_type: str) -> None:
        try:
            data = path.read_bytes()
        except OSError as exc:
            self.send_error(404, str(exc))
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _send_json(self, payload: dict, status: int = 200) -> None:
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(data)


def make_server(root: Path, library_path: Path, host: str = "127.0.0.1",
                port: int = 8722) -> ThreadingHTTPServer:
    handler = type(
        "BoundOverlayHandler",
        (OverlayHandler,),
        {"broadcaster": _Broadcaster(), "library_path": Path(library_path)},
    )
    return ThreadingHTTPServer((host, port), partial(handler, directory=str(root)))


def serve(root: Path, library_path: Path, host: str = "127.0.0.1", port: int = 8722) -> None:
    server = make_server(root, library_path, host, port)
    LOG.info("overlay ready at http://%s:%d  (add as an OBS browser source)", host, port)
    LOG.info("trigger a gift:  curl 'http://%s:%d/trigger?gift=Rose'", host, port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        LOG.info("shutting down")
    finally:
        server.server_close()
