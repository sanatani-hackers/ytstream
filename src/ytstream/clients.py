"""InnerTube client definitions.

Clients are plain data so they can be overridden or extended without touching
code (see :func:`load_clients`). YouTube changes what each client needs every
few weeks; when one stops working, updating its entry is usually the whole fix.

The default values are the ones yt-dlp and other open-source players publish.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Dict, Tuple

_MIB = 1024 * 1024


@dataclass(eq=False)
class ClientConfig:
    """One InnerTube client identity."""

    name: str
    client_id: str  # sent as X-YouTube-Client-Name
    user_agent: str
    context: Dict[str, object]  # the ``context.client`` object (must hold clientName/clientVersion)
    extra_headers: Dict[str, str] = field(default_factory=dict)
    range_bytes: int = _MIB  # preferred size of one ranged media request

    @property
    def version(self) -> str:
        return str(self.context.get("clientVersion", ""))

    def media_headers(self) -> Dict[str, str]:
        """Headers a media (googlevideo) request for a URL from this client should carry."""
        headers = {"User-Agent": self.user_agent}
        for key in ("Origin", "Referer"):
            if key in self.extra_headers:
                headers[key] = self.extra_headers[key]
        return headers

    @classmethod
    def from_dict(cls, name: str, data: dict) -> "ClientConfig":
        try:
            context = dict(data["context"])
            cfg = cls(
                name=name,
                client_id=str(data["client_id"]),
                user_agent=str(data["user_agent"]),
                context=context,
                extra_headers=dict(data.get("extra_headers", {})),
                range_bytes=int(data.get("range_bytes", _MIB)),
            )
        except KeyError as exc:
            raise ValueError(f"client {name!r} is missing required field {exc}") from None
        if "clientName" not in context or "clientVersion" not in context:
            raise ValueError(f"client {name!r}: context needs clientName and clientVersion")
        return cfg


_SAFARI_VISION_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 15_7_3) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/26.0 Safari/605.1.15"
)
_CHROME_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
)


def _builtin() -> Dict[str, ClientConfig]:
    clients = [
        # yt-dlp's default JS-less client; serves whole files.
        ClientConfig(
            "VISIONOS", "101", _SAFARI_VISION_UA,
            {
                "clientName": "VISIONOS", "clientVersion": "1.02",
                "deviceMake": "Apple", "deviceModel": "RealityDevice17,1",
                "userAgent": _SAFARI_VISION_UA,
                "osName": "visionOS", "osVersion": "26.5.23O471",
            },
        ),
        ClientConfig(
            "ANDROID_VR", "28",
            "com.google.android.apps.youtube.vr.oculus/1.65.10 "
            "(Linux; U; Android 12L; eureka-user Build/SQ3A.220605.009.A1) gzip",
            {
                "clientName": "ANDROID_VR", "clientVersion": "1.65.10",
                "deviceMake": "Oculus", "deviceModel": "Quest 3",
                "osName": "Android", "osVersion": "12L", "androidSdkVersion": 32,
            },
            range_bytes=512 * 1024,
        ),
        ClientConfig(
            "TVHTML5", "7",
            "Mozilla/5.0(SMART-TV; Linux; Tizen 4.0.0.2) AppleWebkit/605.1.15 "
            "(KHTML, like Gecko) SamsungBrowser/9.2 TV Safari/605.1.15",
            {"clientName": "TVHTML5", "clientVersion": "7.20260707.07.00"},
        ),
        ClientConfig(
            "WEB_REMIX", "67", _CHROME_UA,
            {"clientName": "WEB_REMIX", "clientVersion": "1.20260707.12.00"},
            extra_headers={"Origin": "https://music.youtube.com", "Referer": "https://music.youtube.com/"},
        ),
        # Last resort: reportedly serves only a ~1 MiB preview of each file.
        ClientConfig(
            "IOS", "5",
            "com.google.ios.youtube/21.26.4 (iPhone16,2; U; CPU iOS 18_3_2 like Mac OS X;)",
            {
                "clientName": "IOS", "clientVersion": "21.26.4",
                "deviceMake": "Apple", "deviceModel": "iPhone16,2",
                "osName": "iPhone", "osVersion": "18.3.2.22D82",
            },
        ),
    ]
    return {c.name: c for c in clients}


DEFAULT_CLIENTS: Dict[str, ClientConfig] = _builtin()
DEFAULT_CASCADE: Tuple[str, ...] = ("VISIONOS", "ANDROID_VR", "TVHTML5", "WEB_REMIX", "IOS")

# Used only to obtain a visitor ID.
WEB_VISITOR_CONTEXT = {"clientName": "WEB", "clientVersion": "2.20260708.00.00", "hl": "en", "gl": "US"}
WEB_USER_AGENT = _CHROME_UA


def load_clients(path: str) -> Dict[str, ClientConfig]:
    """Load client definitions from a JSON file.

    Format::

        {"MYCLIENT": {"client_id": "28", "user_agent": "...",
                      "context": {"clientName": "...", "clientVersion": "..."},
                      "extra_headers": {}, "range_bytes": 1048576}}

    Entries with a built-in name replace the built-in definition.
    """
    with open(path, "r", encoding="utf-8") as fh:
        raw = json.load(fh)
    if not isinstance(raw, dict):
        raise ValueError("client config must be a JSON object mapping names to definitions")
    return {name: ClientConfig.from_dict(name, data) for name, data in raw.items()}
