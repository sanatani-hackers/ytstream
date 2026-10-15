import json
import os
import tempfile
import unittest
from pathlib import Path

from helpers import FakeSession
from ytstream import (ClientConfig, Stream, VideoInfo, YTStreamError, extract_video_id, load_clients,
                      select_stream)
from ytstream.download import render_output
from ytstream.visitor import VisitorStore


def S(itag, mime, bitrate, **kw):
    has_a = kw.pop("has_audio", mime.startswith("audio"))
    has_v = kw.pop("has_video", mime.startswith("video"))
    return Stream(itag=itag, url=f"http://x/{itag}", mime_type=mime, codecs="", bitrate=bitrate,
                  content_length=1000, has_audio=has_a, has_video=has_v, client="T", **kw)


STREAMS = [
    S(139, 'audio/mp4; codecs="mp4a.40.5"', 49000),
    S(140, 'audio/mp4; codecs="mp4a.40.2"', 129000),
    S(251, 'audio/webm; codecs="opus"', 135000),
    S(250, 'audio/webm; codecs="opus"', 70000),
    S(137, 'video/mp4; codecs="avc1"', 4000000, height=1080),
    S(18, 'video/mp4; codecs="avc1, mp4a"', 500000, height=360, has_audio=True, has_video=True),
]


class VideoId(unittest.TestCase):
    def test_forms(self):
        vid = "dQw4w9WgXcQ"
        for text in (vid, f"https://youtu.be/{vid}", f"https://www.youtube.com/watch?v={vid}&t=5",
                     f"https://music.youtube.com/watch?v={vid}", f"https://youtube.com/shorts/{vid}",
                     f"https://www.youtube.com/live/{vid}?feature=share"):
            self.assertEqual(extract_video_id(text), vid, text)

    def test_bad(self):
        with self.assertRaises(YTStreamError):
            extract_video_id("not a video")


class Selection(unittest.TestCase):
    def test_best_and_worst_audio(self):
        self.assertEqual(select_stream(STREAMS, "bestaudio").itag, 251)
        self.assertEqual(select_stream(STREAMS, "worstaudio").itag, 139)

    def test_max_kbps(self):
        self.assertEqual(select_stream(STREAMS, "bestaudio", 130).itag, 140)
        self.assertEqual(select_stream(STREAMS, "bestaudio", 10).itag, 139)  # none fit: smallest

    def test_containers(self):
        self.assertEqual(select_stream(STREAMS, "m4a").itag, 140)
        self.assertEqual(select_stream(STREAMS, "webm").itag, 251)

    def test_itag_and_fallbacks(self):
        self.assertEqual(select_stream(STREAMS, "18").itag, 18)
        self.assertEqual(select_stream(STREAMS, "999/140").itag, 140)
        self.assertIsNone(select_stream(STREAMS, "999"))

    def test_video_selectors(self):
        self.assertEqual(select_stream(STREAMS, "best").itag, 18)
        self.assertEqual(select_stream(STREAMS, "bestvideo").itag, 137)

    def test_ext(self):
        self.assertEqual(STREAMS[1].ext, "m4a")
        self.assertEqual(STREAMS[2].ext, "webm")
        self.assertEqual(STREAMS[4].ext, "mp4")


class Templates(unittest.TestCase):
    def info(self, title):
        return VideoInfo(video_id="abcdefghijk", title=title, author="A/B", duration=7)

    def test_sanitises_values_not_template(self):
        p = render_output("out/%(title)s [%(id)s].%(ext)s", self.info('a/b:c*"d"?'), STREAMS[1])
        self.assertEqual(p, Path("out/a_b_c__d__ [abcdefghijk].m4a"))

    def test_literal_and_dir(self):
        self.assertEqual(render_output("song.m4a", self.info("t"), STREAMS[1]), Path("song.m4a"))
        self.assertEqual(render_output("100%.m4a", self.info("t"), STREAMS[1]), Path("100%.m4a"))
        with tempfile.TemporaryDirectory() as d:
            p = render_output(d, self.info("t"), STREAMS[1])
            self.assertEqual(p.parent, Path(d))
            self.assertEqual(p.name, "t [abcdefghijk].m4a")

    def test_unknown_field(self):
        with self.assertRaises(YTStreamError):
            render_output("%(nope)s", self.info("t"), STREAMS[1])


class Clients(unittest.TestCase):
    def test_load_and_validate(self):
        with tempfile.TemporaryDirectory() as d:
            good = os.path.join(d, "c.json")
            with open(good, "w") as fh:
                json.dump({"X": {"client_id": "1", "user_agent": "UA",
                                 "context": {"clientName": "X", "clientVersion": "9"}}}, fh)
            c = load_clients(good)["X"]
            self.assertIsInstance(c, ClientConfig)
            self.assertEqual(c.version, "9")
            bad = os.path.join(d, "b.json")
            with open(bad, "w") as fh:
                json.dump({"X": {"client_id": "1"}}, fh)
            with self.assertRaises(ValueError):
                load_clients(bad)

    def test_media_headers(self):
        from ytstream import DEFAULT_CLIENTS
        self.assertNotIn("Origin", DEFAULT_CLIENTS["VISIONOS"].media_headers())
        self.assertEqual(DEFAULT_CLIENTS["WEB_REMIX"].media_headers()["Origin"], "https://music.youtube.com")


class Visitor(unittest.TestCase):
    def test_cached_on_disk_and_per_proxy(self):
        with tempfile.TemporaryDirectory() as d:
            fake = FakeSession({}, visitors=("V1", "V2"))
            self.assertEqual(VisitorStore(d).get(fake), "V1")
            again = FakeSession({}, visitors=("V2",))
            self.assertEqual(VisitorStore(d).get(again), "V1")   # from disk, no request
            self.assertEqual(again.visitor_calls, 0)
            self.assertEqual(VisitorStore(d, proxy_key="socks5h://x").get(again), "V2")  # other key
            self.assertEqual(VisitorStore(d).get(again, force=True), "V2")

    def test_fixed_and_disabled(self):
        fake = FakeSession({}, visitors=("V1",))
        self.assertEqual(VisitorStore(None, fixed="FIXED").get(fake), "FIXED")
        self.assertEqual(fake.visitor_calls, 0)
        VisitorStore(None).get(fake)
        self.assertEqual(fake.visitor_calls, 1)


if __name__ == "__main__":
    unittest.main()
