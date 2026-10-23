"""
YouTube Video Downloader — Flask web app (yt-dlp backend).

Features:
    * Single-download-at-a-time queue (global state guarded by a lock).
    * YouTube authentication via an uploaded cookies.txt (Netscape format).
    * Quality selector (Best / 1080p / 720p / 480p), merged to MP4.
    * Live progress via Server-Sent Events.
    * /health + /health/json diagnostics, incl. cookie-age expiry warning.
    * Branding is fully driven by brand/config.json — the entire codebase is
      identical between forks; only brand/config.json + brand/logo.png differ.

Routes:
    /                 URL input (redirects to /setup if not authorized)
    /start   (POST)   start a download
    /progress         live progress page
    /progress-stream  SSE progress stream
    /download         download-ready page
    /get-file         serve the finished file
    /reset   (POST)   start over (deletes file + state)
    /api/status       JSON status (busy-page polling)
    /setup   (GET)    first-run cookies upload page
    /setup   (POST)   receive cookies.txt
    /settings         settings (update cookies, remove & re-setup)
    /settings/reauth (POST)  delete cookies.txt -> /setup
    /settings/cookies (POST) upload/replace cookies.txt
    /health           diagnostics page
    /health/json      diagnostics JSON
    /brand-logo       serves brand/logo.png if present
"""

import os
import re
import sys
import json
import time
import glob
import shutil
import subprocess
import threading

from flask import (
    Flask,
    render_template,
    request,
    redirect,
    url_for,
    Response,
    send_file,
    jsonify,
    abort,
)

import yt_dlp

app = Flask(__name__)

# ---------------------------------------------------------------------------
# Paths / configuration
# ---------------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
BRAND_DIR = os.path.join(BASE_DIR, "brand")
BRAND_CONFIG_PATH = os.path.join(BRAND_DIR, "config.json")
BRAND_LOGO_PATH = os.path.join(BRAND_DIR, "logo.png")

# Persistent config volume (cookies.txt lives here).
CONFIG_DIR = os.environ.get("YTDLP_CONFIG_DIR", "/config")
COOKIES_PATH = os.path.join(CONFIG_DIR, "cookies.txt")

DOWNLOAD_DIR = "/tmp/ytdlp_downloads"
OUTPUT_TEMPLATE = os.path.join(DOWNLOAD_DIR, "download.%(ext)s")

# YouTube cookies typically expire after 2–4 weeks; warn the user once the file
# is this many days old so they can refresh before downloads start failing.
COOKIE_WARN_DAYS = 12

START_TIME = time.time()

os.makedirs(DOWNLOAD_DIR, exist_ok=True)
try:
    os.makedirs(CONFIG_DIR, exist_ok=True)
except OSError:
    # /config may be a read-only mount in some setups; handled gracefully later.
    pass

# ---------------------------------------------------------------------------
# Quality -> yt-dlp format string
# ---------------------------------------------------------------------------
QUALITY_FORMATS = {
    # Each quality level cascades: try the target height, then relax height,
    # then fall back to any bestvideo+bestaudio, then any single best stream.
    "best":  "bestvideo+bestaudio/best",
    "1080p": "bestvideo[height<=1080]+bestaudio/bestvideo+bestaudio/best",
    "720p":  "bestvideo[height<=720]+bestaudio/bestvideo[height<=1080]+bestaudio/bestvideo+bestaudio/best",
    "480p":  "bestvideo[height<=480]+bestaudio/bestvideo[height<=720]+bestaudio/bestvideo+bestaudio/best",
}
QUALITY_LABELS = [
    ("best", "Best Available"),
    ("1080p", "1080p"),
    ("720p", "720p"),
    ("480p", "480p"),
]

# ---------------------------------------------------------------------------
# Branding
# ---------------------------------------------------------------------------
DEFAULT_BRAND = {
    "name": "Video Downloader",
    "tagline": "Simple YouTube Video Downloader",
    "color_primary": "#6366F1",
    "color_dark": "#1e1b4b",
    "has_logo": False,
}


