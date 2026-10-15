"""Stream resolution: client cascade, validation, caching, per-video exclusion."""
from __future__ import annotations

import logging
import re
import threading
import time
from typing import Dict, List, Optional, Sequence

from .clients import DEFAULT_CASCADE, DEFAULT_CLIENTS, ClientConfig
from .download import DEFAULT_TEMPLATE, download_stream, render_output
from .errors import ExtractionError, StreamRefused, YTStreamError
from .http import Session
from .models import Stream, VideoInfo, parse_mime, select_stream
from .visitor import VisitorStore, default_cache_dir

log = logging.getLogger("ytstream")

PLAYER_URL = "https://www.youtube.com/youtubei/v1/player?prettyPrint=false"
_ID_RE = re.compile(r"[A-Za-z0-9_-]{11}")
_URL_ID_RE = re.compile(r"(?:[?&]v=|youtu\.be/|shorts/|embed/|live/)([A-Za-z0-9_-]{11})")


def extract_video_id(text: str) -> str:
    """Accept a bare 11-character id or any common YouTube / YouTube Music URL."""
    text = text.strip()
    if _ID_RE.fullmatch(text):
        return text
    m = _URL_ID_RE.search(text)
    if m:
        return m.group(1)
    raise YTStreamError(f"could not find a video id in {text!r}")


class _Skip(Exception):
    """A client was tried and rejected; the message is the reason."""


def _parse_streams(streaming_data: dict, cfg: ClientConfig):
    streams: List[Stream] = []
    ciphered = False
    headers = cfg.media_headers()
    for key, muxed in (("formats", True), ("adaptiveFormats", False)):
        for f in streaming_data.get(key) or []:
            url = f.get("url")
            if not url:
                if f.get("signatureCipher") or f.get("cipher"):
                    ciphered = True
                continue
            mime, codecs = parse_mime(f.get("mimeType", ""))
            kind = mime.split("/")[0]
            streams.append(Stream(
                itag=int(f.get("itag", 0)), url=url, mime_type=f.get("mimeType", mime), codecs=codecs,
                bitrate=int(f.get("bitrate") or f.get("averageBitrate") or 0),
                content_length=int(f.get("contentLength") or 0),
                has_audio=muxed or kind == "audio", has_video=muxed or kind == "video",
                client=cfg.name, headers=dict(headers),
                width=int(f.get("width") or 0), height=int(f.get("height") or 0), fps=int(f.get("fps") or 0),
                audio_sample_rate=int(f.get("audioSampleRate") or 0),
                audio_quality=f.get("audioQuality", ""), quality_label=f.get("qualityLabel", ""),
            ))
    return streams, ciphered


