import contextlib
import io
import json
import os
import tempfile
import unittest
from unittest import mock

from helpers import FakeSession, RangeServer, player_ok
from ytstream import YTStream, cli

DATA = bytes(range(256)) * 100
VID = "dQw4w9WgXcQ"


def run(argv, yt):
    out, err = io.StringIO(), io.StringIO()
    with mock.patch.object(cli, "_make_client", lambda args: yt), \
            contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = cli.main(argv)
        except SystemExit as exc:
            code = exc.code
    return code, out.getvalue(), err.getvalue()


class Cli(unittest.TestCase):
    def setUp(self):
        self.srv = RangeServer(DATA)
        self.srv.__enter__()
        itags = ((140, 'audio/mp4; codecs="mp4a.40.2"', 129000, len(DATA)),
                 (251, 'audio/webm; codecs="opus"', 135000, len(DATA)))
        self.yt = YTStream(session=FakeSession({"VISIONOS": player_ok(self.srv.url("/file"), itags=itags, title="My Song")}),
                           cache_dir=False, ytdlp_fallback=False)

    def tearDown(self):
        self.srv.__exit__(None, None, None)

    def test_get_url_default_and_format(self):
        code, out, _ = run(["-g", VID], self.yt)
        self.assertEqual((code, out.strip()), (0, self.srv.url("/file") + "?itag=251"))
        code, out, _ = run(["-g", "-f", "m4a", f"https://youtu.be/{VID}"], self.yt)
        self.assertTrue(out.strip().endswith("itag=140"))
        code, out, _ = run(["-g", "--max-kbps", "130", VID], self.yt)
        self.assertTrue(out.strip().endswith("itag=140"))

    def test_list_formats(self):
        code, out, _ = run(["-F", VID], self.yt)
        self.assertEqual(code, 0)
        lines = out.strip().splitlines()
        self.assertTrue(lines[0].startswith("ID"))
        self.assertEqual(len(lines), 3)
        self.assertIn("audio only", lines[1])
        self.assertIn("mp4a.40.2", out)

    def test_dump_json(self):
        code, out, _ = run(["-j", VID], self.yt)
        data = json.loads(out)
        self.assertEqual((code, data["title"], data["client"]), (0, "My Song", "VISIONOS"))
        self.assertEqual(len(data["streams"]), 2)

    def test_download_with_template(self):
        with tempfile.TemporaryDirectory() as d:
            code, out, err = run(["-f", "140", "-o", os.path.join(d, "%(title)s.%(ext)s"), "--no-progress", VID], self.yt)
            self.assertEqual(code, 0, err)
            path = out.strip()
            self.assertEqual(os.path.basename(path), "My Song.m4a")
            self.assertEqual(open(path, "rb").read(), DATA)

    def test_unavailable_format_is_reported(self):
        code, _, err = run(["-g", "-f", "bestvideo", VID], self.yt)
        self.assertEqual(code, 1)
        self.assertIn("not available", err)

    def test_batch_file_and_partial_failure(self):
        with tempfile.TemporaryDirectory() as d:
            batch = os.path.join(d, "ids.txt")
            with open(batch, "w") as fh:
                fh.write(f"# comment\n{VID}\nnot a video\n")
            code, out, err = run(["-g", "-a", batch], self.yt)
            self.assertEqual(code, 1)
            self.assertIn("itag=251", out)
            self.assertIn("could not find a video id", err)

    def test_no_targets_is_a_usage_error(self):
        code, _, err = run([], self.yt)
        self.assertEqual(code, 2)
        self.assertIn("at least one", err)

    def test_help_mentions_template_without_crashing(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit):
            cli.main(["--help"])
        self.assertIn("%(title)s", out.getvalue())


if __name__ == "__main__":
    unittest.main()