def load_brand():
    """Read brand/config.json, falling back to sensible defaults."""
    brand = dict(DEFAULT_BRAND)
    try:
        with open(BRAND_CONFIG_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            brand.update({k: v for k, v in data.items() if v is not None})
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass
    # has_logo is only true if the flag is set AND the file actually exists.
    brand["has_logo"] = bool(brand.get("has_logo")) and os.path.isfile(BRAND_LOGO_PATH)
    return brand


BRAND = load_brand()


@app.context_processor
def inject_brand():
    """Make `brand` available in every template."""
    return {"brand": BRAND}


# ---------------------------------------------------------------------------
# Download state (single download at a time)
# ---------------------------------------------------------------------------
state_lock = threading.Lock()


def _fresh_state():
    return {
        "status": "idle",   # idle | downloading | finished | error
        "progress": 0.0,
        "speed": "",
        "eta": "",
        "title": "",
        "filepath": "",
        "error": "",
        "last_title": "",       # persists across resets for /health
        "last_finished": None,  # epoch seconds
    }


state = _fresh_state()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _strip_ansi(text):
    if not text:
        return ""
    return re.sub(r"\x1b\[[0-9;]*m", "", str(text)).strip()


def clean_downloads():
    """Remove every file in the download directory."""
    try:
        for name in os.listdir(DOWNLOAD_DIR):
            path = os.path.join(DOWNLOAD_DIR, name)
            try:
                if os.path.isfile(path) or os.path.islink(path):
                    os.remove(path)
                elif os.path.isdir(path):
                    shutil.rmtree(path, ignore_errors=True)
            except OSError:
                pass
    except FileNotFoundError:
        os.makedirs(DOWNLOAD_DIR, exist_ok=True)


def has_cookies():
    """True if a non-empty cookies.txt exists."""
    try:
        return os.path.isfile(COOKIES_PATH) and os.path.getsize(COOKIES_PATH) > 0
    except OSError:
        return False


def is_authorized():
    """Authorized if a non-empty cookies.txt exists."""
    return has_cookies()


def _looks_like_cookies(text):
    """Loose validation that an uploaded file is a Netscape cookies.txt."""
    if not text:
        return False
    if "# Netscape HTTP Cookie File" in text or "# HTTP Cookie File" in text:
        return True
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if len(line.split("\t")) >= 7:
            return True
    return False


def _cookie_age_days():
    """Age of cookies.txt in whole days, or None if absent/unknown."""
    if not has_cookies():
        return None
    try:
        age = time.time() - os.path.getmtime(COOKIES_PATH)
        return int(age / 86400)
    except OSError:
        return None


def _format_eta(seconds):
    if seconds is None:
        return ""
    try:
        seconds = int(seconds)
    except (TypeError, ValueError):
        return ""
    if seconds < 0:
        return ""
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h:d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def _format_speed(speed):
    if not speed:
        return ""
    try:
        speed = float(speed)
    except (TypeError, ValueError):
        return ""
    units = ["B/s", "KiB/s", "MiB/s", "GiB/s"]
    idx = 0
    while speed >= 1024 and idx < len(units) - 1:
        speed /= 1024.0
        idx += 1
    return f"{speed:.1f}{units[idx]}"


def _format_bytes(num):
    if num is None:
        return "unknown"
    try:
        num = float(num)
    except (TypeError, ValueError):
        return "unknown"
    units = ["B", "KiB", "MiB", "GiB", "TiB"]
    idx = 0
    while num >= 1024 and idx < len(units) - 1:
        num /= 1024.0
        idx += 1
    return f"{num:.1f} {units[idx]}"


def _base_ydl_opts():
    """Common yt-dlp options shared by all downloads."""
    opts = {
        "cachedir": CONFIG_DIR,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        # Avoid the broken tv/tv_downgraded client.
        "extractor_args": {"youtube": {"player_client": ["default", "web_embedded"]}},
    }
    if has_cookies():
        opts["cookiefile"] = COOKIES_PATH
    return opts


def friendly_error(raw):
    """Map common yt-dlp error strings to user-friendly messages."""
    text = _strip_ansi(raw)
    low = text.lower()
    if "requested format is not available" in low:
        return (
            "The selected quality isn't available for this video. "
            "Try a lower quality or 'Best Available'."
        )
    if "page needs to be reloaded" in low:
        return "YouTube session issue. Upload fresh cookies.txt in Settings."
    if "private video" in low:
        return "This video is private and cannot be downloaded."
    if "video unavailable" in low:
        return "This video is unavailable in your region or has been removed."
    if "sign in" in low or "requires authentication" in low or "login required" in low:
        return "This video requires sign-in. Upload fresh cookies.txt in Settings."
    return text or "Download failed."


# ---------------------------------------------------------------------------
# Progress hook
# ---------------------------------------------------------------------------
def progress_hook(d):
    status = d.get("status")
    if status == "downloading":
        total = d.get("total_bytes") or d.get("total_bytes_estimate")
        downloaded = d.get("downloaded_bytes") or 0
        percent = 0.0
        if total:
            percent = max(0.0, min(100.0, downloaded / total * 100.0))
        else:
            raw = _strip_ansi(d.get("_percent_str", ""))
            m = re.search(r"([\d.]+)%", raw)
            if m:
                try:
                    percent = float(m.group(1))
                except ValueError:
                    percent = 0.0
        with state_lock:
            state["status"] = "downloading"
            state["progress"] = round(percent, 1)
            state["speed"] = _format_speed(d.get("speed"))
            state["eta"] = _format_eta(d.get("eta"))
    elif status == "finished":
        with state_lock:
            state["status"] = "downloading"
            state["progress"] = 99.0
            state["speed"] = ""
            state["eta"] = ""


def _find_downloaded_file():
    if not os.path.isdir(DOWNLOAD_DIR):
        return None
    candidates = [
        os.path.join(DOWNLOAD_DIR, n)
        for n in os.listdir(DOWNLOAD_DIR)
        if n.startswith("download.")
    ]
    if not candidates:
        return None
    for c in candidates:
        if c.endswith(".mp4"):
            return c
    return candidates[0]


def _safe_filename(title, ext):
    title = _strip_ansi(title) or "video"
    title = re.sub(r'[\\/:*?"<>|]+', "_", title).strip()
    if not title:
        title = "video"
    return f"{title}.{ext.lstrip('.')}"


def download_worker(url, quality):
    fmt = QUALITY_FORMATS.get(quality, QUALITY_FORMATS["best"])
    ydl_opts = _base_ydl_opts()
    ydl_opts.update(
        {
            "format": fmt,
            "format_sort": ["vcodec:h264", "acodec:aac", "res", "fps"],
            "merge_output_format": "mp4",
            "outtmpl": OUTPUT_TEMPLATE,
            "progress_hooks": [progress_hook],
            "restrictfilenames": False,
        }
    )

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)

        title = ""
        if isinstance(info, dict):
            title = info.get("title", "") or ""

        filepath = _find_downloaded_file()
        with state_lock:
            state["title"] = _strip_ansi(title)
            if filepath and os.path.isfile(filepath):
                state["status"] = "finished"
                state["progress"] = 100.0
                state["speed"] = ""
                state["eta"] = ""
                state["filepath"] = filepath
                state["last_title"] = _strip_ansi(title)
                state["last_finished"] = time.time()
            else:
                state["status"] = "error"
                state["error"] = "Downloaded file not found after processing."
    except Exception as exc:  # noqa: BLE001
        with state_lock:
            state["status"] = "error"
            state["error"] = friendly_error(str(exc))


