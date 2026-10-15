"""ytstream - resolve and download YouTube audio streams via InnerTube clients.

Quick start::

    from ytstream import YTStream
    yt = YTStream()
    stream = yt.get_stream("https://youtu.be/dQw4w9WgXcQ")   # best audio
    print(stream.url, stream.headers)
"""
__version__ = "0.1.0"

from .clients import DEFAULT_CASCADE, DEFAULT_CLIENTS, ClientConfig, load_clients  # noqa: E402
from .errors import DownloadError, ExtractionError, StreamRefused, YTStreamError  # noqa: E402
from .models import Stream, VideoInfo, select_stream  # noqa: E402
from .resolver import YTStream, extract_video_id  # noqa: E402

_default = None


def resolve(video: str, selector: str = "bestaudio", **kwargs) -> Stream:
    """One-liner using a shared default :class:`YTStream`."""
    global _default
    if _default is None:
        _default = YTStream()
    return _default.get_stream(video, selector, **kwargs)


__all__ = [
    "YTStream", "Stream", "VideoInfo", "ClientConfig", "DEFAULT_CLIENTS", "DEFAULT_CASCADE",
    "load_clients", "select_stream", "extract_video_id", "resolve",
    "YTStreamError", "ExtractionError", "StreamRefused", "DownloadError", "__version__",
]
