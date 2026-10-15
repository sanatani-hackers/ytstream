"""Visitor ID handling.

Anonymous InnerTube requests without a visitor ID are much more likely to be
answered with "Sign in to confirm you're not a bot". A visitor ID is fetched
once, cached in memory and (by default) in a small file.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from typing import Optional

from .clients import WEB_USER_AGENT, WEB_VISITOR_CONTEXT
from .http import Session

log = logging.getLogger("ytstream")

VISITOR_URL = "https://www.youtube.com/youtubei/v1/visitor_id?prettyPrint=false"


def default_cache_dir() -> str:
    base = os.environ.get("YTSTREAM_CACHE_DIR")
    if base:
        return base
    root = os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
    return os.path.join(root, "ytstream")


class VisitorStore:
    def __init__(self, cache_dir: Optional[str], proxy_key: str = "", ttl: float = 24 * 3600,
                 fixed: Optional[str] = None):
        self.cache_dir = cache_dir
        self.proxy_key = proxy_key
        self.ttl = ttl
        self.fixed = fixed
        self._value: Optional[str] = None
        self._ts = 0.0

    @property
    def _path(self) -> Optional[str]:
        return os.path.join(self.cache_dir, "visitor.json") if self.cache_dir else None

    def _read_disk(self) -> None:
        path = self._path
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8") as fh:
                entry = json.load(fh).get(self.proxy_key)
            if entry and time.time() - entry["ts"] < self.ttl:
                self._value, self._ts = entry["value"], entry["ts"]
        except (OSError, ValueError, KeyError, TypeError):
            pass

    def _write_disk(self) -> None:
        path = self._path
        if not path or not self._value:
            return
        try:
            os.makedirs(self.cache_dir, exist_ok=True)
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
                if not isinstance(data, dict):
                    data = {}
            except (OSError, ValueError):
                data = {}
            data[self.proxy_key] = {"value": self._value, "ts": self._ts}
            fd, tmp = tempfile.mkstemp(dir=self.cache_dir, suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh)
            os.replace(tmp, path)
        except OSError as exc:
            log.debug("could not save visitor id: %s", exc)

    def get(self, session: Session, force: bool = False) -> Optional[str]:
        if self.fixed:
            return self.fixed
        if not force:
            if self._value and time.time() - self._ts < self.ttl:
                return self._value
            self._read_disk()
            if self._value and time.time() - self._ts < self.ttl:
                return self._value
        try:
            data = session.post_json(
                VISITOR_URL, {"context": {"client": WEB_VISITOR_CONTEXT}},
                {"User-Agent": WEB_USER_AGENT},
            )
            value = (data.get("responseContext") or {}).get("visitorData")
        except Exception as exc:  # network trouble must not break extraction
            log.debug("visitor id fetch failed: %s", exc)
            value = None
        if value:
            self._value, self._ts = value, time.time()
            self._write_disk()
        log.debug("visitor id: %s", "ok" if value else "none")
        return self._value if value else None
