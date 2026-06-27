# tiktok-live-downloader

Record TikTok live streams from the command line. Supports one-shot recording and daemon mode (auto-watch and record whenever a user goes live).

## Dependencies

- **Python 3.8+**
- **ffmpeg** - must be on PATH
- **yt-dlp** and **streamlink**

```bash
pip install yt-dlp streamlink --upgrade
# On Linux with system Python:
pip install yt-dlp streamlink --upgrade --break-system-packages
```

> The script calls streamlink via `python -m streamlink` so it works even if the `streamlink` command isn't on your PATH.

## Usage

```bash
python tiktok-live-downloader.py <username> [--cookies <file>] [--output <dir>] [--daemon] [--interval <seconds>]
```

By default, recordings are saved to a subfolder named after the user (e.g. `./<username>/<username>_20260627_183204.ts`). Use `--output` to save somewhere else.

```bash
# Record once (user must already be live)
python tiktok-live-downloader.py <username>

# Custom output directory
python tiktok-live-downloader.py <username> --output C:/recordings

# Custom directory + explicit cookies
python tiktok-live-downloader.py <username> --output C:/recordings --cookies myfile.json
```

Output is saved as `<username>_<timestamp>.ts`. Press `q` to stop.

## Daemon mode

Daemon mode keeps running in the background, checking every 60 seconds (customizable) whether the user is live. When they go live it starts recording automatically, and when the stream ends it goes back to watching.

```bash
# Watch and auto-record, check every 60s (default)
python tiktok-live-downloader.py <username> --daemon

# Custom check interval (seconds)
python tiktok-live-downloader.py <username> --daemon --interval 30

# Daemon + custom output dir (recordings saved to C:/recordings/<username>/)
python tiktok-live-downloader.py <username> --daemon --output C:/recordings

# Daemon + custom output dir + custom interval (e.g. check every 5 minutes)
python tiktok-live-downloader.py <username> --daemon --interval 300 --output C:/recordings
```

Press `Ctrl+C` to stop the daemon.

## Quickest setup - export cookies manually

Browser auto-detection is slow and often fails (especially on Chrome 127+). The fastest way is to export cookies yourself:

1. Install **Cookie-Editor** in your browser
   - [Chrome](https://chrome.google.com/webstore/detail/cookie-editor/hlkenndednhfkekhgcdicdfddnkalyna) / [Firefox](https://addons.mozilla.org/en-US/firefox/addon/cookie-editor/)
2. Go to **tiktok.com** while logged in
3. Open Cookie-Editor -> **Export -> JSON**
4. Save as **`tt_cookies.json`** in the same folder as `tiktok-live-downloader.py`

The script will find it automatically - no flags needed. Re-export if cookies expire.

## Cookie resolution order

1. `--cookies <file>` - explicit file, skips everything else
2. Any `.json` file with TikTok cookies in the script's folder
3. `tt_cookies.txt` in the script's folder
4. Browser auto-detection (slow - skipped if any of the above are found)

## Convert output to mp4

```bash
ffmpeg -i file.ts -c copy file.mp4
```

## Notes

- `tt_cookies.json` and `tt_cookies.txt` are in `.gitignore` and will never be committed
- In normal mode, the user must already be live when you run the script
- In daemon mode, the script handles going live automatically
