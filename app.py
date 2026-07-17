"""
YT-DLP Downloader — a minimal single-download-at-a-time web app.

Flow:
    URL input (/)  ->  start download (/start)  ->  progress page (/progress)
    -> download page (/download)  ->  Start Over (/reset)  ->  back to (/)

Design notes:
    * Only ONE download runs at a time. A global state dict guarded by a
      threading.Lock() holds the current download's status.
    * If a second user tries to start a download while one is running, they
      are shown a "busy" page that auto-polls /api/status.
    * Files are stored temporarily in /tmp/ytdlp_downloads and are deleted
      when the user clicks "Start Over" (POST /reset).
    * Progress is streamed to the browser using Server-Sent Events (SSE)
      via yt-dlp's progress_hooks (Python API, not subprocess).
"""

import os
import re
import json
import time
import shutil
import tempfile
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
)

import yt_dlp

app = Flask(__name__)
# Cookies files can be a few hundred KB; cap uploads at 5 MB to be safe.
app.config["MAX_CONTENT_LENGTH"] = 5 * 1024 * 1024

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
DOWNLOAD_DIR = "/tmp/ytdlp_downloads"
# Fixed output name so we always know where the merged file ends up.
OUTPUT_TEMPLATE = os.path.join(DOWNLOAD_DIR, "download.%(ext)s")

os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# ---------------------------------------------------------------------------
# Cookies (mandatory)
# ---------------------------------------------------------------------------
# A YouTube cookies.txt (Netscape format) is REQUIRED. It authenticates
# yt-dlp so YouTube serves the full range of high-resolution formats. The
# file lives in a persistent volume mounted at /config. If it is missing,
# the web UI shows an upload page instead of the downloader.
CONFIG_DIR = os.environ.get("YTDLP_CONFIG_DIR", "/config")
COOKIES_FILE = os.path.join(CONFIG_DIR, "cookies.txt")

os.makedirs(CONFIG_DIR, exist_ok=True)


def cookies_present():
    """True if a non-empty cookies file exists."""
    try:
        return os.path.isfile(COOKIES_FILE) and os.path.getsize(COOKIES_FILE) > 0
    except OSError:
        return False


def _looks_like_cookies(text):
    """Loose validation that an uploaded file is a Netscape cookies.txt.

    Accepts the standard Netscape header, or any line that has the 7
    tab-separated fields a cookies.txt row uses. This is intentionally
    lenient so exports from various browser extensions all pass.
    """
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

# ---------------------------------------------------------------------------
# Global single-download state (guarded by a lock)
# ---------------------------------------------------------------------------
state_lock = threading.Lock()


def _fresh_state():
    """Return a brand-new idle state dict."""
    return {
        "status": "idle",   # idle | downloading | finished | error
        "progress": 0.0,    # 0 - 100
        "speed": "",        # human readable, e.g. "1.2MiB/s"
        "eta": "",          # human readable, e.g. "00:42"
        "title": "",        # real video title (used as download filename)
        "filepath": "",     # absolute path to the finished file
        "error": "",        # error message if status == error
    }


state = _fresh_state()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def clean_downloads():
    """Remove every file in the download directory."""
    try:
        for name in os.listdir(DOWNLOAD_DIR):
            path = os.path.join(DOWNLOAD_DIR, name)
            try:
                if os.path.isfile(path) or os.path.islink(path):
                    os.remove(path)
                elif os.path.isdir(path):
                    # We never create subdirs, but clean them just in case.
                    import shutil

                    shutil.rmtree(path, ignore_errors=True)
            except OSError:
                pass
    except FileNotFoundError:
        os.makedirs(DOWNLOAD_DIR, exist_ok=True)


def _format_eta(seconds):
    """Turn a number of seconds into MM:SS (or HH:MM:SS)."""
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
    """Turn bytes/sec into a human readable string."""
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


def _strip_ansi(text):
    """Remove ANSI escape codes yt-dlp sometimes includes in strings."""
    if not text:
        return ""
    return re.sub(r"\x1b\[[0-9;]*m", "", str(text)).strip()


def progress_hook(d):
    """yt-dlp progress hook — updates global state under the lock."""
    status = d.get("status")

    if status == "downloading":
        total = d.get("total_bytes") or d.get("total_bytes_estimate")
        downloaded = d.get("downloaded_bytes") or 0
        percent = 0.0
        if total:
            percent = max(0.0, min(100.0, downloaded / total * 100.0))
        else:
            # Fall back to yt-dlp's own percent string if available.
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
        # A stream finished downloading; ffmpeg merge may still run afterwards.
        with state_lock:
            state["status"] = "downloading"
            state["progress"] = 99.0
            state["speed"] = ""
            state["eta"] = ""

    elif status == "error":
        with state_lock:
            state["status"] = "error"
            state["error"] = "Download failed."


def _find_downloaded_file():
    """Return the absolute path of the merged output file, or None."""
    if not os.path.isdir(DOWNLOAD_DIR):
        return None
    candidates = [
        os.path.join(DOWNLOAD_DIR, n)
        for n in os.listdir(DOWNLOAD_DIR)
        if n.startswith("download.")
    ]
    if not candidates:
        return None
    # Prefer the mp4 (final merged) file if present.
    for c in candidates:
        if c.endswith(".mp4"):
            return c
    return candidates[0]


def _safe_filename(title, ext):
    """Build a safe download filename from the video title."""
    title = _strip_ansi(title) or "video"
    title = re.sub(r'[\\/:*?"<>|]+', "_", title).strip()
    if not title:
        title = "video"
    return f"{title}.{ext.lstrip('.')}"


