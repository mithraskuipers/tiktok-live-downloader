#!/usr/bin/env python3
"""
TikTok Live Downloader
Usage: python tiktok-live-downloader.py <username> [--cookies <file>] [--output <dir>] [--daemon] [--interval <seconds>]

Examples:
  python tiktok-live-downloader.py <username>
  python tiktok-live-downloader.py <username> --cookies myfile.json
  python tiktok-live-downloader.py <username> --output C:/recordings
  python tiktok-live-downloader.py <username> --daemon
  python tiktok-live-downloader.py <username> --daemon --interval 30
"""

import sys
import subprocess
import http.cookiejar
import tempfile
import os
import json
import re
import glob
import argparse
import time
import urllib.request
import urllib.error
from datetime import datetime


BROWSERS = ["chrome", "firefox", "edge", "brave", "chromium", "opera", "vivaldi"]
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

LIVE_CHECK_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)


def load_cookies_json(cookie_file: str) -> list[str]:
    """Parse Cookie-Editor JSON export format."""
    with open(cookie_file, "r", encoding="utf-8") as f:
        cookies = json.load(f)
    args = []
    for c in cookies:
        domain = c.get("domain", "")
        name   = c.get("name", "")
        value  = c.get("value", "")
        if "tiktok" in domain and name and value:
            args += ["--http-cookie", f"{name}={value}"]
    return args


def load_cookies_txt(cookie_file: str) -> list[str]:
    """Parse Netscape/Mozilla cookies.txt format."""
    jar = http.cookiejar.MozillaCookieJar(cookie_file)
    jar.load(ignore_discard=True, ignore_expires=True)
    args = []
    for c in jar:
        if "tiktok" in c.domain:
            args += ["--http-cookie", f"{c.name}={c.value}"]
    return args


def load_cookies(cookie_file: str) -> list[str]:
    """Auto-detect format by extension."""
    if cookie_file.lower().endswith(".json"):
        return load_cookies_json(cookie_file)
    else:
        return load_cookies_txt(cookie_file)


def find_json_in_project() -> str | None:
    """Scan project dir for any .json file containing TikTok cookies."""
    candidates = glob.glob(os.path.join(SCRIPT_DIR, "*.json"))
    for path in candidates:
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list) and any("tiktok" in c.get("domain", "") for c in data):
                return path
        except Exception:
            continue
    return None


