# ytstream

Resolve and download YouTube **audio** streams in about a second, using InnerTube
clients directly. No `player.js`, no JavaScript runtime, **no dependencies** (stdlib only).
Handy for music bots and players that just need a playable URL.

> Uses YouTube's unofficial, undocumented InnerTube API. It can break whenever YouTube
> changes something (see [Keeping it working](#keeping-it-working)). Only download or play
> content you have the right to use; this project is not affiliated with YouTube or Google.

## Install

```bash
pip install git+https://github.com/<you>/ytstream.git
# optional: let it fall back to yt-dlp when every client fails
pip install "yt-dlp"
```

Python 3.9+ (works in Termux).

## Command line

```bash
ytstream https://youtu.be/dQw4w9WgXcQ              # download best audio -> "Title [id].webm"
ytstream -g dQw4w9WgXcQ                             # just print the stream URL
ytstream -F dQw4w9WgXcQ                             # list formats
ytstream -f m4a -o "%(author)s - %(title)s.%(ext)s" dQw4w9WgXcQ
ytstream -j dQw4w9WgXcQ > info.json                 # metadata + every stream as JSON
ytstream -a ids.txt -o music/                       # batch file, one URL/ID per line
ytstream --proxy socks5h://127.0.0.1:9050 -g ID     # through Tor
```

| Option | Meaning |
|---|---|
| `-g` / `-F` / `-j` | print URL / list formats / dump JSON (default: download) |
| `-f SEL` | `bestaudio` (default), `worstaudio`, `m4a`, `webm`, an itag like `140`, `best`, `bestvideo`, or fallbacks `251/140/bestaudio` |
| `-o TEMPLATE` | `%(id)s %(title)s %(author)s %(ext)s %(itag)s %(kbps)s %(duration)s %(client)s`; a directory (or trailing `/`) gets the default name |
| `--max-kbps N` | prefer audio up to N kbps |
| `--no-validate` | skip the last-byte URL check (faster, less safe) |
| `--no-continue`, `--retries N` | resume control (downloads go to `*.part` and resume by default) |
| `--clients A,B`, `--client-config FILE` | choose client order / add or update clients from JSON |
| `--proxy URL` | `socks5h://host:port` or `http://host:port` |
| `--no-ytdlp`, `--no-cache`, `--visitor-data ID` | fallback / visitor-ID cache controls |
| `-v`, `-q`, `--no-progress` | logging and output |

`-F`, `-g` and `-j` only talk to the API; downloads use 1 MiB ranged requests.

## Library

```python
from ytstream import YTStream

yt = YTStream()                                   # reuse one instance (thread-safe, caches results)
stream = yt.get_stream("https://youtu.be/dQw4w9WgXcQ")          # best audio
print(stream.url, stream.ext, stream.kbps)
print(stream.headers)                             # send these with media requests (User-Agent, ...)

info = yt.extract("dQw4w9WgXcQ")                  # title, author, duration, thumbnails, all streams
yt.get_stream("dQw4w9WgXcQ", "m4a", max_kbps=130)
path = yt.download("dQw4w9WgXcQ", "music/%(title)s.%(ext)s")

# asyncio (Quart, FastAPI, discord/whatsapp bots ...)
stream = await yt.aget_stream("dQw4w9WgXcQ")
```

If a listener's player gets HTTP 403 on a URL, tell the resolver, then ask again:

```python
yt.report_refused(video_id, stream.client)        # skips that client for this video for 10 minutes
stream = yt.get_stream(video_id)                  # fresh URL from another client
```

Options: `YTStream(proxy=..., timeout=..., cascade=[...], clients={...}, cache_ttl=..., ytdlp_fallback=True)`.
Errors: `ExtractionError` (has `.reasons` per client), `StreamRefused`, `DownloadError`, all subclasses of `YTStreamError`.

## How it works

1. Fetches a **visitor ID** once (cached in `~/.cache/ytstream`). Without one, anonymous requests often get
   *"Sign in to confirm you're not a bot"*.
2. Calls the InnerTube `/player` endpoint as each client in turn (`VISIONOS`, `ANDROID_VR`, `TVHTML5`,
   `WEB_REMIX`, `IOS`) until one returns direct audio URLs.
3. **Validates** the URL by requesting the file's *last byte*. Some clients only serve a ~1 MiB preview;
   they pass a first-bytes check but fail this one.
4. Caches the result until shortly before the URL expires (about 6 hours).

Stream URLs from these clients are not tied to the requester's IP in our tests, but that is Google's
call and could change; keep `report_refused` in your player's error path.

## Keeping it working

Client versions and user agents are plain data (`ytstream/clients.py`). When a client stops working, update
its entry, either in a release or without code changes via `--client-config clients.json` / `YTStream(clients=...)`:

```json
{"VISIONOS": {"client_id": "101", "user_agent": "...",
              "context": {"clientName": "VISIONOS", "clientVersion": "1.02", "deviceMake": "Apple"}}}
```

Current values can be read from yt-dlp's `extractor/youtube/_base.py` (`INNERTUBE_CLIENTS`).

## Limitations

- Audio first. `-f best` / `bestvideo` list and download what clients return, but there is no video+audio merging.
- No playlists, search, login or cookies, and no signature-cipher / PoToken support. Clients that need those are
  skipped, and yt-dlp (if installed) is used as the fallback.
- SABR-only responses (no direct URLs) are skipped.

## Development

```bash
python -m unittest discover -s tests -v
```

The tests use fake InnerTube responses plus local HTTP/SOCKS5 servers; they never contact YouTube.

## License

MIT. Client configuration values are factual identifiers also published by yt-dlp and other open-source projects.
