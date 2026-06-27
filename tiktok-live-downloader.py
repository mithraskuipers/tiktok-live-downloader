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
import glob
import argparse
import time
import urllib.request
import urllib.error
from datetime import datetime


BROWSERS = ["chrome", "firefox", "edge", "brave", "chromium", "opera", "vivaldi"]
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))


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


def is_live(username: str, cookie_args: list[str]) -> bool:
    """Check if a TikTok user is currently live."""
    try:
        result = subprocess.run(
            [sys.executable, "-m", "streamlink",
             "--url", f"https://www.tiktok.com/@{username}/live"]
            + cookie_args,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=20,
        )
        output = result.stdout.decode(errors="ignore") + result.stderr.decode(errors="ignore")
        return result.returncode == 0 and "No playable streams" not in output
    except Exception:
        return False


def record(username: str, cookie_args: list[str], output: str) -> None:
    print(f"[*] Recording @{username} to {output} - press q to stop")
    cmd = (
        [sys.executable, "-m", "streamlink"]
        + cookie_args
        + ["--output", output, f"https://www.tiktok.com/@{username}/live", "best"]
    )
    subprocess.run(cmd)


def resolve_cookies(args) -> list[str]:
    """Resolve cookie args from all sources. Exits on failure."""

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
        print("\n[-] Could not extract TikTok cookies automatically.")
        print("\n    Recommended fix:")
        print("    1. Install 'Cookie-Editor' extension in your browser")
        print("    2. Go to tiktok.com while logged in")
        print("    3. Open Cookie-Editor, click Export -> JSON")
        print(f"    4. Save the file anywhere in: {SCRIPT_DIR}")
        print("    5. Re-run this script - it will find it automatically")
        print("\n    Or specify a file directly:")
        print("    python tiktok-live-downloader.py <username> --cookies <file>")
        sys.exit(1)

    return load_cookies_txt(auto_cookie_file)


def make_output_path(username: str, out_dir: str) -> str:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return os.path.join(out_dir, f"{username}_{timestamp}.ts")


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
                    record(username, cookie_args, output)
                    print(f"[*] Stream ended. Resuming watch in {args.interval}s...")
                else:
                    print(f"    Not live. Checking again in {args.interval}s...")
                time.sleep(args.interval)
        except KeyboardInterrupt:
            print("\n[*] Daemon stopped.")
    else:
        output = make_output_path(username, out_dir)
        print(f"[*] Saving to {output}")
        record(username, cookie_args, output)


if __name__ == "__main__":
    main()