# ---------------------------------------------------------------------------
# Routes — main flow
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    if not is_authorized():
        return redirect(url_for("setup"))
    with state_lock:
        status = state["status"]
    if status == "downloading":
        return redirect(url_for("progress"))
    return render_template("index.html", qualities=QUALITY_LABELS)


@app.route("/start", methods=["POST"])
def start():
    if not is_authorized():
        return redirect(url_for("setup"))

    url = (request.form.get("url") or "").strip()
    quality = (request.form.get("quality") or "best").strip()
    if quality not in QUALITY_FORMATS:
        quality = "best"

    if not url:
        return render_template(
            "index.html", error="Please enter a URL.", qualities=QUALITY_LABELS
        )

    with state_lock:
        if state["status"] == "downloading":
            return render_template("busy.html"), 409
        last_title = state.get("last_title", "")
        last_finished = state.get("last_finished")
        clean_downloads()
        state.update(_fresh_state())
        state["last_title"] = last_title
        state["last_finished"] = last_finished
        state["status"] = "downloading"

    threading.Thread(
        target=download_worker, args=(url, quality), daemon=True
    ).start()
    return redirect(url_for("progress"))


@app.route("/progress")
def progress():
    with state_lock:
        status = state["status"]
        title = state["title"]
    if status == "idle":
        return redirect(url_for("index"))
    if status == "finished":
        return redirect(url_for("download"))
    return render_template("progress.html", title=title)