def try_browser_extraction(cookie_file: str) -> str | None:
    for browser in BROWSERS:
        print(f"[*] Trying {browser}...")
        if os.path.exists(cookie_file):
            os.remove(cookie_file)

        try:
            result = subprocess.run(
                ["yt-dlp", "--no-update", "--cookies-from-browser", browser,
                 "--cookies", cookie_file, "https://www.tiktok.com"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=15,
            )
        except subprocess.TimeoutExpired:
            print(f"    [-] {browser} timed out")
            continue
        except FileNotFoundError:
            # yt-dlp isn't installed/on PATH. Browser cookie extraction is
            # impossible without it - stop trying entirely instead of
            # silently failing once per browser (and, more importantly,
            # instead of letting this exception escape uncaught and kill
            # the whole cookie-resolution/record flow, which is what was
            # previously causing recordings to silently never start).
            print("    [-] yt-dlp not found on PATH - can't extract cookies "
                  "from browsers. Install it with: pip install yt-dlp")
            return None
        except OSError as e:
            print(f"    [-] {browser} extraction failed to launch: {e}")
            continue

        if "DPAPI" in result.stderr or "Could not copy" in result.stderr:
            print(f"    [-] {browser} cookie encryption not supported (Chrome 127+ issue)")
            continue

        if not os.path.exists(cookie_file) or os.path.getsize(cookie_file) == 0:
            continue

        try:
            jar = http.cookiejar.MozillaCookieJar(cookie_file)
            jar.load(ignore_discard=True, ignore_expires=True)
            tiktok_cookies = [c for c in jar if "tiktok" in c.domain]
            if tiktok_cookies:
                print(f"[*] Found TikTok cookies in {browser} ({len(tiktok_cookies)} cookies)")
                return browser
            else:
                print(f"    [-] {browser} has no TikTok cookies")
        except Exception as e:
            print(f"    [-] Failed to read {browser} cookies: {e}")

    return None


def _cookie_header_from_args(cookie_args: list[str]) -> str:
    """Convert streamlink-style ['--http-cookie', 'name=value', ...] pairs
    into a single 'name=value; name2=value2' Cookie header string."""
    pairs = []
    it = iter(cookie_args or [])
    for a in it:
        if a == "--http-cookie":
            try:
                pairs.append(next(it))
            except StopIteration:
                break
    return "; ".join(pairs)


def check_live_status(username: str, cookie_args: list[str] | None = None) -> dict:
    """
    Check if a TikTok user is currently live by reading the room status
    embedded in their live page's SIGI_STATE (or the newer
    __UNIVERSAL_DATA_FOR_REHYDRATION__) JSON block. This is the same
    signal TikTok's own frontend uses, so it's far more reliable than
    asking streamlink to probe the stream.

    Returns a dict with 'is_live', 'status_code', and 'room_id'.
    Raises on network/parse failure — callers wanting a plain bool
    should use is_live() below, which swallows errors as False.
    """
    url = f"https://www.tiktok.com/@{username}/live"
    headers = {
        "User-Agent": LIVE_CHECK_USER_AGENT,
        "Accept-Language": "en-US,en;q=0.9",
    }
    cookie_header = _cookie_header_from_args(cookie_args)
    if cookie_header:
        headers["Cookie"] = cookie_header

    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=10) as resp:
        html = resp.read().decode("utf-8", errors="ignore")

    match = re.search(
        r'<script id="SIGI_STATE" type="application/json">(.*?)</script>',
        html, re.DOTALL
    )

    if match:
        data = json.loads(match.group(1))
        room_info = data.get("LiveRoom", {}).get("liveRoomUserInfo", {})
    else:
        match = re.search(
            r'<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__" type="application/json">(.*?)</script>',
            html, re.DOTALL
        )
        if not match:
            raise RuntimeError(
                "Could not find state data in page — TikTok may have changed "
                "their markup, or served a login wall."
            )
        data = json.loads(match.group(1))
        room_info = (
            data.get("__DEFAULT_SCOPE__", {})
                .get("webapp.user-detail", {})
                .get("userInfo", {})
        )

    live_room = room_info.get("liveRoom", {})
    status = live_room.get("status")
    room_id = live_room.get("roomId") or room_info.get("user", {}).get("roomId")

    return {
        "username": username,
        "is_live": status == 2,
        "status_code": status,
        "room_id": room_id,
    }


def is_live(username: str, cookie_args: list[str]) -> bool:
    """Check if a TikTok user is currently live. Returns False on any
    network/parse error rather than raising, so callers can poll safely."""
    try:
        return check_live_status(username, cookie_args)["is_live"]
    except Exception:
        return False


_QUALITY_WEIGHTS = {"ld": 300, "sd": 400, "hd": 500, "hd_60": 600,
                    "uhd": 700, "uhd_60": 800, "origin": 1000000}
VERIFY_BYTES = 32768      # output must reach this size to count as a working recording
VERIFY_SECONDS = 25       # ...within this many seconds


def candidate_streams(streams: dict) -> list:
    """Every video variant TikTok offers, H.264 first (plays everywhere),
    then HEVC, each group best quality first. Audio-only and aliases skipped."""
    ranked = []
    for name, stream in streams.items():
        codec, _, quality = name.partition("_")
        weight = _QUALITY_WEIGHTS.get(quality)
        if weight is not None and quality != "ao":
            ranked.append((codec == "h264", weight, name, stream))
    ranked.sort(key=lambda r: (r[0], r[1]), reverse=True)
    out = [(n, s) for _, _, n, s in ranked]
    if not out and "best" in streams:
        out = [("best", streams["best"])]
    return out