class YTStream:
    """Resolve and download YouTube streams. Thread-safe; create one and reuse it."""

    def __init__(self, *, proxy: Optional[str] = None, timeout: float = 15.0,
                 clients: Optional[Dict[str, ClientConfig]] = None,
                 cascade: Optional[Sequence[str]] = None,
                 cache_dir=None, cache_ttl: float = 5 * 3600,
                 ytdlp_fallback: bool = True, visitor_data: Optional[str] = None,
                 session: Optional[Session] = None):
        """
        proxy:          ``socks5h://127.0.0.1:9050`` (Tor), ``http://host:port`` ... or None.
        clients:        extra/overriding client definitions (see ``load_clients``).
        cascade:        client names to try, in order (default: built-in order).
        cache_dir:      where the visitor ID is cached; ``False`` disables the file cache.
        cache_ttl:      seconds an in-memory result is reused (also capped by URL expiry).
        ytdlp_fallback: use yt-dlp when every client fails, if it is installed.
        """
        self.proxy = proxy
        self.session = session or Session(proxy=proxy, timeout=timeout)
        self.clients: Dict[str, ClientConfig] = {**DEFAULT_CLIENTS, **(clients or {})}
        self.cascade = tuple(cascade) if cascade else tuple(n for n in DEFAULT_CASCADE if n in self.clients)
        self.cache_ttl = cache_ttl
        self.ytdlp_fallback = ytdlp_fallback
        vdir = None if cache_dir is False else (cache_dir or default_cache_dir())
        self._visitor = VisitorStore(vdir, proxy_key=proxy or "", fixed=visitor_data)
        self._cache: Dict[str, VideoInfo] = {}
        self._excluded: Dict[str, Dict[str, float]] = {}
        self._lock = threading.Lock()

    # ----- cache / exclusion -------------------------------------------------
    def _cache_get(self, vid: str) -> Optional[VideoInfo]:
        with self._lock:
            info = self._cache.get(vid)
            if info is None:
                return None
            deadline = info.fetched_at + self.cache_ttl
            if info.expires_at:
                deadline = min(deadline, info.expires_at - 600)
            if time.time() >= deadline:
                del self._cache[vid]
                return None
            return info

    def _cache_put(self, info: VideoInfo) -> None:
        with self._lock:
            self._cache[info.video_id] = info

    def _excluded_for(self, vid: str) -> set:
        with self._lock:
            entries = self._excluded.get(vid, {})
            now = time.time()
            for name in [n for n, until in entries.items() if until <= now]:
                del entries[name]
            return set(entries)

    def report_refused(self, video_id: str, client: str, ttl: float = 600.0) -> None:
        """Tell the resolver a client's URL was refused (e.g. a listener got HTTP 403).

        That client is skipped for this video for ``ttl`` seconds and the cached
        result is dropped, so the next ``extract`` produces a fresh URL.
        """
        vid = extract_video_id(video_id)
        with self._lock:
            self._excluded.setdefault(vid, {})[client] = time.time() + ttl
            cached = self._cache.get(vid)
            if cached is not None and cached.client == client:
                del self._cache[vid]
        log.debug("client %s excluded for %s for %ss", client, vid, ttl)

    def clear_cache(self) -> None:
        with self._lock:
            self._cache.clear()
            self._excluded.clear()

    # ----- extraction ----------------------------------------------------------
    def extract(self, video: str, *, validate: bool = True, max_kbps: Optional[int] = 160,
                clients: Optional[Sequence[str]] = None, use_cache: bool = True) -> VideoInfo:
        """Resolve ``video`` (id or URL) into a :class:`VideoInfo` with direct stream URLs.

        With ``validate`` the default audio stream is probed (last byte of the file),
        which rejects clients that only serve a short preview.
        """
        vid = extract_video_id(video)
        excluded = self._excluded_for(vid)
        if use_cache:
            cached = self._cache_get(vid)
            if cached is not None and cached.client not in excluded:
                return cached

        order = list(clients) if clients else list(self.cascade)
        reasons: Dict[str, str] = {}
        visitor = self._visitor.get(self.session)
        for attempt in (0, 1):
            for name in order:
                if name in excluded:
                    reasons[name] = "skipped (refused recently)"
                    continue
                cfg = self.clients.get(name)
                if cfg is None:
                    reasons[name] = "unknown client"
                    continue
                try:
                    info = self._try_client(vid, cfg, visitor, validate, max_kbps)
                except _Skip as skip:
                    reasons[name] = str(skip)
                    log.debug("%s: %s", name, skip)
                    continue
                except Exception as exc:  # network error, bad JSON, ...
                    reasons[name] = f"{type(exc).__name__}: {exc}"
                    log.debug("%s failed: %s", name, reasons[name])
                    continue
                log.debug("resolved %s via %s", vid, name)
                self._cache_put(info)
                return info
            if attempt == 0 and not self._visitor.fixed and any("LOGIN_REQUIRED" in r for r in reasons.values()):
                log.debug("bot check hit; fetching a fresh visitor id and retrying once")
                fresh = self._visitor.get(self.session, force=True)
                if not fresh or fresh == visitor:
                    break
                visitor = fresh
                reasons.clear()
                continue
            break

        if self.ytdlp_fallback and clients is None and "yt-dlp" not in excluded:
            try:
                info = self._ytdlp_extract(vid)
            except Exception as exc:
                reasons["yt-dlp"] = f"{type(exc).__name__}: {exc}"
            else:
                if info is not None:
                    self._cache_put(info)
                    return info
        raise ExtractionError(vid, reasons)

    def _try_client(self, vid: str, cfg: ClientConfig, visitor: Optional[str],
                    validate: bool, max_kbps: Optional[int]) -> VideoInfo:
        ctx = {**cfg.context, "hl": "en", "gl": "US"}
        if visitor:
            ctx["visitorData"] = visitor
        body = {"videoId": vid, "contentCheckOk": True, "racyCheckOk": True, "context": {"client": ctx}}
        headers = {
            "User-Agent": cfg.user_agent,
            "X-YouTube-Client-Name": cfg.client_id,
            "X-YouTube-Client-Version": cfg.version,
            **cfg.extra_headers,
        }
        if visitor:
            headers["X-Goog-Visitor-Id"] = visitor
        data = self.session.post_json(PLAYER_URL, body, headers)

        status = data.get("playabilityStatus") or {}
        if status.get("status") != "OK":
            raise _Skip(f"{status.get('status')}: {status.get('reason', '')}".strip().rstrip(":"))
        sdata = data.get("streamingData") or {}
        streams, ciphered = _parse_streams(sdata, cfg)
        if not streams:
            raise _Skip("only signatureCipher formats (not supported)" if ciphered
                        else "no direct stream URLs (SABR-only?)")

        det = data.get("videoDetails") or {}
        expires_in = int(sdata.get("expiresInSeconds") or 0)
        info = VideoInfo(
            video_id=vid, title=det.get("title", ""), author=det.get("author", ""),
            channel_id=det.get("channelId", ""), duration=int(det.get("lengthSeconds") or 0),
            view_count=int(det.get("viewCount") or 0), is_live=bool(det.get("isLiveContent")),
            description=det.get("shortDescription", ""),
            thumbnails=list((det.get("thumbnail") or {}).get("thumbnails") or []),
            streams=streams, client=cfg.name,
            loudness_db=((data.get("playerConfig") or {}).get("audioConfig") or {}).get("loudnessDb"),
            expires_at=(time.time() + expires_in) if expires_in else 0.0,
        )
        if validate:
            probe = select_stream(streams, "bestaudio", max_kbps) or streams[0]
            code = self._probe(probe)
            if code is not None and code not in (200, 206):
                raise _Skip(f"stream URL rejected (HTTP {code})")
            info.validated_itag = probe.itag
        return info

    def _probe(self, stream: Stream) -> Optional[int]:
        """Probe the LAST byte (preview-capped URLs fail there). None = network trouble."""
        n = stream.content_length
        rng = f"{n - 1}-{n - 1}" if n > 0 else "0-1"
        try:
            return self.session.probe(stream.url, stream.headers, rng)
        except Exception as exc:
            log.debug("probe error (%s); accepting optimistically", exc)
            return None

    def _ytdlp_extract(self, vid: str) -> Optional[VideoInfo]:
        try:
            import yt_dlp  # type: ignore
        except ImportError:
            log.debug("yt-dlp not installed; no fallback")
            return None
        opts = {"quiet": True, "no_warnings": True, "socket_timeout": 15, "skip_download": True}
        if self.proxy:
            opts["proxy"] = self.proxy
        with yt_dlp.YoutubeDL(opts) as ydl:
            data = ydl.extract_info(f"https://www.youtube.com/watch?v={vid}", download=False)
        streams: List[Stream] = []
        for f in data.get("formats") or []:
            url = f.get("url")
            if not url or str(f.get("protocol", "")).startswith(("m3u8", "http_dash_segments")):
                continue
            has_a = f.get("acodec") not in (None, "none")
            has_v = f.get("vcodec") not in (None, "none")
            if not (has_a or has_v):
                continue
            ext = f.get("ext") or "bin"
            codecs = ", ".join(c for c in (f.get("vcodec") if has_v else None,
                                           f.get("acodec") if has_a else None) if c)
            fid = str(f.get("format_id", ""))
            streams.append(Stream(
                itag=int(fid) if fid.isdigit() else 0, url=url,
                mime_type=f'{"video" if has_v else "audio"}/{"mp4" if ext == "m4a" else ext}; codecs="{codecs}"',
                codecs=codecs, bitrate=int((f.get("abr") or f.get("tbr") or 0) * 1000),
                content_length=int(f.get("filesize") or f.get("filesize_approx") or 0),
                has_audio=has_a, has_video=has_v, client="yt-dlp",
                headers=dict(f.get("http_headers") or {}),
                width=int(f.get("width") or 0), height=int(f.get("height") or 0), fps=int(f.get("fps") or 0),
            ))
        if not streams:
            return None
        return VideoInfo(
            video_id=vid, title=data.get("title") or "", author=data.get("uploader") or "",
            channel_id=data.get("channel_id") or "", duration=int(data.get("duration") or 0),
            view_count=int(data.get("view_count") or 0), is_live=bool(data.get("is_live")),
            description=data.get("description") or "", thumbnails=list(data.get("thumbnails") or []),
            streams=streams, client="yt-dlp",
        )

    # ----- convenience ---------------------------------------------------------
    def get_stream(self, video: str, selector: str = "bestaudio", *, max_kbps: Optional[int] = None,
                   **extract_kwargs) -> Stream:
        """Resolve and pick one stream (default: best audio). Raises if none matches."""
        info = self.extract(video, max_kbps=max_kbps or 160, **extract_kwargs)
        stream = info.select(selector, max_kbps)
        if stream is None:
            have = ", ".join(f"{s.itag}({s.ext})" for s in info.streams) or "none"
            raise YTStreamError(f"format {selector!r} not available (have: {have})")
        return stream

    def download(self, video: str, output: str = DEFAULT_TEMPLATE, selector: str = "bestaudio", *,
                 max_kbps: Optional[int] = None, resume: bool = True, retries: int = 3,
                 progress=None, validate: bool = True, failovers: int = 3,
                 chunk_size: Optional[int] = None):
        """Download a stream to ``output`` (a path or a template such as
        ``"%(title)s [%(id)s].%(ext)s"``). If a URL is refused, the client is
        excluded and another one is tried, up to ``failovers`` times."""
        vid = extract_video_id(video)
        last: Optional[Exception] = None
        prev = None
        for _ in range(failovers + 1):
            info = self.extract(vid, validate=validate, max_kbps=max_kbps or 160)
            stream = info.select(selector, max_kbps)
            if stream is None:
                have = ", ".join(f"{s.itag}({s.ext})" for s in info.streams) or "none"
                raise YTStreamError(f"format {selector!r} not available (have: {have})")
            dest = render_output(output, info, stream)
            ident = (stream.itag, stream.content_length)
            if prev is not None and ident != prev:  # different file now: old partial data is useless
                part = dest.with_name(dest.name + ".part")
                if part.exists():
                    part.unlink()
            prev = ident
            cfg = self.clients.get(stream.client)
            try:
                return download_stream(self.session, stream, dest,
                                       chunk_size=chunk_size or (cfg.range_bytes if cfg else None),
                                       resume=resume, retries=retries, progress=progress)
            except StreamRefused as exc:
                log.debug("download refused via %s (%s); trying another client", stream.client, exc)
                self.report_refused(vid, stream.client)
                last = exc
        raise last if last else YTStreamError("download failed")

    # ----- async wrappers (for Quart / FastAPI / asyncio code) --------------------
    async def aextract(self, *args, **kwargs) -> VideoInfo:
        import asyncio
        return await asyncio.to_thread(self.extract, *args, **kwargs)

    async def aget_stream(self, *args, **kwargs) -> Stream:
        import asyncio
        return await asyncio.to_thread(self.get_stream, *args, **kwargs)

    async def adownload(self, *args, **kwargs):
        import asyncio
        return await asyncio.to_thread(self.download, *args, **kwargs)