@app.route("/progress-stream")
def progress_stream():
    def generate():
        while True:
            with state_lock:
                payload = {
                    "status": state["status"],
                    "progress": state["progress"],
                    "speed": state["speed"],
                    "eta": state["eta"],
                    "title": state["title"],
                    "error": state["error"],
                }
            yield f"data: {json.dumps(payload)}\n\n"
            if payload["status"] in ("finished", "error", "idle"):
                break
            time.sleep(0.5)

    return Response(generate(), mimetype="text/event-stream")


@app.route("/download")
def download():
    with state_lock:
        status = state["status"]
        title = state["title"]
        filepath = state["filepath"]
        error = state["error"]

    if status == "downloading":
        return redirect(url_for("progress"))
    if status == "error":
        return render_template(
            "index.html", error=error or "Download failed.", qualities=QUALITY_LABELS
        )
    if status != "finished" or not filepath:
        return redirect(url_for("index"))

    return render_template(
        "download.html", title=title, filename=os.path.basename(filepath)
    )


@app.route("/get-file")
def get_file():
    with state_lock:
        filepath = state["filepath"]
        title = state["title"]
    if not filepath or not os.path.isfile(filepath):
        return redirect(url_for("index"))
    ext = os.path.splitext(filepath)[1] or ".mp4"
    return send_file(
        filepath, as_attachment=True, download_name=_safe_filename(title, ext)
    )


@app.route("/reset", methods=["POST"])
def reset():
    with state_lock:
        last_title = state.get("last_title", "")
        last_finished = state.get("last_finished")
        state.update(_fresh_state())
        state["last_title"] = last_title
        state["last_finished"] = last_finished
        clean_downloads()
    return redirect(url_for("index"))


@app.route("/api/status")
def api_status():
    with state_lock:
        payload = {
            "status": state["status"],
            "progress": state["progress"],
            "speed": state["speed"],
            "eta": state["eta"],
            "title": state["title"],
            "error": state["error"],
        }
    return jsonify(payload)


# ---------------------------------------------------------------------------
# Routes — setup (first-run cookies upload)
# ---------------------------------------------------------------------------
@app.route("/setup", methods=["GET"])
def setup():
    if is_authorized():
        return redirect(url_for("index"))
    return render_template("setup.html", error=None)