def _try_stream(name: str, stream, final: str, ffmpeg: str) -> bool:
    """Pipe one stream variant through ffmpeg into `final` (fragmented MP4:
    one file that grows in real time and is playable at every moment, even
    if the process is killed). Returns True once the recording is confirmed
    working (output reached VERIFY_BYTES), False if this variant is unusable.
    """
    fd = ff = None
    verified = False
    total = 0
    try:
        fd = stream.open()
        data = fd.read(65536)
        if not data:
            print("[-] Stream opened but sent no data", flush=True)
            return False
        print(f"[*] First bytes: {data[:16].hex()} ({'FLV' if data[:3] == b'FLV' else 'NOT FLV'})", flush=True)

        tag = ["-tag:v", "hvc1"] if name.startswith("h265") else []
        ff = subprocess.Popen(
            [ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
             "-fflags", "+discardcorrupt",
             "-analyzeduration", "2000000", "-probesize", "2000000",
             "-i", "pipe:0", "-c", "copy"] + tag +
            ["-flush_packets", "1",
             "-movflags", "frag_keyframe+empty_moov+default_base_moof",
             "-f", "mp4", final],
            stdin=subprocess.PIPE,
        )
        started = last_report = time.time()
        while data:
            if ff.poll() is not None:
                print(f"[-] ffmpeg stopped (exit {ff.returncode})", flush=True)
                break
            ff.stdin.write(data)
            ff.stdin.flush()
            total += len(data)
            now = time.time()
            if not verified:
                if os.path.exists(final) and os.path.getsize(final) >= VERIFY_BYTES:
                    verified = True
                    print("[*] Recording confirmed, file is growing", flush=True)
                elif now - started > VERIFY_SECONDS:
                    print(f"[-] No usable video after {VERIFY_SECONDS}s", flush=True)
                    break
            elif now - last_report >= 10:
                print(f"[*] {os.path.getsize(final) / 1048576:.1f} MB on disk", flush=True)
                last_report = now
            data = fd.read(65536)
    except Exception as e:
        print(f"[-] Stream error: {e}", flush=True)
    finally:
        if fd is not None:
            try:
                fd.close()
            except Exception:
                pass
        if ff is not None:
            try:
                ff.stdin.close()
            except Exception:
                pass
            try:
                ff.wait(timeout=30)
            except Exception:
                ff.kill()
    if not verified and os.path.exists(final):
        os.remove(final)
    return verified


def _write_raw_flv(stream, final: str) -> bool:
    """No ffmpeg available: save the stream as-is (.flv), flushed every chunk."""
    total = 0
    try:
        fd = stream.open()
        data = fd.read(65536)
        if not data:
            return False
        with open(final, "wb") as f:
            last_sync = time.time()
            while data:
                f.write(data)
                f.flush()
                total += len(data)
                if time.time() - last_sync >= 2:
                    os.fsync(f.fileno())
                    last_sync = time.time()
                data = fd.read(65536)
        fd.close()
    except Exception as e:
        print(f"[-] Stream error: {e}", flush=True)
    return total > 0


