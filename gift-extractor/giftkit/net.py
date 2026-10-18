"""HTTP plumbing: a browser-shaped session plus a resilient file downloader."""
from __future__ import annotations

import shutil
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Callable, Iterable, Sequence

from .util import LOG, sha256_file

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
DEFAULT_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.tiktok.com/",
}


class HttpError(RuntimeError):
    pass


class Session:
    """Thin wrapper over ``requests`` with a stdlib fallback.

    ``requests`` gives us connection pooling and transparent decompression, but
    the convert stage must keep working on a machine that only has the stdlib,
    so we degrade instead of hard-failing at import time.
    """

    def __init__(self, headers: dict[str, str] | None = None, timeout: float = 30.0,
                 cookies: dict[str, str] | None = None, retries: int = 3):
        self.timeout = timeout
        self.retries = retries
        self.headers = {**DEFAULT_HEADERS, **(headers or {})}
        self.cookies = cookies or {}
        try:
            import requests  # noqa: F401

            self._requests = requests
            self._session = requests.Session()
            self._session.headers.update(self.headers)
            self._session.cookies.update(self.cookies)
        except ImportError:  # pragma: no cover - exercised only on bare installs
            LOG.debug("requests not installed; using urllib")
            self._requests = None
            self._session = None

    # -- low level ---------------------------------------------------------
    def get_bytes(self, url: str, params: dict[str, str] | None = None) -> bytes:
        last: Exception | None = None
        for attempt in range(1, self.retries + 1):
            try:
                if self._session is not None:
                    resp = self._session.get(url, params=params, timeout=self.timeout)
                    if resp.status_code >= 400:
                        raise HttpError(f"HTTP {resp.status_code} for {url}")
                    return resp.content
                full = url
                if params:
                    from urllib.parse import urlencode

                    full = f"{url}{'&' if '?' in url else '?'}{urlencode(params)}"
                request = urllib.request.Request(full, headers=self.headers)
                with urllib.request.urlopen(request, timeout=self.timeout) as resp:
                    return resp.read()
            except Exception as exc:  # noqa: BLE001 - retried below
                last = exc
                if attempt < self.retries:
                    backoff = 2 ** (attempt - 1)
                    LOG.debug("GET %s failed (%s); retry in %ss", url, exc, backoff)
                    time.sleep(backoff)
        raise HttpError(f"GET {url} failed after {self.retries} attempts: {last}")

    def get_text(self, url: str, params: dict[str, str] | None = None) -> str:
        return self.get_bytes(url, params).decode("utf-8", "replace")

    def get_json(self, url: str, params: dict[str, str] | None = None):
        import json

        raw = self.get_text(url, params)
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            snippet = raw[:200].replace("\n", " ")
            raise HttpError(f"{url} did not return JSON ({exc}); body starts: {snippet!r}")

    # -- files -------------------------------------------------------------
    def download(self, urls: Sequence[str], dest: Path, *, overwrite: bool = False) -> Path:
        """Fetch the first URL that works into ``dest``.

        Writes through a ``.part`` file so an interrupted run never leaves a
        truncated asset that later looks cached.
        """
        dest = Path(dest)
        if dest.exists() and dest.stat().st_size > 0 and not overwrite:
            LOG.debug("cached: %s", dest.name)
            return dest
        dest.parent.mkdir(parents=True, exist_ok=True)
        errors = []
        for url in urls:
            part = dest.with_suffix(dest.suffix + ".part")
            try:
                if self._session is not None:
                    with self._session.get(url, stream=True, timeout=self.timeout) as resp:
                        if resp.status_code >= 400:
                            raise HttpError(f"HTTP {resp.status_code}")
                        with open(part, "wb") as handle:
                            for chunk in resp.iter_content(1 << 16):
                                handle.write(chunk)
                else:
                    request = urllib.request.Request(url, headers=self.headers)
                    with urllib.request.urlopen(request, timeout=self.timeout) as resp, \
                            open(part, "wb") as handle:
                        shutil.copyfileobj(resp, handle)
                if part.stat().st_size == 0:
                    raise HttpError("empty response body")
                part.replace(dest)
                return dest
            except Exception as exc:  # noqa: BLE001 - try the next mirror
                errors.append(f"{url}: {exc}")
                part.unlink(missing_ok=True)
        raise HttpError("all mirrors failed:\n  " + "\n  ".join(errors))


def download_many(
    session: Session,
    jobs: Iterable[tuple[Sequence[str], Path]],
    *,
    workers: int = 8,
    overwrite: bool = False,
    on_done: Callable[[Path, Exception | None], None] | None = None,
) -> dict[Path, Exception | None]:
    """Download in parallel; never raises, reports per-job outcomes."""
    results: dict[Path, Exception | None] = {}
    jobs = list(jobs)
    if not jobs:
        return results
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = {
            pool.submit(session.download, urls, dest, overwrite=overwrite): dest
            for urls, dest in jobs
        }
        for future in as_completed(futures):
            dest = futures[future]
            try:
                future.result()
                results[dest] = None
            except Exception as exc:  # noqa: BLE001
                results[dest] = exc
            if on_done:
                on_done(dest, results[dest])
    return results


def file_digest(path: Path) -> str:
    return sha256_file(path)
