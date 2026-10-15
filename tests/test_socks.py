import unittest

from helpers import RangeServer, Socks5Server
from ytstream import YTStreamError
from ytstream.http import Session


class SocksProxy(unittest.TestCase):
    def test_request_goes_through_proxy_with_remote_dns(self):
        with RangeServer(b"hello world") as srv, Socks5Server() as proxy:
            session = Session(proxy=f"socks5h://127.0.0.1:{proxy.port}", timeout=5)
            with session.open(f"http://localhost:{srv.port}/file", headers={"Range": "bytes=0-4"}) as r:
                self.assertEqual(r.status, 206)
                self.assertEqual(r.read(), b"hello")
            self.assertEqual(proxy.hosts, ["localhost"])    # the NAME reached the proxy, unresolved

    def test_probe_through_proxy(self):
        with RangeServer(b"hello world") as srv, Socks5Server() as proxy:
            session = Session(proxy=f"socks5h://127.0.0.1:{proxy.port}", timeout=5)
            self.assertEqual(session.probe(f"http://localhost:{srv.port}/file", {}, "0-1"), 206)
            self.assertEqual(session.probe(f"http://localhost:{srv.port}/denied", {}, "0-1"), 403)

    def test_dead_proxy_is_an_error(self):
        session = Session(proxy="socks5h://127.0.0.1:1", timeout=2)
        with self.assertRaises(OSError):
            session.open("http://localhost:9/x")

    def test_bad_scheme(self):
        with self.assertRaises(YTStreamError):
            Session(proxy="ftp://x:1")


if __name__ == "__main__":
    unittest.main()


import os
import shutil
import ssl
import subprocess
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest import mock


class _TlsHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_GET(self):
        body = b"secure hello"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@unittest.skipUnless(shutil.which("openssl"), "openssl CLI needed to make a test certificate")
class SocksProxyTls(unittest.TestCase):
    """HTTPS through the SOCKS5 proxy: the path real YouTube requests (e.g. via Tor) take."""

    def test_https_over_socks(self):
        with tempfile.TemporaryDirectory() as d:
            key, crt = os.path.join(d, "k.pem"), os.path.join(d, "c.pem")
            subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", key,
                            "-out", crt, "-subj", "/CN=localhost", "-days", "1",
                            "-addext", "subjectAltName=DNS:localhost"],
                           check=True, capture_output=True)
            server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            server_ctx.load_cert_chain(crt, key)
            httpd = HTTPServer(("127.0.0.1", 0), _TlsHandler)
            httpd.socket = server_ctx.wrap_socket(httpd.socket, server_side=True)
            threading.Thread(target=lambda: httpd.serve_forever(poll_interval=0.01), daemon=True).start()

            real = ssl.create_default_context

            def trusting(*a, **k):
                ctx = real(*a, **k)
                ctx.load_verify_locations(crt)   # trust only our throwaway cert
                return ctx

            try:
                with Socks5Server() as proxy, mock.patch("ytstream.http.ssl.create_default_context", trusting):
                    session = Session(proxy=f"socks5h://127.0.0.1:{proxy.port}", timeout=5)
                    with session.open(f"https://localhost:{httpd.server_address[1]}/x") as r:
                        self.assertEqual(r.read(), b"secure hello")
                    self.assertEqual(proxy.hosts, ["localhost"])
            finally:
                httpd.shutdown()
                httpd.server_close()