def record(username: str, cookie_args: list[str], output: str, url: str | None = None) -> str | None:
    """Record one live session into ONE file that grows while it records.

    Tries each stream variant TikTok offers (H.264 first, then HEVC) until
    one produces a working recording. Returns the file path, or None.
    """
    import streamlink

    url = url or f"https://www.tiktok.com/@{username}/live"
    session = streamlink.Streamlink()
    session.set_option("stream-timeout", 30)
    session.set_option("http-timeout", 20)
    for kv in (cookie_args[i + 1] for i, a in enumerate(cookie_args) if a == "--http-cookie"):
        key, _, value = kv.partition("=")
        session.http.cookies.set(key, value, domain=".tiktok.com")

    print(f"[*] Resolving stream for @{username}...", flush=True)
    try:
        streams = session.streams(url)
    except Exception as e:
        print(f"[-] Could not resolve stream: {e}", flush=True)
        return None
    candidates = candidate_streams(streams or {})
    if not candidates:
        print("[-] No playable stream (offline, or TikTok refused the request)", flush=True)
        return None
    print(f"[*] Variants: {', '.join(n for n, _ in candidates)}", flush=True)

    base = os.path.splitext(output)[0]
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        print("[!] ffmpeg not found, saving raw .flv (pip install imageio-ffmpeg)", flush=True)
        final = base + ".flv"
        print(f"[*] Recording to {final}", flush=True)
        return final if _write_raw_flv(candidates[0][1], final) else None

    final = base + ".mp4"
    for name, stream in candidates[:6]:
        print(f"[*] Trying {name} -> {final}", flush=True)
        if _try_stream(name, stream, final, ffmpeg):
            print("[*] Recording ended", flush=True)
            return final
    print("[-] No stream variant produced a working recording", flush=True)
    return None


def resolve_cookies(args) -> list[str]:
    """Resolve cookie args from all sources. Exits only if the user
    explicitly pointed at a cookie file that's missing/empty - otherwise
    falls back to an empty list (anonymous/no-cookie recording)."""

    # 1. Explicit --cookies flag
    if args.cookies:
        cookie_file = os.path.abspath(args.cookies)
        if not os.path.exists(cookie_file):
            print(f"[-] Cookie file not found: {cookie_file}")
            sys.exit(1)
        print(f"[*] Using specified cookie file: {cookie_file}")
        cookie_args = load_cookies(cookie_file)
        if not cookie_args:
            print("[-] No TikTok cookies found in the specified file.")
            sys.exit(1)
        return cookie_args

    # 2. Scan project dir for any .json with TikTok cookies
    json_file = find_json_in_project()
    if json_file:
        print(f"[*] Found cookie file: {os.path.basename(json_file)}")
        cookie_args = load_cookies_json(json_file)
        if cookie_args:
            return cookie_args
        print("    [-] No TikTok cookies in JSON, trying next...")

    # 3. Check for tt_cookies.txt
    txt_file = os.path.join(SCRIPT_DIR, "tt_cookies.txt")
    if os.path.exists(txt_file):
        print(f"[*] Found tt_cookies.txt")
        cookie_args = load_cookies_txt(txt_file)
        if cookie_args:
            return cookie_args
        print("    [-] No TikTok cookies in tt_cookies.txt, trying next...")

    # 4. Auto browser detection
    print("[*] No cookie file found, trying browsers...")
    auto_cookie_file = os.path.join(tempfile.gettempdir(), "tt_cookies.txt")
    browser = try_browser_extraction(auto_cookie_file)

    if not browser:
        print("\n[!] Could not find TikTok cookies anywhere.")
        print("    Proceeding WITHOUT cookies - this works fine for public")
        print("    live streams, but may fail for age-restricted/friends-only")
        print("    streams or if TikTok rate-limits anonymous requests.")
        print("\n    To use cookies instead:")
        print("    1. Install 'Cookie-Editor' extension in your browser")
        print("    2. Go to tiktok.com while logged in")
        print("    3. Open Cookie-Editor, click Export -> JSON")
        print(f"    4. Save the file anywhere in: {SCRIPT_DIR}")
        print("    5. Re-run this script - it will find it automatically")
        print("\n    Or specify a file directly:")
        print("    python tiktok-live-downloader.py <username> --cookies <file>")
        return []

    return load_cookies_txt(auto_cookie_file)


def find_ffmpeg() -> str | None:
    """System ffmpeg if present, otherwise the binary bundled with the
    imageio-ffmpeg package (installed into the venv by the launchers)."""
    import shutil
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


