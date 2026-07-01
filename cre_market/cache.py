"""File-based HTTP cache with per-host throttling and retries.

Government APIs (especially BLS and HUD) have rate limits; every request in
this tool goes through ``CachedSession`` so that (a) repeat runs hit the local
disk cache instead of the network and (b) live requests are spaced out.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from urllib.parse import urlencode, urlparse

import requests

from .config import DEFAULT_CACHE_DIR, HOST_THROTTLE_SECONDS

log = logging.getLogger(__name__)

_RETRY_STATUSES = {429, 500, 502, 503, 504}


class CachedSession:
    def __init__(self, cache_dir: str = DEFAULT_CACHE_DIR, enabled: bool = True,
                 max_retries: int = 4):
        self.cache_dir = cache_dir
        self.enabled = enabled
        self.max_retries = max_retries
        self._session = requests.Session()
        self._session.headers["User-Agent"] = "cre-market-research/0.1 (internship tool)"
        self._last_request_at: dict[str, float] = {}
        if enabled:
            os.makedirs(cache_dir, exist_ok=True)

    # -- cache plumbing -----------------------------------------------------
    def _cache_path(self, url: str, params: dict | None, binary: bool) -> str:
        raw = url + "?" + urlencode(sorted((params or {}).items()))
        digest = hashlib.sha256(raw.encode()).hexdigest()[:32]
        ext = "bin" if binary else "txt"
        return os.path.join(self.cache_dir, f"{digest}.{ext}")

    def _throttle(self, url: str) -> None:
        host = urlparse(url).netloc
        min_gap = HOST_THROTTLE_SECONDS.get(host, 0.25)
        last = self._last_request_at.get(host, 0.0)
        wait = last + min_gap - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self._last_request_at[host] = time.monotonic()

    # -- public API ----------------------------------------------------------
    def get(self, url: str, params: dict | None = None, headers: dict | None = None,
            binary: bool = False, timeout: int = 60) -> bytes | str:
        """GET with disk cache. Returns text (str) or raw bytes if binary=True.

        Cache keys never include headers, so Authorization tokens are not
        written to disk.
        """
        path = self._cache_path(url, params, binary)
        if self.enabled and os.path.exists(path):
            mode = "rb" if binary else "r"
            with open(path, mode) as fh:
                return fh.read()

        resp = self._request(url, params, headers, timeout)
        data = resp.content if binary else resp.text
        if self.enabled:
            mode = "wb" if binary else "w"
            with open(path, mode) as fh:
                fh.write(data)
        return data

    def get_json(self, url: str, params: dict | None = None,
                 headers: dict | None = None):
        return json.loads(self.get(url, params, headers))

    def _request(self, url, params, headers, timeout) -> requests.Response:
        delay = 2.0
        for attempt in range(self.max_retries + 1):
            self._throttle(url)
            try:
                resp = self._session.get(url, params=params, headers=headers,
                                         timeout=timeout, allow_redirects=True)
            except requests.RequestException as exc:
                if attempt == self.max_retries:
                    raise
                log.warning("request error for %s (%s); retrying in %.0fs", url, exc, delay)
                time.sleep(delay)
                delay *= 2
                continue
            if resp.status_code in _RETRY_STATUSES and attempt < self.max_retries:
                retry_after = resp.headers.get("Retry-After")
                pause = float(retry_after) if retry_after and retry_after.isdigit() else delay
                log.warning("HTTP %s for %s; retrying in %.0fs", resp.status_code, url, pause)
                time.sleep(pause)
                delay *= 2
                continue
            resp.raise_for_status()
            return resp
        raise RuntimeError(f"unreachable retry loop for {url}")
