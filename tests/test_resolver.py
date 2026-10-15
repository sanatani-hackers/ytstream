import sys
import types
import unittest

from helpers import FakeSession, player_denied, player_ok
from ytstream import ExtractionError, YTStream, YTStreamError

VID = "dQw4w9WgXcQ"


def make(player, **kw):
    probes = kw.pop("probes", None)
    visitors = kw.pop("visitors", ("V1",))
    session = FakeSession(player, probes, visitors)
    return YTStream(session=session, cache_dir=False, ytdlp_fallback=kw.pop("ytdlp_fallback", False), **kw), session


class Cascade(unittest.TestCase):
    def test_falls_through_to_next_client(self):
        yt, fs = make({"VISIONOS": player_denied(), "ANDROID_VR": player_ok("http://a/vr", title="T")})
        info = yt.extract(VID)
        self.assertEqual(info.client, "ANDROID_VR")
        self.assertEqual(info.title, "T")
        self.assertEqual([c[0] for c in fs.player_calls], ["VISIONOS", "ANDROID_VR"])

    def test_request_shape(self):
        yt, fs = make({"VISIONOS": player_ok("http://a/v")})
        yt.extract(VID)
        name, body, headers = fs.player_calls[0]
        self.assertEqual(body["videoId"], VID)
        self.assertEqual(body["context"]["client"]["visitorData"], "V1")
        self.assertEqual(headers["X-Goog-Visitor-Id"], "V1")
        self.assertEqual(headers["X-YouTube-Client-Name"], "101")

    def test_all_fail_reports_reasons(self):
        yt, _ = make({n: player_denied("UNPLAYABLE", "Video unavailable")
                      for n in ("VISIONOS", "ANDROID_VR", "TVHTML5", "WEB_REMIX", "IOS")})
        with self.assertRaises(ExtractionError) as cm:
            yt.extract(VID)
        self.assertEqual(set(cm.exception.reasons), {"VISIONOS", "ANDROID_VR", "TVHTML5", "WEB_REMIX", "IOS"})
        self.assertIn("UNPLAYABLE", cm.exception.reasons["IOS"])

    def test_cipher_only_and_sabr_only_are_skipped(self):
        ciphered = {"playabilityStatus": {"status": "OK"},
                    "streamingData": {"adaptiveFormats": [{"itag": 140, "signatureCipher": "s=x", "mimeType": "audio/mp4"}]}}
        bare = {"playabilityStatus": {"status": "OK"}, "streamingData": {"adaptiveFormats": [{"itag": 140}]}}
        yt, _ = make({"VISIONOS": ciphered, "ANDROID_VR": bare}, cascade=["VISIONOS", "ANDROID_VR"])
        with self.assertRaises(ExtractionError) as cm:
            yt.extract(VID)
        self.assertIn("signatureCipher", cm.exception.reasons["VISIONOS"])
        self.assertIn("SABR", cm.exception.reasons["ANDROID_VR"])

    def test_pinned_clients(self):
        yt, fs = make({"IOS": player_ok("http://a/ios")})
        self.assertEqual(yt.extract(VID, clients=["IOS"]).client, "IOS")
        self.assertEqual([c[0] for c in fs.player_calls], ["IOS"])

    def test_network_error_in_one_client_is_not_fatal(self):
        def boom(body):
            raise OSError("connection reset")
        yt, _ = make({"VISIONOS": boom, "ANDROID_VR": player_ok("http://a/vr")})
        self.assertEqual(yt.extract(VID).client, "ANDROID_VR")


class Validation(unittest.TestCase):
    def test_capped_preview_is_rejected_by_last_byte_probe(self):
        yt, fs = make({"VISIONOS": player_ok("http://a/v", itags=((140, 'audio/mp4; codecs="x"', 129000, 5000),)),
                       "ANDROID_VR": player_ok("http://a/vr", itags=((140, 'audio/mp4; codecs="x"', 129000, 5000),))},
                      probes={"http://a/v?itag=140": 403})
        info = yt.extract(VID)
        self.assertEqual(info.client, "ANDROID_VR")
        self.assertEqual(fs.probe_calls[0], ("http://a/v?itag=140", "4999-4999"))  # last byte, not first
        self.assertEqual(info.validated_itag, 140)

    def test_no_validate_skips_probe(self):
        yt, fs = make({"VISIONOS": player_ok("http://a/v")}, probes={"http://a/v?itag=140": 403})
        self.assertEqual(yt.extract(VID, validate=False).client, "VISIONOS")
        self.assertEqual(fs.probe_calls, [])

    def test_probe_network_error_is_accepted(self):
        yt, fs = make({"VISIONOS": player_ok("http://a/v")})
        fs.probe = lambda *a, **k: (_ for _ in ()).throw(OSError("timeout"))
        self.assertEqual(yt.extract(VID).client, "VISIONOS")


class CacheAndExclusion(unittest.TestCase):
    def test_second_extract_uses_cache(self):
        yt, fs = make({"VISIONOS": player_ok("http://a/v")})
        a = yt.extract(VID)
        b = yt.extract(VID)
        self.assertIs(a, b)
        self.assertEqual(len(fs.player_calls), 1)
        yt.extract(VID, use_cache=False)
        self.assertEqual(len(fs.player_calls), 2)

    def test_expiry_drops_cache(self):
        yt, fs = make({"VISIONOS": player_ok("http://a/v")})
        info = yt.extract(VID)
        info.expires_at = 1.0   # long gone
        yt.extract(VID)
        self.assertEqual(len(fs.player_calls), 2)

    def test_report_refused_skips_client_and_drops_cache(self):
        yt, fs = make({"VISIONOS": player_ok("http://a/v"), "ANDROID_VR": player_ok("http://a/vr")})
        self.assertEqual(yt.extract(VID).client, "VISIONOS")
        yt.report_refused(VID, "VISIONOS")
        self.assertEqual(yt.extract(VID).client, "ANDROID_VR")
        yt.report_refused(VID, "VISIONOS", ttl=-1)  # already expired: no longer excluded
        yt.clear_cache()
        self.assertEqual(yt.extract(VID).client, "VISIONOS")