@app.route("/setup", methods=["POST"])
def setup_upload():
    """Receive cookies.txt on the setup page (first-run flow)."""
    file = request.files.get("cookies")
    if not file or not file.filename:
        return render_template("setup.html", error="Please choose a cookies.txt file.")
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        raw = file.read()
        text = raw.decode("utf-8", errors="replace")
        if not _looks_like_cookies(text):
            return render_template(
                "setup.html",
                error="That doesn't look like a Netscape cookies.txt. Export it again using a browser extension.",
            )
        with open(COOKIES_PATH, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
    except OSError as exc:
        return render_template("setup.html", error=f"Could not save: {exc}. Check that the /config volume is writable.")
    return redirect(url_for("index"))


# ---------------------------------------------------------------------------
# Routes — settings
# ---------------------------------------------------------------------------
@app.route("/settings")
def settings():
    cookie_age_days = None
    cookie_warning = False
    if has_cookies():
        try:
            age = time.time() - os.path.getmtime(COOKIES_PATH)
            cookie_age_days = int(age / 86400)
            cookie_warning = cookie_age_days >= COOKIE_WARN_DAYS  # warn after 12 days
        except OSError:
            pass
    return render_template(
        "settings.html",
        has_cookies=has_cookies(),
        authorized=is_authorized(),
        cookie_age_days=cookie_age_days,
        cookie_warning=cookie_warning,
    )


@app.route("/settings/reauth", methods=["POST"])
def settings_reauth():
    """Delete cookies.txt so the user can upload fresh ones."""
    try:
        if os.path.isfile(COOKIES_PATH):
            os.remove(COOKIES_PATH)
    except OSError:
        pass
    return redirect(url_for("setup"))


def _render_settings(**extra):
    """Render settings.html with freshly-computed cookie age context."""
    cookie_age_days = None
    cookie_warning = False
    if has_cookies():
        try:
            age = time.time() - os.path.getmtime(COOKIES_PATH)
            cookie_age_days = int(age / 86400)
            cookie_warning = cookie_age_days >= COOKIE_WARN_DAYS
        except OSError:
            pass
    ctx = {
        "has_cookies": has_cookies(),
        "authorized": is_authorized(),
        "cookie_age_days": cookie_age_days,
        "cookie_warning": cookie_warning,
    }
    ctx.update(extra)
    return render_template("settings.html", **ctx)


@app.route("/settings/cookies", methods=["POST"])
def settings_cookies():
    file = request.files.get("cookies")
    if not file or not file.filename:
        return _render_settings(error="Please choose a cookies.txt file to upload.")
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        raw = file.read()
        text = raw.decode("utf-8", errors="replace")
        if not _looks_like_cookies(text):
            return _render_settings(
                error="That doesn't look like a Netscape cookies.txt. Export it again using a browser extension.",
            )
        with open(COOKIES_PATH, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
    except OSError as exc:
        return _render_settings(error=f"Could not save cookies file: {exc}")
    return _render_settings(message="cookies.txt uploaded successfully.")


# ---------------------------------------------------------------------------
# Routes — health / diagnostics
# ---------------------------------------------------------------------------
def _ffmpeg_version():
    try:
        out = subprocess.run(
            ["ffmpeg", "-version"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if out.returncode == 0 and out.stdout:
            first = out.stdout.splitlines()[0]
            return first.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return "not found"


def _collect_health():
    # Disk space for the downloads dir.
    try:
        usage = shutil.disk_usage(DOWNLOAD_DIR)
        disk_free = _format_bytes(usage.free)
        disk_total = _format_bytes(usage.total)
    except OSError:
        disk_free = disk_total = "unknown"

    cookies_present = has_cookies()
    cookie_mtime = None
    cookie_age_days = None
    cookie_warning = False
    if cookies_present:
        try:
            mtime = os.path.getmtime(COOKIES_PATH)
            cookie_mtime = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(mtime))
            cookie_age_days = int((time.time() - mtime) / 86400)
            cookie_warning = cookie_age_days >= COOKIE_WARN_DAYS
        except OSError:
            cookie_mtime = "unknown"

    with state_lock:
        last_title = state.get("last_title") or ""
        last_finished = state.get("last_finished")
    if last_finished:
        last_dl = time.strftime(
            "%Y-%m-%d %H:%M:%S UTC", time.gmtime(last_finished)
        )
        last_download = f"{last_title or 'Untitled'} ({last_dl})"
    else:
        last_download = "None yet"

    auth_method = "Authorized via cookies.txt" if cookies_present else "Not authorized"

    return {
        "authorized": is_authorized(),
        "auth_method": auth_method,
        "cookies_present": cookies_present,
        "cookie_path": COOKIES_PATH,
        "cookie_mtime": cookie_mtime or "n/a",
        "cookie_age_days": cookie_age_days,
        "cookie_warning": cookie_warning,
        "ytdlp_version": getattr(yt_dlp.version, "__version__", "unknown"),
        "ffmpeg": _ffmpeg_version(),
        "disk_free": disk_free,
        "disk_total": disk_total,
        "python_version": sys.version.split()[0],
        "python_version_full": sys.version.replace("\n", " "),
        "uptime_seconds": int(time.time() - START_TIME),
        "last_download": last_download,
    }


def _format_uptime(seconds):
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m}m {s}s"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


@app.route("/health")
def health():
    data = _collect_health()
    data["uptime_human"] = _format_uptime(data["uptime_seconds"])
    return render_template("health.html", health=data)


@app.route("/health/json")
def health_json():
    return jsonify(_collect_health())


# ---------------------------------------------------------------------------
# Routes — brand logo
# ---------------------------------------------------------------------------
@app.route("/brand-logo")
def brand_logo():
    if os.path.isfile(BRAND_LOGO_PATH):
        return send_file(BRAND_LOGO_PATH)
    abort(404)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, threaded=True)
