"""Exception types raised by ytstream."""
from __future__ import annotations

from typing import Dict, Optional


class YTStreamError(Exception):
    """Base class for all ytstream errors."""


class ExtractionError(YTStreamError):
    """No client could produce a usable stream URL.

    ``reasons`` maps each client name to why it was skipped.
    """

    def __init__(self, video_id: str, reasons: Dict[str, str]):
        self.video_id = video_id
        self.reasons = dict(reasons)
        detail = "; ".join(f"{k}: {v}" for k, v in self.reasons.items()) or "no clients tried"
        super().__init__(f"could not resolve a stream for {video_id!r} ({detail})")


class StreamRefused(YTStreamError):
    """googlevideo refused a stream URL (HTTP 403/410/...)."""

    def __init__(self, message: str, status: Optional[int] = None, client: Optional[str] = None):
        self.status = status
        self.client = client
        super().__init__(message)


class DownloadError(YTStreamError):
    """A download failed for a reason other than the URL being refused."""
