"""
YouTube Video Downloader — Flask web app (yt-dlp backend).

Features:
    * Single-download-at-a-time queue (global state guarded by a lock).
    * YouTube authentication via the `yt-dlp-youtube-oauth2` plugin
      (device-code OAuth2 flow). Token is cached under /config.
    * cookies.txt fallback (secondary auth option under Settings).
    * Quality selector (Best / 1080p / 720p / 480p), merged to MP4.
    * Live progress via Server-Sent Events.
    * /health + /health/json diagnostics.
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
    /setup            OAuth authorize page
    /setup/authorize (POST)  trigger device-code flow
    /api/oauth-status SSE stream of device-code + auth status
    /settings         settings (re-auth, cookies fallback)
    /settings/reauth (POST)  delete token -> /setup
    /settings/cookies (POST) upload cookies.txt fallback
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

# Persistent config volume (OAuth token cache + cookies fallback live here).
CONFIG_DIR = os.environ.get("YTDLP_CONFIG_DIR", "/config")
# The oauth2 plugin stores its token at <cachedir>/youtube-oauth2/token_data.json.
# We point yt-dlp's cachedir at CONFIG_DIR so the token persists on the volume.
OAUTH_TOKEN_PATH = os.path.join(CONFIG_DIR, "youtube-oauth2", "token_data.json")
COOKIES_PATH = os.path.join(CONFIG_DIR, "cookies.txt")

DOWNLOAD_DIR = "/tmp/ytdlp_downloads"
OUTPUT_TEMPLATE = os.path.join(DOWNLOAD_DIR, "download.%(ext)s")

# A short, always-available video used only to trigger the OAuth device flow.
OAUTH_TRIGGER_URL = "https://www.youtube.com/watch?v=jNQXAC9IVRw"

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
    "best": "bestvideo+bestaudio/best",
    "1080p": "bestvideo[height<=1080]+bestaudio/best[height<=1080]/best",
    "720p": "bestvideo[height<=720]+bestaudio/best[height<=720]/best",
    "480p": "bestvideo[height<=480]+bestaudio/best[height<=480]/best",
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
# OAuth device-flow state
# ---------------------------------------------------------------------------
oauth_lock = threading.Lock()


def _fresh_oauth_state():
    return {
        "status": "idle",       # idle | pending | waiting | authorized | error
        "verification_url": "",
        "user_code": "",
        "message": "",
        "error": "",
    }


oauth_state = _fresh_oauth_state()


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


def has_oauth_token():
    """True if a non-empty OAuth token file exists."""
    try:
        return os.path.isfile(OAUTH_TOKEN_PATH) and os.path.getsize(OAUTH_TOKEN_PATH) > 0
    except OSError:
        return False


def has_cookies():
    """True if a non-empty cookies.txt fallback exists."""
    try:
        return os.path.isfile(COOKIES_PATH) and os.path.getsize(COOKIES_PATH) > 0
    except OSError:
        return False


def is_authorized():
    """Authorized if we have an OAuth token or a cookies fallback."""
    return has_oauth_token() or has_cookies()


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
    """Common yt-dlp options shared by downloads and the OAuth trigger."""
    opts = {
        "cachedir": CONFIG_DIR,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        # Avoid the broken tv/tv_downgraded client ("page needs to be reloaded").
        "extractor_args": {"youtube": {"player_client": ["default", "web_embedded"]}},
    }
    # Prefer OAuth2 if a token exists; otherwise fall back to cookies.txt.
    if has_oauth_token():
        opts["username"] = "oauth2"
        opts["password"] = ""
    elif has_cookies():
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
        return "YouTube session issue. Try re-authorizing in Settings."
    if "private video" in low:
        return "This video is private and cannot be downloaded."
    if "video unavailable" in low:
        return "This video is unavailable in your region or has been removed."
    if "sign in" in low or "requires authentication" in low or "login required" in low:
        return "This video requires sign-in. Re-authorize in Settings."
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
# OAuth device-code flow
# ---------------------------------------------------------------------------
class OAuthLogger:
    """Captures yt-dlp/plugin log output to extract the device code + URL."""

    def _capture(self, msg):
        text = _strip_ansi(msg)
        if not text:
            return
        url = None
        code = None

        m_url = re.search(r"https?://\S*(?:google|youtube)\.com/\S*", text)
        if m_url:
            url = m_url.group(0).rstrip(".,)")

        m_code = re.search(r"enter code[:\s]+([A-Z0-9\-]{4,})", text, re.IGNORECASE)
        if not m_code:
            m_code = re.search(r"\bcode[:\s]+([A-Z0-9]{4,}-?[A-Z0-9]*)", text, re.IGNORECASE)
        if m_code:
            code = m_code.group(1).strip()

        if url or code:
            with oauth_lock:
                if url:
                    oauth_state["verification_url"] = url
                if code:
                    oauth_state["user_code"] = code
                oauth_state["status"] = "waiting"
                oauth_state["message"] = text

    def debug(self, msg):
        self._capture(msg)

    def info(self, msg):
        self._capture(msg)

    def warning(self, msg):
        self._capture(msg)

    def error(self, msg):
        self._capture(msg)


def authorize_worker():
    """Trigger the device-code OAuth flow by probing a video with oauth2 user."""
    ydl_opts = {
        "cachedir": CONFIG_DIR,
        "username": "oauth2",
        "password": "",
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "logger": OAuthLogger(),
        "extractor_args": {"youtube": {"player_client": ["default", "web_embedded"]}},
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.extract_info(OAUTH_TRIGGER_URL, download=False)
        # If extract_info returned and a token now exists, we're authorized.
        if has_oauth_token():
            with oauth_lock:
                oauth_state["status"] = "authorized"
                oauth_state["message"] = "Authorized."
        else:
            with oauth_lock:
                # Some plugin versions complete auth but we re-verify once.
                oauth_state["status"] = "authorized" if is_authorized() else "error"
                if oauth_state["status"] == "error":
                    oauth_state["error"] = (
                        "Authorization finished but no token was saved. "
                        "Please try again."
                    )
    except Exception as exc:  # noqa: BLE001
        if has_oauth_token():
            with oauth_lock:
                oauth_state["status"] = "authorized"
                oauth_state["message"] = "Authorized."
        else:
            with oauth_lock:
                oauth_state["status"] = "error"
                oauth_state["error"] = _strip_ansi(str(exc)) or "Authorization failed."


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
# Routes — OAuth setup
# ---------------------------------------------------------------------------
@app.route("/setup")
def setup():
    if is_authorized():
        return redirect(url_for("index"))
    return render_template("setup.html")


@app.route("/setup/authorize", methods=["POST"])
def setup_authorize():
    with oauth_lock:
        if oauth_state["status"] in ("pending", "waiting"):
            # Already in progress.
            return ("", 204)
        oauth_state.update(_fresh_oauth_state())
        oauth_state["status"] = "pending"
    threading.Thread(target=authorize_worker, daemon=True).start()
    return ("", 204)


@app.route("/api/oauth-status")
def api_oauth_status():
    def generate():
        while True:
            with oauth_lock:
                payload = dict(oauth_state)
            # If a token appeared out of band, report authorized.
            if payload["status"] != "authorized" and has_oauth_token():
                payload["status"] = "authorized"
            yield f"data: {json.dumps(payload)}\n\n"
            if payload["status"] in ("authorized", "error"):
                break
            time.sleep(1.0)

    return Response(generate(), mimetype="text/event-stream")


# ---------------------------------------------------------------------------
# Routes — settings
# ---------------------------------------------------------------------------
@app.route("/settings")
def settings():
    return render_template(
        "settings.html",
        has_token=has_oauth_token(),
        has_cookies=has_cookies(),
        authorized=is_authorized(),
    )


@app.route("/settings/reauth", methods=["POST"])
def settings_reauth():
    try:
        if os.path.isfile(OAUTH_TOKEN_PATH):
            os.remove(OAUTH_TOKEN_PATH)
    except OSError:
        pass
    with oauth_lock:
        oauth_state.update(_fresh_oauth_state())
    return redirect(url_for("setup"))


@app.route("/settings/cookies", methods=["POST"])
def settings_cookies():
    file = request.files.get("cookies")
    if not file or not file.filename:
        return render_template(
            "settings.html",
            has_token=has_oauth_token(),
            has_cookies=has_cookies(),
            authorized=is_authorized(),
            error="Please choose a cookies.txt file to upload.",
        )
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        file.save(COOKIES_PATH)
        msg = "cookies.txt uploaded successfully."
    except OSError as exc:
        return render_template(
            "settings.html",
            has_token=has_oauth_token(),
            has_cookies=has_cookies(),
            authorized=is_authorized(),
            error=f"Could not save cookies file: {exc}",
        )
    return render_template(
        "settings.html",
        has_token=has_oauth_token(),
        has_cookies=has_cookies(),
        authorized=is_authorized(),
        message=msg,
    )


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

    token_exists = has_oauth_token()
    token_mtime = None
    if token_exists:
        try:
            token_mtime = time.strftime(
                "%Y-%m-%d %H:%M:%S UTC",
                time.gmtime(os.path.getmtime(OAUTH_TOKEN_PATH)),
            )
        except OSError:
            token_mtime = "unknown"

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

    if token_exists:
        auth_method = "Authorized via OAuth2"
    elif has_cookies():
        auth_method = "Authorized via cookies.txt"
    else:
        auth_method = "Not authorized"

    return {
        "authorized": is_authorized(),
        "auth_method": auth_method,
        "oauth_token_present": token_exists,
        "cookies_present": has_cookies(),
        "ytdlp_version": getattr(yt_dlp.version, "__version__", "unknown"),
        "ffmpeg": _ffmpeg_version(),
        "token_path": OAUTH_TOKEN_PATH,
        "token_modified": token_mtime or "n/a",
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