def remux_to_mp4(path: str) -> str:
    """TikTok serves FLV. Remux (no re-encode) the finished recording to a
    standard MP4 that plays everywhere, then drop the .flv. Falls back to
    keeping the .flv (still playable in VLC) if ffmpeg is unavailable or
    fails. Works on partial files, e.g. a recording that was stopped."""
    if not path.lower().endswith(".flv") or not os.path.exists(path):
        return path
    if os.path.getsize(path) == 0:
        os.remove(path)
        return path
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        print("[!] ffmpeg not found - keeping .flv (pip install imageio-ffmpeg to auto-convert)")
        return path
    out = path[:-4] + ".mp4"
    tmp = out + ".part"
    try:
        r = subprocess.run(
            [ffmpeg, "-y", "-loglevel", "error", "-fflags", "+discardcorrupt", "-i", path,
             "-c", "copy", "-movflags", "+faststart", "-f", "mp4", tmp],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
        )
    except OSError as e:
        print(f"[-] ffmpeg failed to launch: {e} - keeping .flv")
        return path
    if r.returncode == 0 and os.path.exists(tmp) and os.path.getsize(tmp) > 0:
        os.replace(tmp, out)
        os.remove(path)
        print(f"[*] Converted to {out}")
        return out
    if os.path.exists(tmp):
        os.remove(tmp)
    print(f"[-] Remux failed, keeping .flv: {(r.stderr or '').strip()[-200:]}")
    return path


def remux_dir(out_dir: str) -> None:
    for p in sorted(glob.glob(os.path.join(out_dir, "*.flv"))):
        remux_to_mp4(p)


def make_output_path(username: str, out_dir: str) -> str:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return os.path.join(out_dir, f"{username}_{timestamp}.mp4")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Record a TikTok live stream",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python tiktok-live-downloader.py <username>\n"
            "  python tiktok-live-downloader.py <username> --cookies myfile.json\n"
            "  python tiktok-live-downloader.py <username> --output C:/recordings\n"
            "  python tiktok-live-downloader.py <username> --output C:/recordings --cookies myfile.json\n"
            "  python tiktok-live-downloader.py <username> --daemon\n"
            "  python tiktok-live-downloader.py <username> --daemon --interval 30\n"
        )
    )
    parser.add_argument("username", help="TikTok username to record")
    parser.add_argument(
        "--cookies", "-c",
        metavar="FILE",
        help="Path to a cookies file (.json or .txt). Skips all auto-detection.",
        default=None,
    )
    parser.add_argument(
        "--output", "-o",
        metavar="DIR",
        help="Directory to save recordings (default: ./<username>/). Created if it doesn't exist.",
        default=None,
    )
    parser.add_argument(
        "--daemon", "-d",
        action="store_true",
        help="Daemon mode: keep watching and auto-record whenever the user goes live.",
    )
    parser.add_argument(
        "--interval", "-i",
        metavar="SECONDS",
        type=int,
        default=60,
        help="How often to check if the user is live in daemon mode (default: 60).",
    )
    args = parser.parse_args()

    username = args.username
    out_dir = os.path.abspath(args.output) if args.output else os.path.join(os.getcwd(), username)
    os.makedirs(out_dir, exist_ok=True)

    cookie_args = resolve_cookies(args)

    if args.daemon:
        print(f"[*] Daemon mode - watching @{username} every {args.interval}s. Press Ctrl+C to stop.")
        try:
            while True:
                print(f"[{datetime.now().strftime('%H:%M:%S')}] Checking if @{username} is live...")
                if is_live(username, cookie_args):
                    print(f"[*] @{username} is live!")
                    output = make_output_path(username, out_dir)
                    print(f"[*] Saving to {output}")
                    path = record(username, cookie_args, output)
                    if path:
                        remux_to_mp4(path)
                    print(f"[*] Stream ended. Resuming watch in {args.interval}s...")
                else:
                    print(f"    Not live. Checking again in {args.interval}s...")
                time.sleep(args.interval)
        except KeyboardInterrupt:
            print("\n[*] Daemon stopped.")
    else:
        output = make_output_path(username, out_dir)
        print(f"[*] Saving to {output}")
        path = record(username, cookie_args, output)
        if path:
            remux_to_mp4(path)


if __name__ == "__main__":
    main()