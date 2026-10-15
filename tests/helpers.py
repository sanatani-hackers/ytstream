"""Test helpers: a scriptable fake InnerTube session, a Range-capable HTTP server, a SOCKS5 server."""
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from ytstream.http import Session


def player_ok(base_url, *, title="Song", itags=((140, "audio/mp4; codecs=\"mp4a.40.2\"", 129000, 1000),),
              extra_formats=None):
    """Build a /player response whose stream URLs point at ``base_url``."""
    adaptive = []
    for itag, mime, bitrate, length in itags:
        adaptive.append({"itag": itag, "url": f"{base_url}?itag={itag}", "mimeType": mime,
                         "bitrate": bitrate, "contentLength": str(length)})
    sd = {"adaptiveFormats": adaptive, "expiresInSeconds": "21540"}
    if extra_formats:
        sd["formats"] = extra_formats
    return {
        "playabilityStatus": {"status": "OK"},
        "streamingData": sd,
        "videoDetails": {"title": title, "author": "Artist", "lengthSeconds": "100", "viewCount": "5",
                         "channelId": "UC1", "shortDescription": "d",
                         "thumbnail": {"thumbnails": [{"url": "http://x/t.jpg", "width": 1, "height": 1}]}},
        "playerConfig": {"audioConfig": {"loudnessDb": -1.5}},
    }


def player_denied(status="LOGIN_REQUIRED", reason="Sign in to confirm you're not a bot"):
    return {"playabilityStatus": {"status": status, "reason": reason}}


class FakeSession(Session):
    """Scripted InnerTube responses; real HTTP (for media URLs) still goes through ``open``."""

    def __init__(self, player, probes=None, visitors=("VISITOR1",)):
        super().__init__()
        self.player = player          # client name -> response dict or callable(body)
        self.probes = probes or {}    # url -> status (default 206)
        self.visitors = list(visitors)
        self.player_calls = []
        self.probe_calls = []
        self.visitor_calls = 0

    def post_json(self, url, body, headers, timeout=None):
        if "visitor_id" in url:
            self.visitor_calls += 1
            vid = self.visitors[min(self.visitor_calls - 1, len(self.visitors) - 1)]
            return {"responseContext": {"visitorData": vid}}
        name = body["context"]["client"]["clientName"]
        self.player_calls.append((name, body, headers))
        resp = self.player[name]
        return resp(body) if callable(resp) else resp

    def probe(self, url, headers, byte_range, timeout=None):
        self.probe_calls.append((url, byte_range))
        return self.probes.get(url, 206)


class _RangeHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def do_GET(self):
        srv = self.server
        rng = self.headers.get("Range")
        srv.requests.append((self.path, rng))
        if self.path.startswith("/redir"):
            self.send_response(302)
            self.send_header("Location", "/file" + self.path[len("/redir"):])
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if self.path.startswith("/denied"):
            self.send_response(403)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        data = srv.data
        start, end = 0, len(data) - 1
        if rng and rng.startswith("bytes="):
            a, _, b = rng[6:].partition("-")
            start = int(a)
            end = min(int(b), len(data) - 1) if b else len(data) - 1
        if self.path.startswith("/cap") and start >= srv.cap:
            self.send_response(403)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if start >= len(data):
            self.send_response(416)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        body = data[start:end + 1]
        if rng:
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{start + len(body) - 1}/{len(data)}")
        else:
            self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class RangeServer:
    """Local HTTP server: /file (ranges), /redir (302 -> /file), /denied (403), /cap (403 past cap bytes)."""

    def __init__(self, data: bytes, cap: int = 0):
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _RangeHandler)
        self.httpd.data = data
        self.httpd.cap = cap
        self.httpd.requests = []
        self.thread = threading.Thread(target=lambda: self.httpd.serve_forever(poll_interval=0.01), daemon=True)

    @property
    def requests(self):
        return self.httpd.requests

    @property
    def port(self):
        return self.httpd.server_address[1]

    def url(self, path="/file"):
        return f"http://127.0.0.1:{self.port}{path}"

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()


class Socks5Server:
    """Minimal SOCKS5 proxy (no auth, domain or IPv4 targets) that records requested hosts."""

    def __init__(self):
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(5)
        self.hosts = []
        self.thread = threading.Thread(target=self._serve, daemon=True)

    @property
    def port(self):
        return self.sock.getsockname()[1]

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.sock.close()

    def _serve(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    @staticmethod
    def _exact(conn, n):
        buf = b""
        while len(buf) < n:
            chunk = conn.recv(n - len(buf))
            if not chunk:
                raise OSError("closed")
            buf += chunk
        return buf

    def _handle(self, conn):
        upstream = None
        try:
            _, n = self._exact(conn, 2)
            self._exact(conn, n)
            conn.sendall(b"\x05\x00")
            _, _, _, atyp = self._exact(conn, 4)
            if atyp == 3:
                host = self._exact(conn, self._exact(conn, 1)[0]).decode()
            else:
                host = socket.inet_ntoa(self._exact(conn, 4))
            port = int.from_bytes(self._exact(conn, 2), "big")
            self.hosts.append(host)
            upstream = socket.create_connection(("127.0.0.1" if host == "localhost" else host, port), 5)
            conn.sendall(b"\x05\x00\x00\x01\x00\x00\x00\x00\x00\x00")

            def pump(src, dst):
                try:
                    while True:
                        data = src.recv(65536)
                        if not data:
                            break
                        dst.sendall(data)
                except OSError:
                    pass
                finally:
                    try:
                        dst.shutdown(socket.SHUT_WR)
                    except OSError:
                        pass

            t = threading.Thread(target=pump, args=(upstream, conn), daemon=True)
            t.start()
            pump(conn, upstream)
            t.join(5)
        except OSError:
            pass
        finally:
            conn.close()
            if upstream is not None:
                upstream.close()
