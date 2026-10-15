"""Command line interface (``ytstream`` / ``python -m ytstream``)."""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from typing import List, Optional

from . import __version__
from .clients import load_clients
from .download import DEFAULT_TEMPLATE
from .errors import YTStreamError
from .models import Stream, VideoInfo
from .resolver import YTStream


def _size(n: int) -> str:
    if not n:
        return "-"
    value = float(n)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if value < 1024 or unit == "GiB":
            return f"{value:.1f}{unit}" if unit != "B" else f"{int(value)}B"
        value /= 1024
    return "-"


def format_table(info: VideoInfo) -> str:
    rows = [("ID", "EXT", "TYPE", "BITRATE", "SIZE", "CODECS", "CLIENT")]
    for s in sorted(info.streams, key=lambda s: (not s.audio_only, s.audio_only and -s.bitrate, s.height)):
        kind = "audio only" if s.audio_only else (f"video only {s.height}p" if s.video_only else f"muxed {s.height}p")
        rows.append((str(s.itag), s.ext, kind, f"{s.kbps}k" if s.bitrate else "-",
                     _size(s.content_length), s.codecs or "-", s.client))
    widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
    return "\n".join("  ".join(c.ljust(w) for c, w in zip(r, widths)).rstrip() for r in rows)


class _Progress:
    def __init__(self, enabled: bool):
        self.enabled = enabled
        self.start = time.time()
        self.last = 0.0
        self.first: Optional[int] = None

    def __call__(self, done: int, total: Optional[int]) -> None:
        if not self.enabled:
            return
        now = time.time()
        if self.first is None:
            self.first = done
        if now - self.last < 0.2 and not (total and done >= total):
            return
        self.last = now
        speed = (done - self.first) / max(now - self.start, 1e-6)
        if total:
            eta = (total - done) / speed if speed > 0 else 0
            line = (f"[download] {done * 100 / total:5.1f}% of {_size(total)} "
                    f"at {_size(int(speed))}/s ETA {int(eta) // 60:02d}:{int(eta) % 60:02d}")
        else:
            line = f"[download] {_size(done)} at {_size(int(speed))}/s"
        sys.stderr.write("\r" + line + "   ")
        if total and done >= total:
            sys.stderr.write("\n")
        sys.stderr.flush()


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="ytstream",
        description="Resolve and download YouTube audio through InnerTube clients (no player.js needed).",
    )
    p.add_argument("targets", nargs="*", metavar="URL_OR_ID")
    p.add_argument("-a", "--batch-file", metavar="FILE", help="file with one URL/ID per line")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("-g", "--get-url", action="store_true", help="print the stream URL instead of downloading")
    mode.add_argument("-F", "--list-formats", action="store_true", help="list available formats")
    mode.add_argument("-j", "--dump-json", action="store_true", help="print video info and streams as JSON")
    p.add_argument("-f", "--format", default="bestaudio", metavar="SEL",
                   help="itag, bestaudio, worstaudio, m4a, webm, best, bestvideo ... or a/b/c fallbacks "
                        "(default: bestaudio)")
    p.add_argument("-o", "--output", default=DEFAULT_TEMPLATE, metavar="TEMPLATE",
                   help='output path/template: %%(id)s %%(title)s %%(author)s %%(ext)s %%(itag)s ... '
                        f'(default: "{DEFAULT_TEMPLATE.replace("%", "%%")}")')
    p.add_argument("--max-kbps", type=int, metavar="N", help="prefer audio up to N kbps")
    p.add_argument("--no-validate", action="store_true", help="skip the last-byte URL check")
    p.add_argument("--no-continue", action="store_true", help="do not resume a .part file")
    p.add_argument("--retries", type=int, default=3, metavar="N", help="retries per chunk (default 3)")
    p.add_argument("--clients", metavar="A,B", help="client order, e.g. VISIONOS,ANDROID_VR")
    p.add_argument("--client-config", metavar="FILE", help="JSON file with client definitions")
    p.add_argument("--proxy", metavar="URL", help="socks5h://127.0.0.1:9050 (Tor), http://host:port, ...")
    p.add_argument("--timeout", type=float, default=15.0, metavar="SEC")
    p.add_argument("--visitor-data", metavar="ID", help="use this visitor ID instead of fetching one")
    p.add_argument("--no-cache", action="store_true", help="do not read/write the visitor ID cache file")
    p.add_argument("--no-ytdlp", action="store_true", help="never fall back to yt-dlp")
    p.add_argument("--no-progress", action="store_true")
    p.add_argument("-q", "--quiet", action="store_true")
    p.add_argument("-v", "--verbose", action="store_true", help="debug log on stderr")
    p.add_argument("--version", action="version", version=f"ytstream {__version__}")
    return p


def _make_client(args: argparse.Namespace) -> YTStream:
    return YTStream(
        proxy=args.proxy, timeout=args.timeout,
        clients=load_clients(args.client_config) if args.client_config else None,
        cascade=[c.strip().upper() for c in args.clients.split(",") if c.strip()] if args.clients else None,
        cache_dir=False if args.no_cache else None,
        ytdlp_fallback=not args.no_ytdlp,
        visitor_data=args.visitor_data,
    )


def _pick(info: VideoInfo, args: argparse.Namespace) -> Stream:
    stream = info.select(args.format, args.max_kbps)
    if stream is None:
        have = ", ".join(f"{s.itag}({s.ext})" for s in info.streams) or "none"
        raise YTStreamError(f"requested format {args.format!r} is not available (have: {have})")
    return stream


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    targets = list(args.targets)
    if args.batch_file:
        try:
            with open(args.batch_file, "r", encoding="utf-8") as fh:
                targets += [ln.strip() for ln in fh if ln.strip() and not ln.lstrip().startswith("#")]
        except OSError as exc:
            parser.error(f"cannot read batch file: {exc}")
    if not targets:
        parser.error("give at least one URL or video ID")

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING,
                        format="%(name)s: %(message)s", stream=sys.stderr)
    try:
        yt = _make_client(args)
    except (YTStreamError, ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    status = 0
    for target in targets:
        try:
            if args.get_url or args.list_formats or args.dump_json:
                info = yt.extract(target, validate=not (args.no_validate or args.list_formats),
                                  max_kbps=args.max_kbps or 160)
                if args.list_formats:
                    print(format_table(info))
                elif args.dump_json:
                    print(json.dumps(info.to_dict(), indent=2, ensure_ascii=False))
                else:
                    print(_pick(info, args).url)
            else:
                progress = _Progress(enabled=not (args.quiet or args.no_progress))
                path = yt.download(target, args.output, args.format, max_kbps=args.max_kbps,
                                   resume=not args.no_continue, retries=args.retries,
                                   progress=progress, validate=not args.no_validate)
                print(path)
        except YTStreamError as exc:
            print(f"error: {exc}", file=sys.stderr)
            status = 1
    return status


if __name__ == "__main__":
    raise SystemExit(main())
