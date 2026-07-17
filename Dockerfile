# ---------------------------------------------------------------------------
# YT-DLP Downloader — container image
#
# Uses the Alpine-based Python image, which is dramatically smaller than the
# Debian "slim" image. All dependencies (flask, gunicorn, yt-dlp) are pure
# Python, so no compilers are needed and Alpine works cleanly.
# ---------------------------------------------------------------------------
FROM python:3.11-alpine

# ffmpeg is required by yt-dlp to merge separate video + audio streams.
# --no-cache keeps the layer small (no package index left behind).
RUN apk add --no-cache ffmpeg

WORKDIR /app

# Install Python dependencies first for better layer caching.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Always pull the newest yt-dlp on (re)build. The build arg below busts the
# Docker layer cache so a plain `--build` refreshes yt-dlp even when nothing
# else changed. Override it to force a refresh:
#   docker compose build --build-arg YTDLP_REFRESH=$(date +%s)
ARG YTDLP_REFRESH=1
RUN pip install --no-cache-dir --upgrade yt-dlp

# Copy application code and templates.
COPY app.py .
COPY templates/ ./templates/

# Directories for temporary downloads and the (mandatory) cookies file.
RUN mkdir -p /tmp/ytdlp_downloads /config

EXPOSE 5000

# Single worker (global in-process state) with multiple threads so the SSE
# stream and the background download thread can run concurrently.
CMD ["gunicorn", "--workers=1", "--threads=4", "--timeout=0", "--bind=0.0.0.0:5000", "app:app"]
