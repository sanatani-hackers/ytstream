import os
import tempfile
import unittest

from helpers import FakeSession, RangeServer, player_ok
from ytstream import StreamRefused, YTStream
from ytstream.download import download_stream
from ytstream.models import Stream

DATA = bytes(range(256)) * 4096          # 1 MiB


def read(path):
    with open(path, "rb") as fh:
        return fh.read()
VID = "dQw4w9WgXcQ"


def stream_for(url, length=len(DATA)):
    return Stream(itag=140, url=url, mime_type='audio/mp4; codecs="x"', codecs="x", bitrate=128000,
                  content_length=length, has_audio=True, has_video=False, client="T", headers={"User-Agent": "UA"})


class DownloadStream(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def session(self):
        return FakeSession({})

    def test_full_download_in_chunks(self):
        with RangeServer(DATA) as srv:
            dest = os.path.join(self.dir, "a.m4a")
            seen = []
            download_stream(self.session(), stream_for(srv.url()), dest, chunk_size=300_000,
                            progress=lambda d, t: seen.append((d, t)))
            self.assertEqual(read(dest), DATA)
            self.assertFalse(os.path.exists(dest + ".part"))
            self.assertEqual(seen[-1], (len(DATA), len(DATA)))
            self.assertEqual(len(srv.requests), 4)          # ceil(1 MiB / 300 kB)
            self.assertTrue(all(r[1].startswith("bytes=") for r in srv.requests))

    def test_follows_redirects_keeping_range(self):
        with RangeServer(DATA) as srv:
            dest = os.path.join(self.dir, "a.m4a")
            download_stream(self.session(), stream_for(srv.url("/redir")), dest, chunk_size=600_000)
            self.assertEqual(read(dest), DATA)
            self.assertTrue(all(r[1] for r in srv.requests if r[0].startswith("/file")))

    def test_resume_from_part_file(self):
        with RangeServer(DATA) as srv:
            dest = os.path.join(self.dir, "a.m4a")
            with open(dest + ".part", "wb") as fh:
                fh.write(DATA[:400_000])
            download_stream(self.session(), stream_for(srv.url()), dest, chunk_size=1 << 20)
            self.assertEqual(read(dest), DATA)
            self.assertEqual(srv.requests[0][1], "bytes=400000-1048575")

    def test_no_resume_starts_over(self):
        with RangeServer(DATA) as srv:
            dest = os.path.join(self.dir, "a.m4a")
            with open(dest + ".part", "wb") as fh:
                fh.write(b"garbage")
            download_stream(self.session(), stream_for(srv.url()), dest, resume=False)
            self.assertEqual(read(dest), DATA)
            self.assertEqual(srv.requests[0][1], "bytes=0-1048575")

    def test_403_raises_stream_refused_and_keeps_part(self):
        with RangeServer(DATA) as srv:
            dest = os.path.join(self.dir, "a.m4a")
            with self.assertRaises(StreamRefused) as cm:
                download_stream(self.session(), stream_for(srv.url("/denied")), dest)
            self.assertEqual(cm.exception.status, 403)
            self.assertFalse(os.path.exists(dest))

    def test_cap_makes_second_chunk_fail(self):
        with RangeServer(DATA, cap=200_000) as srv:
            dest = os.path.join(self.dir, "a.m4a")
            with self.assertRaises(StreamRefused):
                download_stream(self.session(), stream_for(srv.url("/cap")), dest, chunk_size=200_000)
            self.assertEqual(os.path.getsize(dest + ".part"), 200_000)   # got what the preview allowed

    def test_unknown_length(self):
        with RangeServer(DATA) as srv:
            dest = os.path.join(self.dir, "a.m4a")
            download_stream(self.session(), stream_for(srv.url(), length=0), dest, chunk_size=400_000)
            self.assertEqual(read(dest), DATA)

    def test_content_range_overrides_wrong_declared_length(self):
        with RangeServer(DATA) as srv:
            dest = os.path.join(self.dir, "a.m4a")
            download_stream(self.session(), stream_for(srv.url(), length=len(DATA) + 10), dest)
            self.assertEqual(read(dest), DATA)

    def test_short_file_ends_consistently(self):
        with RangeServer(DATA[:1000]) as srv:
            dest = os.path.join(self.dir, "a.m4a")
            # declared size is wrong; the server's Content-Range corrects it, so the result is
            # exactly what the server has
            download_stream(self.session(), stream_for(srv.url(), length=5000), dest)
            self.assertEqual(read(dest), DATA[:1000])

    def test_retries_transient_failure(self):
        with RangeServer(DATA) as srv:
            session = self.session()
            real_open, calls = session.open, []

            def flaky(url, **kw):
                calls.append(1)
                if len(calls) == 1:
                    raise ConnectionResetError("boom")
                return real_open(url, **kw)
            session.open = flaky
            import ytstream.download as dl
            dl.time.sleep = lambda s: None
            dest = os.path.join(self.dir, "a.m4a")
            download_stream(session, stream_for(srv.url()), dest)
            self.assertEqual(read(dest), DATA)
            self.assertEqual(len(calls), 2)


class DownloadFailover(unittest.TestCase):
    def test_refused_client_is_replaced_by_the_next(self):
        with RangeServer(DATA) as srv, tempfile.TemporaryDirectory() as d:
            itag = ((140, 'audio/mp4; codecs="x"', 129000, len(DATA)),)
            player = {"VISIONOS": player_ok(srv.url("/denied"), itags=itag, title="My Song"),
                      "ANDROID_VR": player_ok(srv.url("/file"), itags=itag, title="My Song")}
            yt = YTStream(session=FakeSession(player), cache_dir=False, ytdlp_fallback=False)
            path = yt.download(VID, os.path.join(d, "%(title)s [%(id)s].%(ext)s"))
            self.assertEqual(path.name, f"My Song [{VID}].m4a")
            self.assertEqual(path.read_bytes(), DATA)
            self.assertEqual(yt.extract(VID).client, "ANDROID_VR")   # VISIONOS now excluded

    def test_gives_up_after_failovers(self):
        with RangeServer(DATA) as srv, tempfile.TemporaryDirectory() as d:
            itag = ((140, 'audio/mp4; codecs="x"', 129000, len(DATA)),)
            player = {n: player_ok(srv.url("/denied"), itags=itag) for n in
                      ("VISIONOS", "ANDROID_VR", "TVHTML5", "WEB_REMIX", "IOS")}
            yt = YTStream(session=FakeSession(player), cache_dir=False, ytdlp_fallback=False)
            with self.assertRaises(StreamRefused):
                yt.download(VID, os.path.join(d, "x.m4a"), failovers=2)


if __name__ == "__main__":
    unittest.main()
