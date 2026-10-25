#!/usr/bin/env python3
"""
TikTok Live Downloader - Web UI
Usage: python3 ui.py <runtime_dir> <start_port> <user1> [user2 ...]
"""

import sys
import os
import time
import signal
import socket
import subprocess
import threading
import re
import secrets
import hmac
from flask import Flask, jsonify, request, redirect, session, abort

# ---------------------------------------------------------------------------
# Args
#
# NOTE: worker subprocesses re-execute this entire file via
# exec(open(__file__).read()) and then call _worker_main(...) directly.
# In that context sys.argv is just ['-c'], so this block must be guarded
# to avoid an IndexError that silently kills every worker before it starts.
# ---------------------------------------------------------------------------
if len(sys.argv) > 1:
    runtime_dir    = sys.argv[1]
    start_port     = int(sys.argv[2])
    initial_users  = [u.lower() for u in sys.argv[3:]]
else:
    runtime_dir   = None
    start_port    = None
    initial_users = []

status_dir = os.path.join(runtime_dir, "status") if runtime_dir else None
paused_dir = os.path.join(runtime_dir, "paused") if runtime_dir else None
pid_dir    = os.path.join(runtime_dir, "pids") if runtime_dir else None
log_dir    = os.path.join(runtime_dir, "logs") if runtime_dir else None

try:
    SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
except NameError:
    # __file__ doesn't exist when this code runs via exec(open(...).read())
    # inside a worker subprocess (started with `python -c ...`). In that
    # case sys.argv[0] is '-c', so fall back to the cwd (the worker
    # subprocess is spawned with the same SCRIPT_DIR already known by the
    # parent, but this module-level line still runs during the re-exec).
    SCRIPT_DIR = os.getcwd()
USERS_FILE        = os.path.join(SCRIPT_DIR, "users.txt")
DOWNLOADER        = os.path.join(SCRIPT_DIR, "tiktok-live-downloader.py")
SAVE_LOCATION_FILE = os.path.join(SCRIPT_DIR, "save_location.txt")
PASSWORD_FILE      = os.path.join(SCRIPT_DIR, "password.txt")
SECRET_KEY_FILE    = os.path.join(SCRIPT_DIR, ".flask_secret")
LOGIN_FILE         = os.path.join(SCRIPT_DIR, "login.html")
HTML_FILE          = os.path.join(SCRIPT_DIR, "dashboard.html")


def _get_password():
    try:
        pw = open(PASSWORD_FILE).read().strip()
        if pw:
            return pw
    except Exception:
        pass
    pw = secrets.token_urlsafe(12)
    try:
        with open(PASSWORD_FILE, "w") as f:
            f.write(pw + "\n")
        os.chmod(PASSWORD_FILE, 0o600)
    except Exception:
        pass
    print("")
    print("  ====================================================")
    print(f"  No password.txt found — generated one for you:")
    print(f"  Password: {pw}")
    print(f"  (saved to {PASSWORD_FILE} — edit it any time)")
    print("  ====================================================")
    print("")
    return pw


def _get_secret_key():
    try:
        key = open(SECRET_KEY_FILE).read().strip()
        if key:
            return key
    except Exception:
        pass
    key = secrets.token_hex(32)
    try:
        with open(SECRET_KEY_FILE, "w") as f:
            f.write(key)
        os.chmod(SECRET_KEY_FILE, 0o600)
    except Exception:
        pass
    return key


def _get_download_dir():
    try:
        return open(SAVE_LOCATION_FILE).read().strip()
    except Exception:
        return os.environ.get("DOWNLOAD_DIR", "")


def _fmt_bytes(b):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if b < 1024:
            return f"{b:.1f} {unit}"
        b /= 1024
    return f"{b:.1f} PB"


def get_disk_info():
    path = _get_download_dir()
    if not path or not os.path.isdir(path):
        return {"ok": False, "path": path or "(not set)"}
    try:
        # shutil.disk_usage works on both Windows and POSIX,
        # unlike os.statvfs which is POSIX-only.
        import shutil
        total, used, free = shutil.disk_usage(path)
        pct = round(used / total * 100, 1) if total else 0
        return {
            "ok":    True,
            "path":  path,
            "total": _fmt_bytes(total),
            "used":  _fmt_bytes(used),
            "free":  _fmt_bytes(free),
            "pct":   pct,
        }
    except Exception as e:
        return {"ok": False, "path": path, "error": str(e)}


