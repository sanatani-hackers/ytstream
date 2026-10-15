"""Ranged downloader with resume, retries and output templates."""
from __future__ import annotations

import http.client
import os
import re
import socket
import time
import urllib.error
from pathlib import Path
from typing import Callable, Optional

from .errors import DownloadError, StreamRefused, YTStreamError
from .http import Session
from .models import Stream, VideoInfo

DEFAULT_TEMPLATE = "%(title)s [%(id)s].%(ext)s"
DEFAULT_CHUNK = 1024 * 1024

ProgressFn = Callable[[int, Optional[int]], None]


def _clean(value) -> str:
    text = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", str(value)).strip(" .")
    return text[:150] or "_"


def render_output(template: str, info: VideoInfo, stream: Stream) -> Path:
    """Fill ``%(id)s %(title)s %(author)s %(ext)s %(itag)s %(kbps)s %(duration)s %(client)s``.

    A directory (existing, or ending in ``/``) gets the default template appended.
    A string without ``%(...)`` is used as a literal path.
    """
    fields = {k: _clean(v) for k, v in {
        "id": info.video_id, "title": info.title or info.video_id, "author": info.author or "unknown",
        "ext": stream.ext, "itag": stream.itag, "kbps": stream.kbps,
        "duration": info.duration, "client": stream.client,
    }.items()}
    if template.endswith(("/", os.sep)) or os.path.isdir(template):
        template = os.path.join(template, DEFAULT_TEMPLATE)
    try:
        return Path(template % fields)
    except KeyError as exc:
        raise YTStreamError(f"unknown output field {exc} (available: {', '.join(fields)})") from None
    except (ValueError, TypeError):
        return Path(template)


def _fetch_range(session: Session, stream: Stream, start: int, end: int, retries: int,
                 timeout: Optional[float]):
    """Return (data, total_size_or_None) for bytes start..end, retrying transient errors."""
    headers = {**stream.headers, "Range": f"bytes={start}-{end}"}
    attempt = 0
    while True:
        try:
            with session.open(stream.url, headers=headers, timeout=timeout) as resp:
                status = resp.status
                data = resp.read()
                content_range = resp.headers.get("Content-Range")
            if status == 200 and start > 0:
                raise DownloadError("server ignored the Range header")
            total = None
            if content_range and "/" in content_range:
                tail = content_range.rsplit("/", 1)[1]
                total = int(tail) if tail.isdigit() else None
            elif status == 200:
                total = len(data)  # whole body came back
            return data, total
        except urllib.error.HTTPError as exc:
            if exc.code == 416:  # range past the end: nothing more to read
                return b"", None
            if exc.code in (400, 401, 403, 404, 410):
                raise StreamRefused(f"HTTP {exc.code}", status=exc.code, client=stream.client) from exc
            err: Exception = exc
        except (urllib.error.URLError, OSError, http.client.HTTPException, socket.timeout) as exc:
            err = exc
        attempt += 1
        if attempt > retries:
            raise DownloadError(f"giving up at byte {start}: {err}") from err
        time.sleep(min(0.5 * 2 ** attempt, 8.0))


def download_stream(session: Session, stream: Stream, dest, *, chunk_size: Optional[int] = None,
                    resume: bool = True, retries: int = 3, progress: Optional[ProgressFn] = None,
                    timeout: Optional[float] = None) -> Path:
    """Download ``stream`` to ``dest`` via ``dest.part`` (renamed when complete)."""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    chunk = chunk_size or DEFAULT_CHUNK
    total = stream.content_length or None

    pos = part.stat().st_size if (resume and part.exists()) else 0
    if total is not None and pos > total:  # leftovers from something else
        pos = 0
    with open(part, "ab" if pos else "wb") as fh:
        if pos and total is not None and pos == total:
            pass  # already complete
        while total is None or pos < total:
            end = pos + chunk - 1
            if total is not None:
                end = min(end, total - 1)
            asked = end - pos + 1
            data, seen_total = _fetch_range(session, stream, pos, end, retries, timeout)
            if seen_total is not None:
                total = seen_total
            if not data:
                break
            fh.write(data)
            pos += len(data)
            if progress:
                progress(pos, total)
            if total is None and len(data) < asked:
                break  # short read with unknown size: end of file
    if total is not None and pos != total:
        raise DownloadError(f"incomplete download: got {pos} of {total} bytes")
    os.replace(part, dest)
    return dest