class VisitorRetry(unittest.TestCase):
    def test_bot_check_triggers_one_fresh_visitor_retry(self):
        def needs_v2(body):
            ok = body["context"]["client"].get("visitorData") == "V2"
            return player_ok("http://a/v") if ok else player_denied()
        yt, fs = make({n: needs_v2 for n in ("VISIONOS", "ANDROID_VR", "TVHTML5", "WEB_REMIX", "IOS")},
                      visitors=("V1", "V2"))
        self.assertEqual(yt.extract(VID).client, "VISIONOS")
        self.assertEqual(fs.visitor_calls, 2)

    def test_fixed_visitor_is_never_replaced(self):
        yt, fs = make({n: player_denied() for n in ("VISIONOS", "ANDROID_VR", "TVHTML5", "WEB_REMIX", "IOS")},
                      visitor_data="FIXED")
        with self.assertRaises(ExtractionError):
            yt.extract(VID)
        self.assertEqual(fs.visitor_calls, 0)


class Facade(unittest.TestCase):
    def test_get_stream_and_missing_format(self):
        yt, _ = make({"VISIONOS": player_ok("http://a/v")})
        s = yt.get_stream(VID)
        self.assertEqual((s.itag, s.client, s.ext), (140, "VISIONOS", "m4a"))
        self.assertEqual(s.headers["User-Agent"][:7], "Mozilla")
        with self.assertRaises(YTStreamError):
            yt.get_stream(VID, "bestvideo")

    def test_info_to_dict(self):
        yt, _ = make({"VISIONOS": player_ok("http://a/v")})
        d = yt.extract(VID).to_dict(include_urls=False)
        self.assertEqual(d["title"], "Song")
        self.assertNotIn("url", d["streams"][0])
        self.assertEqual(d["loudness_db"], -1.5)


class YtdlpFallback(unittest.TestCase):
    def setUp(self):
        self.saved = sys.modules.get("yt_dlp")

    def tearDown(self):
        if self.saved is None:
            sys.modules.pop("yt_dlp", None)
        else:
            sys.modules["yt_dlp"] = self.saved

    def fake_ytdlp(self, seen):
        mod = types.ModuleType("yt_dlp")

        class YDL:
            def __init__(self, opts):
                seen.append(opts)
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def extract_info(self, url, download=False):
                return {"title": "FromDLP", "uploader": "U", "duration": 9, "formats": [
                    {"format_id": "140", "url": "http://d/140", "ext": "m4a", "acodec": "mp4a.40.2",
                     "vcodec": "none", "abr": 129, "protocol": "https", "filesize": 777},
                    {"format_id": "hls", "url": "http://d/x.m3u8", "ext": "mp4", "acodec": "a", "vcodec": "v",
                     "protocol": "m3u8_native"}]}
        mod.YoutubeDL = YDL
        sys.modules["yt_dlp"] = mod

    def test_used_when_every_client_fails(self):
        seen = []
        self.fake_ytdlp(seen)
        yt, _ = make({n: player_denied() for n in ("VISIONOS", "ANDROID_VR", "TVHTML5", "WEB_REMIX", "IOS")},
                     ytdlp_fallback=True, proxy=None)
        info = yt.extract(VID)
        self.assertEqual(info.client, "yt-dlp")
        s = info.select("bestaudio")
        self.assertEqual((s.itag, s.ext, s.content_length, s.kbps), (140, "m4a", 777, 129))
        self.assertEqual(len(info.streams), 1)   # the m3u8 entry is ignored

    def test_not_used_when_clients_pinned_or_disabled(self):
        self.fake_ytdlp([])
        denied = {n: player_denied() for n in ("VISIONOS", "ANDROID_VR", "TVHTML5", "WEB_REMIX", "IOS")}
        yt, _ = make(denied, ytdlp_fallback=True)
        with self.assertRaises(ExtractionError):
            yt.extract(VID, clients=["IOS"])
        yt2, _ = make(denied, ytdlp_fallback=False)
        with self.assertRaises(ExtractionError):
            yt2.extract(VID)

    def test_missing_ytdlp_is_not_an_error_source(self):
        sys.modules["yt_dlp"] = None   # makes `import yt_dlp` raise ImportError
        yt, _ = make({n: player_denied() for n in ("VISIONOS", "ANDROID_VR", "TVHTML5", "WEB_REMIX", "IOS")},
                     ytdlp_fallback=True)
        with self.assertRaises(ExtractionError) as cm:
            yt.extract(VID)
        self.assertNotIn("yt-dlp", cm.exception.reasons)


class AsyncApi(unittest.TestCase):
    def test_aextract(self):
        import asyncio
        yt, _ = make({"VISIONOS": player_ok("http://a/v")})
        info = asyncio.run(yt.aextract(VID))
        self.assertEqual(info.client, "VISIONOS")


if __name__ == "__main__":
    unittest.main()