# Mutable in-process state
_lock       = threading.Lock()
_users      = list(initial_users)
_worker_pids = {}


# ---------------------------------------------------------------------------
# Status / pause helpers
# ---------------------------------------------------------------------------
def get_status(user):
    try:
        raw   = open(os.path.join(status_dir, f"{user}.status")).read().strip()
        parts = raw.split("|")
        st    = parts[0] if parts else "?"
        det   = parts[1] if len(parts) > 1 else ""
        ts    = int(parts[2]) if len(parts) > 2 else int(time.time())
        return st, det, ts
    except Exception:
        return "STARTING", "", int(time.time())


def is_paused(user):
    return os.path.exists(os.path.join(paused_dir, f"{user}.paused"))


def _get_worker_pid(user):
    try:
        return int(open(os.path.join(pid_dir, f"{user}.pid")).read().strip())
    except Exception:
        return None


def _children_of(pid):
    # POSIX only. Windows uses taskkill /T instead (wmic is removed from
    # current Windows 11 builds, which silently broke child discovery).
    try:
        out = subprocess.check_output(
            ["pgrep", "-P", str(pid)], text=True,
            stderr=subprocess.DEVNULL, timeout=5
        ).split()
        return [int(p) for p in out if p.strip()]
    except Exception:
        return []


def _descendants(pid):
    found, stack = [], [pid]
    while stack:
        for c in _children_of(stack.pop()):
            if c not in found:
                found.append(c)
                stack.append(c)
    return found