def download_worker(url):
    """Background thread: run yt-dlp and update state."""
    # yt-dlp may rewrite the cookies file (refreshing session cookies) when it
    # closes. To avoid mutating — or failing on a read-only — user-provided
    # file, work on a private writable copy that we delete afterwards.
    temp_cookies = None
    if cookies_present():
        try:
            fd, temp_cookies = tempfile.mkstemp(prefix="cookies_", suffix=".txt")
            os.close(fd)
            shutil.copyfile(COOKIES_FILE, temp_cookies)
        except OSError:
            temp_cookies = None

    ydl_opts = {
        # Cap at 1080p; within that, prefer the H264/AAC MP4 combo (mirrors the
        # user's proven local command: -S "vcodec:h264,acodec:aac,res,fps" -t mp4).
        "format": "bestvideo[height<=1080]+bestaudio/best[height<=1080]/best",
        "format_sort": ["vcodec:h264", "acodec:aac", "res", "fps"],
        "merge_output_format": "mp4",
        "outtmpl": OUTPUT_TEMPLATE,
        "progress_hooks": [progress_hook],
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "restrictfilenames": False,
    }

    if temp_cookies:
        ydl_opts["cookiefile"] = temp_cookies

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)

        # Store the real title so the download page / send_file can use it.
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
            else:
                state["status"] = "error"
                state["error"] = "Downloaded file not found after processing."

    except Exception as exc:  # noqa: BLE001 - report any failure to the UI
        with state_lock:
            state["status"] = "error"
            state["error"] = _strip_ansi(str(exc)) or "Download failed."
    finally:
        if temp_cookies and os.path.isfile(temp_cookies):
            try:
                os.remove(temp_cookies)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    """URL input page. If a download is running, show the busy page."""
    # Cookies are mandatory — send the user to the upload page if missing.
    if not cookies_present():
        return redirect(url_for("setup"))
    with state_lock:
        status = state["status"]
    if status == "downloading":
        return redirect(url_for("progress"))
    return render_template("index.html")


@app.route("/setup", methods=["GET"])
def setup():
    """Cookie upload page. Shown whenever no cookies file is present."""
    return render_template("setup.html", have_cookies=cookies_present())


@app.route("/setup", methods=["POST"])
def setup_upload():
    """Receive an uploaded cookies.txt and store it in the config volume."""
    file = request.files.get("cookies")
    if file is None or not file.filename:
        return render_template(
            "setup.html", have_cookies=cookies_present(),
            error="Please choose a cookies.txt file to upload.",
        )

    raw = file.read()
    try:
        text = raw.decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        text = ""

    if not _looks_like_cookies(text):
        return render_template(
            "setup.html", have_cookies=cookies_present(),
            error="That doesn't look like a Netscape-format cookies.txt file. "
                  "Please export it again using a 'Get cookies.txt' browser extension.",
        )

    try:
        # Normalise to LF and write atomically into the config volume.
        with open(COOKIES_FILE, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
    except OSError as exc:
        return render_template(
            "setup.html", have_cookies=cookies_present(),
            error=f"Could not save the file: {exc}. Make sure the /config "
                  "volume is writable (do not mount it read-only).",
        )

    return redirect(url_for("index"))


@app.route("/start", methods=["POST"])
def start():
    """Kick off a download if none is running, else show the busy page."""
    if not cookies_present():
        return redirect(url_for("setup"))

    url = (request.form.get("url") or "").strip()

    if not url:
        return render_template("index.html", error="Please enter a URL.")

    with state_lock:
        if state["status"] == "downloading":
            # Someone else is already downloading.
            return render_template("busy.html"), 409
        # Reset state and mark as downloading before releasing the lock so
        # no second request can slip in.
        clean_downloads()
        state.update(_fresh_state())
        state["status"] = "downloading"

    thread = threading.Thread(target=download_worker, args=(url,), daemon=True)
    thread.start()

    return redirect(url_for("progress"))


@app.route("/progress")
def progress():
    """Live progress page (SSE-driven)."""
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
    """Server-Sent Events stream of the current download progress."""

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
    """Download page shown when the file is ready."""
    with state_lock:
        status = state["status"]
        title = state["title"]
        filepath = state["filepath"]
        error = state["error"]

    if status == "downloading":
        return redirect(url_for("progress"))
    if status == "error":
        return render_template("index.html", error=error or "Download failed.")
    if status != "finished" or not filepath:
        return redirect(url_for("index"))

    filename = os.path.basename(filepath)
    return render_template("download.html", title=title, filename=filename)


@app.route("/get-file")
def get_file():
    """Send the actual file to the browser."""
    with state_lock:
        filepath = state["filepath"]
        title = state["title"]

    if not filepath or not os.path.isfile(filepath):
        return redirect(url_for("index"))

    ext = os.path.splitext(filepath)[1] or ".mp4"
    download_name = _safe_filename(title, ext)

    return send_file(
        filepath,
        as_attachment=True,
        download_name=download_name,
    )


@app.route("/reset", methods=["POST"])
def reset():
    """Start over: clear state, delete files, back to the URL input page."""
    with state_lock:
        state.update(_fresh_state())
        clean_downloads()
    return redirect(url_for("index"))


@app.route("/api/status")
def api_status():
    """JSON status endpoint (used by the busy page to poll)."""
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


if __name__ == "__main__":
    # For local development only; production uses gunicorn (see Dockerfile).
    app.run(host="0.0.0.0", port=5000, threaded=True)
