"""Data models and format selection."""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

_EXT_BY_MIME = {
    "audio/mp4": "m4a", "video/mp4": "mp4",
    "audio/webm": "webm", "video/webm": "webm",
    "audio/3gpp": "3gp", "video/3gpp": "3gp",
}


@dataclass
class Stream:
    """One downloadable format with a direct URL."""

    itag: int
    url: str
    mime_type: str
    codecs: str
    bitrate: int  # bits per second
    content_length: int  # bytes, 0 if unknown
    has_audio: bool
    has_video: bool
    client: str
    headers: Dict[str, str] = field(default_factory=dict)  # send these with media requests
    width: int = 0
    height: int = 0
    fps: int = 0
    audio_sample_rate: int = 0
    audio_quality: str = ""
    quality_label: str = ""

    @property
    def ext(self) -> str:
        base = self.mime_type.split(";")[0].strip().lower()
        return _EXT_BY_MIME.get(base, base.split("/")[-1] or "bin")

    @property
    def kbps(self) -> int:
        return self.bitrate // 1000

    @property
    def audio_only(self) -> bool:
        return self.has_audio and not self.has_video

    @property
    def video_only(self) -> bool:
        return self.has_video and not self.has_audio

    @property
    def muxed(self) -> bool:
        return self.has_audio and self.has_video

    def to_dict(self, include_url: bool = True) -> Dict[str, Any]:
        d = {
            "itag": self.itag, "ext": self.ext, "mime_type": self.mime_type, "codecs": self.codecs,
            "bitrate": self.bitrate, "kbps": self.kbps, "content_length": self.content_length,
            "has_audio": self.has_audio, "has_video": self.has_video,
            "width": self.width, "height": self.height, "fps": self.fps,
            "audio_sample_rate": self.audio_sample_rate, "audio_quality": self.audio_quality,
            "quality_label": self.quality_label, "client": self.client, "headers": dict(self.headers),
        }
        if include_url:
            d["url"] = self.url
        return d


@dataclass
class VideoInfo:
    """Metadata plus every stream a client returned."""

    video_id: str
    title: str = ""
    author: str = ""
    channel_id: str = ""
    duration: int = 0  # seconds
    view_count: int = 0
    is_live: bool = False
    description: str = ""
    thumbnails: List[Dict[str, Any]] = field(default_factory=list)
    streams: List[Stream] = field(default_factory=list)
    client: str = ""  # which client produced ``streams``
    loudness_db: Optional[float] = None
    fetched_at: float = field(default_factory=time.time)
    expires_at: float = 0.0  # epoch seconds; 0 if unknown
    validated_itag: Optional[int] = None  # the stream whose URL was probed, if any

    @property
    def audio_streams(self) -> List[Stream]:
        return [s for s in self.streams if s.audio_only]

    def select(self, selector: str = "bestaudio", max_kbps: Optional[int] = None) -> Optional[Stream]:
        return select_stream(self.streams, selector, max_kbps)

    def to_dict(self, include_urls: bool = True) -> Dict[str, Any]:
        return {
            "id": self.video_id, "title": self.title, "author": self.author,
            "channel_id": self.channel_id, "duration": self.duration, "view_count": self.view_count,
            "is_live": self.is_live, "description": self.description, "thumbnails": self.thumbnails,
            "client": self.client, "loudness_db": self.loudness_db,
            "fetched_at": self.fetched_at, "expires_at": self.expires_at,
            "validated_itag": self.validated_itag,
            "streams": [s.to_dict(include_urls) for s in self.streams],
        }


def parse_mime(mime: str):
    """``'audio/mp4; codecs="mp4a.40.2"'`` -> ``('audio/mp4', 'mp4a.40.2')``."""
    base = mime.split(";")[0].strip()
    m = re.search(r'codecs="([^"]*)"', mime)
    return base, (m.group(1) if m else "")


def _pick_audio(cands: List[Stream], max_kbps: Optional[int]) -> Optional[Stream]:
    if not cands:
        return None
    if max_kbps:
        within = [s for s in cands if s.bitrate <= max_kbps * 1000]
        if within:
            return max(within, key=lambda s: s.bitrate)
        return min(cands, key=lambda s: s.bitrate)  # nothing under the cap: take the smallest
    return max(cands, key=lambda s: s.bitrate)


def _select_one(streams: List[Stream], sel: str, max_kbps: Optional[int]) -> Optional[Stream]:
    sel = sel.strip().lower()
    audio = [s for s in streams if s.audio_only]
    if sel.isdigit():
        return next((s for s in streams if s.itag == int(sel)), None)
    if sel in ("bestaudio", "ba"):
        return _pick_audio(audio, max_kbps)
    if sel in ("worstaudio", "wa"):
        return min(audio, key=lambda s: s.bitrate) if audio else None
    if sel in ("m4a", "mp4a"):
        return _pick_audio([s for s in audio if s.ext == "m4a"], max_kbps)
    if sel in ("webm", "opus"):
        return _pick_audio([s for s in audio if s.ext == "webm"], max_kbps)
    muxed = [s for s in streams if s.muxed]
    video = [s for s in streams if s.video_only]
    key = lambda s: (s.height, s.bitrate)  # noqa: E731
    if sel in ("best", "b"):
        return max(muxed, key=key) if muxed else None
    if sel in ("worst", "w"):
        return min(muxed, key=key) if muxed else None
    if sel in ("bestvideo", "bv"):
        return max(video, key=key) if video else None
    if sel in ("worstvideo", "wv"):
        return min(video, key=key) if video else None
    return None


def select_stream(streams: List[Stream], selector: str = "bestaudio",
                  max_kbps: Optional[int] = None) -> Optional[Stream]:
    """Pick a stream. ``selector`` is an itag (``140``), ``bestaudio``, ``worstaudio``,
    ``m4a``, ``webm``, ``best`` (best muxed), ``bestvideo``..., or ``a/b/c`` fallbacks."""
    for part in selector.split("/"):
        found = _select_one(streams, part, max_kbps)
        if found is not None:
            return found
    return None