def _kill_tree(pid):
    """Kill pid and everything under it. On POSIX the tree is snapshotted
    first, because children get re-parented as soon as their parent dies."""
    if not pid or pid == os.getpid():
        return
    try:
        if os.name == "nt":
            subprocess.call(["taskkill", "/F", "/T", "/PID", str(pid)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            timeout=10)
        else:
            victims = _descendants(pid)
            for p in [pid] + victims:
                try:
                    os.kill(p, signal.SIGTERM)
                except Exception:
                    pass
    except Exception:
        pass


def _worker_alive(user):
    wpid = _get_worker_pid(user)
    if wpid is None:
        return False
    if os.name == "nt":
        # os.kill(pid, 0) is not a liveness probe on Windows.
        import ctypes
        k32 = ctypes.windll.kernel32
        h = k32.OpenProcess(0x1000, False, wpid)  # QUERY_LIMITED_INFORMATION
        if not h:
            return False
        code = ctypes.c_ulong()
        ok = k32.GetExitCodeProcess(h, ctypes.byref(code))
        k32.CloseHandle(h)
        return bool(ok) and code.value == 259  # STILL_ACTIVE
    try:
        # Reap it if it is our own exited child, so it doesn't look alive as a zombie.
        if os.waitpid(wpid, os.WNOHANG)[0] == wpid:
            return False
    except ChildProcessError:
        pass
    try:
        os.kill(wpid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def _set_status(user, state):
    try:
        with open(os.path.join(status_dir, f"{user}.status"), "w") as f:
            f.write(f"{state}||{int(time.time())}\n")
    except Exception:
        pass


def toggle_pause(user):
    pf = os.path.join(paused_dir, f"{user}.paused")
    if is_paused(user):
        try:
            os.remove(pf)
        except Exception:
            pass
        if not _worker_alive(user):
            pid = _spawn_worker(user)
            with _lock:
                _worker_pids[user] = pid
        else:
            _set_status(user, "STARTING")
    else:
        open(pf, "w").close()
        # The worker sees the flag within ~1s and stops its own recording.
        _set_status(user, "PAUSED")


def elapsed(ts):
    d = int(time.time()) - ts
    if d < 60:
        return f"{d}s"
    if d < 3600:
        return f"{d // 60}m {d % 60:02d}s"
    return f"{d // 3600}h {(d % 3600) // 60:02d}m"


def find_free_port(start):
    for port in range(start, start + 100):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(("0.0.0.0", port))
                return port
            except OSError:
                continue
    raise RuntimeError(f"No free port found in range {start}-{start + 99}")


# ---------------------------------------------------------------------------
# users.txt helpers
# ---------------------------------------------------------------------------
def _read_users_file():
    try:
        return open(USERS_FILE).readlines()
    except FileNotFoundError:
        return []


def _write_users_file(lines):
    with open(USERS_FILE, "w") as f:
        f.writelines(lines)


def users_file_add(user):
    lines = _read_users_file()
    existing = {l.strip().lower() for l in lines
                if l.strip() and not l.strip().startswith("#")}
    if user in existing:
        return False
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"
    lines.append(user + "\n")
    _write_users_file(lines)
    return True


def users_file_remove(user):
    lines = _read_users_file()
    new_lines = [l for l in lines
                 if l.strip().startswith("#") or
                    l.strip() == "" or
                    l.strip().lower() != user]
    _write_users_file(new_lines)


# ---------------------------------------------------------------------------
# Worker management
# ---------------------------------------------------------------------------

def _worker_main(user, status_file, pid_file, paused_file, log_file,
                 download_dir, sleep_interval, downloader):
    """
    Pure-Python worker: runs in a subprocess, polls TikTok live status,
    records when live, writes status files in the cbdaemon convention.
    Cross-platform (Windows + Linux/macOS).
    """
    import os, sys, time, subprocess
    from datetime import datetime

    # Write our own PID so the UI can signal us
    with open(pid_file, "w") as f:
        f.write(str(os.getpid()) + "\n")

    def set_st(state, detail=""):
        with open(status_file, "w") as f:
            f.write(f"{state}|{detail}|{int(time.time())}\n")

    def is_paused():
        return os.path.exists(paused_file)

    # Load the downloader script as a module once, so we can reuse its
    # cookie-loading helpers *and* its check_live_status()/is_live()
    # HTTP-based check (reads the room status straight out of the TikTok
    # live page, same as the browser does) instead of duplicating logic.
    def _load_downloader_module():
        try:
            import importlib.util
            spec = importlib.util.spec_from_file_location("_tld_mod", downloader)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            return mod
        except Exception:
            return None

    _dl_mod = _load_downloader_module()

    def _load_cookie_args():
        if _dl_mod is None:
            return []
        try:
            json_file = _dl_mod.find_json_in_project()
            if json_file:
                args = _dl_mod.load_cookies_json(json_file)
                if args:
                    return args

            txt_file = os.path.join(os.path.dirname(downloader), "tt_cookies.txt")
            if os.path.exists(txt_file):
                args = _dl_mod.load_cookies_txt(txt_file)
                if args:
                    return args
        except Exception:
            pass
        return []

    cookie_args = _load_cookie_args()

    def is_live():
        if _dl_mod is None:
            return False
        try:
            return _dl_mod.is_live(user, cookie_args)
        except Exception:
            return False

    # "Stop DL" flag lives next to the pause flag
    stop_file = os.path.join(os.path.dirname(paused_file), f"{user}.stop")

    def _rm(path):
        try:
            os.remove(path)
        except OSError:
            pass

    def kill_tree(proc):
        try:
            if os.name == "nt":
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               timeout=10)
            else:
                import signal
                try:
                    os.killpg(proc.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(proc.pid, signal.SIGKILL)
        except Exception:
            pass
        try:
            proc.wait(timeout=5)
        except Exception:
            pass

    def record(out_dir):
        _rm(stop_file)
        kw = {} if os.name == "nt" else {"start_new_session": True}
        with open(log_file, "a") as lf:
            proc = subprocess.Popen(
                [sys.executable, downloader, user, "--output", out_dir],
                stdout=lf, stderr=subprocess.STDOUT, **kw
            )
            while proc.poll() is None:
                if is_paused() or os.path.exists(stop_file):
                    kill_tree(proc)
                    break
                time.sleep(1)
        _rm(stop_file)

    set_st("STARTING")

    while True:
        if is_paused():
            set_st("PAUSED")
            while is_paused():
                time.sleep(1)
            set_st("STARTING")
            continue

        set_st("CHECKING")

        try:
            live = is_live()
        except Exception:
            set_st("ERROR", "live check failed")
            live = False

        # Paused while the live check was in flight: don't start recording.
        if live and not is_paused():
            ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
            out_dir = os.path.join(download_dir, user)
            os.makedirs(out_dir, exist_ok=True)
            set_st("DOWNLOADING", ts)
            try:
                record(out_dir)
            except Exception:
                pass
            # A pause/stop kills the downloader before it can convert, so do it here.
            try:
                if _dl_mod is not None:
                    _dl_mod.remux_dir(out_dir)
            except Exception:
                pass

        # Offline countdown (1s ticks so pause reacts quickly)
        elapsed = 0
        while elapsed < sleep_interval:
            if is_paused():
                break
            if elapsed % 5 == 0:
                next_check = int(time.time()) + (sleep_interval - elapsed)
                with open(status_file, "w") as f:
                    f.write(f"OFFLINE|{next_check}|{int(time.time())}\n")
            time.sleep(1)
            elapsed += 1

def _spawn_worker(user):
    status_file  = os.path.join(status_dir, f"{user}.status")
    pid_file     = os.path.join(pid_dir,    f"{user}.pid")
    paused_file  = os.path.join(paused_dir, f"{user}.paused")
    log_file     = os.path.join(log_dir,    f"{user}.log")
    download_dir = _get_download_dir() or os.path.join(SCRIPT_DIR, user)
    sleep_interval = int(os.environ.get("SLEEP_INTERVAL", "60"))

    with open(status_file, "w") as f:
        f.write(f"STARTING||{int(time.time())}\n")

    # Spawn _worker_main in a fresh Python interpreter so it's truly
    # independent (and killable) on both Windows and Linux.
    worker_code = (
        "import sys, os; sys.path.insert(0, r'{sd}');"
        "exec(open(r'{me}').read());"
        "_worker_main({user!r},{sf!r},{pf!r},{paus!r},{lf!r},{dl!r},{si},{dn!r})"
    ).format(
        sd   = SCRIPT_DIR,
        me   = os.path.abspath(__file__),
        user = user,
        sf   = status_file,
        pf   = pid_file,
        paus = paused_file,
        lf   = log_file,
        dl   = download_dir,
        si   = sleep_interval,
        dn   = DOWNLOADER,
    )

    with open(log_file, "a") as lf:
        proc = subprocess.Popen(
            [sys.executable, "-c", worker_code],
            stdout=lf, stderr=lf,
            # Windows: don't keep console window open
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
    return proc.pid


def _kill_worker(user):
    pid_file = os.path.join(pid_dir, f"{user}.pid")
    wpid = _get_worker_pid(user)

    with _lock:
        stored_pid = _worker_pids.pop(user, None)

    for root in filter(None, {wpid, stored_pid}):
        _kill_tree(root)

    for path in [
        pid_file,
        os.path.join(status_dir, f"{user}.status"),
        os.path.join(paused_dir, f"{user}.paused"),
        os.path.join(paused_dir, f"{user}.stop"),
    ]:
        try:
            os.remove(path)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Flask app
# ---------------------------------------------------------------------------
app = Flask(__name__)
app.logger.disabled = True
import logging
log = logging.getLogger("werkzeug")
log.setLevel(logging.ERROR)

app.secret_key = _get_secret_key()
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.permanent_session_lifetime = 60 * 60 * 24 * 30   # 30 days

_PASSWORD = _get_password()


@app.before_request
def _require_login():
    if request.path in ("/login", "/logout"):
        return None
    if request.path.startswith("/static/"):
        return None
    if not session.get("authed"):
        if request.path.startswith("/api/"):
            abort(401)
        return redirect("/login")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        pw = request.form.get("password", "")
        if hmac.compare_digest(pw, _PASSWORD):
            session.permanent = True
            session["authed"] = True
            return redirect("/")
        with open(LOGIN_FILE, "r", encoding="utf-8") as f:
            page = f.read()
        return page.replace("{{error}}", "Incorrect password"), 401
    with open(LOGIN_FILE, "r", encoding="utf-8") as f:
        page = f.read()
    return page.replace("{{error}}", "")


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect("/login")


@app.route("/")
def index():
    with open(HTML_FILE, "r", encoding="utf-8") as f:
        return f.read()


@app.route("/api/status")
def api_status():
    with _lock:
        current = list(_users)
    out = []
    now = int(time.time())
    for u in current:
        st, det, ts = get_status(u)
        entry = {
            "user":    u,
            "status":  st,
            "since":   det,
            "elapsed": elapsed(ts),
        }
        if st == "OFFLINE" and det:
            try:
                entry["next_check_in"] = max(0, int(det) - now)
            except ValueError:
                pass
        out.append(entry)
    return jsonify(out)


@app.route("/api/disk")
def api_disk():
    return jsonify(get_disk_info())


@app.route("/api/toggle/<user>", methods=["POST"])
def api_toggle(user):
    with _lock:
        if user not in _users:
            return jsonify({"ok": False, "error": "Unknown user"}), 404
    toggle_pause(user)
    return jsonify({"ok": True})


@app.route("/api/stop/<user>", methods=["POST"])
def api_stop(user):
    with _lock:
        if user not in _users:
            return jsonify({"ok": False, "error": "Unknown user"}), 404
    st, _, _ = get_status(user)
    if st == "DOWNLOADING":
        # The worker owns the downloader process and stops it within ~1s.
        open(os.path.join(paused_dir, f"{user}.stop"), "w").close()
    return jsonify({"ok": True})


@app.route("/api/users/add", methods=["POST"])
def api_add_user():
    data = request.get_json(force=True)
    user = (data.get("user") or "").strip().lower()

    if not user or not re.match(r'^[a-z0-9_.]+$', user):
        return jsonify({"ok": False, "error": "Invalid username"}), 400

    with _lock:
        if user in _users:
            return jsonify({"ok": False, "error": "User already monitored"}), 409
        _users.append(user)

    users_file_add(user)
    pid = _spawn_worker(user)
    with _lock:
        _worker_pids[user] = pid

    return jsonify({"ok": True})


@app.route("/api/users/remove", methods=["POST"])
def api_remove_user():
    data = request.get_json(force=True)
    user = (data.get("user") or "").strip().lower()

    with _lock:
        if user not in _users:
            return jsonify({"ok": False, "error": "Unknown user"}), 404
        st, _, _ = get_status(user)
        if st != "PAUSED":
            return jsonify({"ok": False, "error": "Pause first, then remove."}), 409
        _users.remove(user)

    _kill_worker(user)
    users_file_remove(user)
    return jsonify({"ok": True})


@app.route("/api/shutdown", methods=["POST"])
def api_shutdown():
    with _lock:
        current = list(_users)

    for u in current:
        _kill_tree(_get_worker_pid(u))
        _set_status(u, "STOPPED")

    def _do_exit():
        time.sleep(1.2)
        os._exit(0)

    threading.Thread(target=_do_exit, daemon=True).start()
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__" and runtime_dir is not None:
    # The "and runtime_dir is not None" guard matters: worker subprocesses
    # re-execute this whole file via exec(open(__file__).read()) and their
    # __name__ is also "__main__" (since they're launched as `python -c`).
    # Without this guard they would start a second Flask server here and
    # app.run() would block forever, so _worker_main(...) (called right
    # after the exec() in _spawn_worker) would never actually run.
    os.makedirs(status_dir, exist_ok=True)
    os.makedirs(paused_dir, exist_ok=True)
    os.makedirs(pid_dir,    exist_ok=True)
    os.makedirs(log_dir,    exist_ok=True)

    # Spawn a worker for each initial user
    for user in initial_users:
        sf = os.path.join(status_dir, f"{user}.status")
        with open(sf, "w") as f:
            f.write(f"STARTING||{int(time.time())}\n")
        pid = _spawn_worker(user)
        with _lock:
            _worker_pids[user] = pid

    port = find_free_port(start_port)
    if port != start_port:
        print(f"  Port {start_port} in use — using port {port} instead.")
    print(f"\n  Web UI -> http://localhost:{port}\n")
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)
