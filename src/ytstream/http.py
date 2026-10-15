"""Tiny HTTP layer on urllib, with HTTP and SOCKS5 proxy support (no dependencies)."""
from __future__ import annotations

import functools
import http.client
import json
import socket
import ssl
import struct
import urllib.error
import urllib.request
from typing import Dict, Optional, Tuple
from urllib.parse import unquote, urlparse

from .errors import YTStreamError


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise OSError("SOCKS5 proxy closed the connection")
        buf += chunk
    return buf


def socks5_connect(proxy_host: str, proxy_port: int, host: str, port: int, timeout: float,
                   username: Optional[str] = None, password: Optional[str] = None) -> socket.socket:
    """Open a TCP connection to host:port through a SOCKS5 proxy.

    The destination name is sent to the proxy unresolved (so DNS happens on the
    proxy side, as ``socks5h`` means; this is what you want with Tor).
    """
    sock = socket.create_connection((proxy_host, proxy_port), timeout)
    try:
        methods = b"\x00\x02" if username is not None else b"\x00"
        sock.sendall(b"\x05" + bytes([len(methods)]) + methods)
        ver, method = _recv_exact(sock, 2)
        if ver != 5 or method == 0xFF:
            raise OSError("SOCKS5 proxy refused our authentication methods")
        if method == 2:
            if username is None:
                raise OSError("SOCKS5 proxy requires a username/password")
            u, p = username.encode(), (password or "").encode()
            sock.sendall(b"\x01" + bytes([len(u)]) + u + bytes([len(p)]) + p)
            _, status = _recv_exact(sock, 2)
            if status != 0:
                raise OSError("SOCKS5 authentication failed")
        elif method != 0:
            raise OSError(f"SOCKS5 proxy chose unsupported method {method}")
        name = host.encode("idna")
        sock.sendall(b"\x05\x01\x00\x03" + bytes([len(name)]) + name + struct.pack(">H", port))
        _, rep, _, atyp = _recv_exact(sock, 4)
        if rep != 0:
            raise OSError(f"SOCKS5 connect to {host}:{port} failed (reply code {rep})")
        if atyp == 1:
            _recv_exact(sock, 4 + 2)
        elif atyp == 3:
            _recv_exact(sock, _recv_exact(sock, 1)[0] + 2)
        elif atyp == 4:
            _recv_exact(sock, 16 + 2)
        sock.settimeout(timeout)
        return sock
    except BaseException:
        sock.close()
        raise


def _timeout_value(timeout) -> float:
    return float(timeout) if isinstance(timeout, (int, float)) else 30.0


class _SocksHTTPConnection(http.client.HTTPConnection):
    def __init__(self, *args, socks_proxy, **kwargs):
        super().__init__(*args, **kwargs)
        self._socks = socks_proxy

    def connect(self):
        ph, pp, user, pw = self._socks
        self.sock = socks5_connect(ph, pp, self.host, self.port, _timeout_value(self.timeout), user, pw)


class _SocksHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, *args, socks_proxy, ssl_context, **kwargs):
        super().__init__(*args, context=ssl_context, **kwargs)
        self._socks = socks_proxy
        self._ssl_ctx = ssl_context

    def connect(self):
        ph, pp, user, pw = self._socks
        raw = socks5_connect(ph, pp, self.host, self.port, _timeout_value(self.timeout), user, pw)
        self.sock = self._ssl_ctx.wrap_socket(raw, server_hostname=self.host)


class _SocksHandler(urllib.request.HTTPHandler, urllib.request.HTTPSHandler):
    def __init__(self, socks_proxy: Tuple[str, int, Optional[str], Optional[str]]):
        self._ctx = ssl.create_default_context()
        urllib.request.HTTPHandler.__init__(self)
        urllib.request.HTTPSHandler.__init__(self, context=self._ctx)
        self._socks = socks_proxy

    def http_open(self, req):
        return self.do_open(functools.partial(_SocksHTTPConnection, socks_proxy=self._socks), req)

    def https_open(self, req):
        return self.do_open(
            functools.partial(_SocksHTTPSConnection, socks_proxy=self._socks, ssl_context=self._ctx), req
        )


def _build_opener(proxy: Optional[str]) -> urllib.request.OpenerDirector:
    if not proxy:
        return urllib.request.build_opener()
    u = urlparse(proxy)
    if u.scheme in ("socks5", "socks5h"):
        if not u.hostname:
            raise YTStreamError(f"bad proxy URL: {proxy!r}")
        creds = (unquote(u.username) if u.username else None, unquote(u.password) if u.password else None)
        return urllib.request.build_opener(_SocksHandler((u.hostname, u.port or 1080, creds[0], creds[1])))
    if u.scheme in ("http", "https"):
        return urllib.request.build_opener(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    raise YTStreamError(f"unsupported proxy scheme {u.scheme!r}; use socks5h://, http:// or https://")


class Session:
    """Holds the proxy settings and does the actual requests."""

    def __init__(self, proxy: Optional[str] = None, timeout: float = 15.0):
        self.proxy = proxy
        self.timeout = timeout
        self._opener = _build_opener(proxy)

    def open(self, url: str, *, data: Optional[bytes] = None, headers: Optional[Dict[str, str]] = None,
             method: Optional[str] = None, timeout: Optional[float] = None):
        req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
        return self._opener.open(req, timeout=timeout or self.timeout)

    def post_json(self, url: str, body: dict, headers: Dict[str, str], timeout: Optional[float] = None) -> dict:
        hdrs = {"Content-Type": "application/json", **headers}
        with self.open(url, data=json.dumps(body).encode(), headers=hdrs, method="POST", timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))

    def probe(self, url: str, headers: Dict[str, str], byte_range: str, timeout: Optional[float] = None) -> int:
        """GET one tiny byte range and return the final HTTP status (redirects followed)."""
        try:
            with self.open(url, headers={**headers, "Range": f"bytes={byte_range}"}, timeout=timeout) as r:
                return r.status
        except urllib.error.HTTPError as exc:
            return exc.code
