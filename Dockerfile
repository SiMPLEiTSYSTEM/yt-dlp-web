FROM python:3.11-alpine

# ffmpeg is required by yt-dlp to merge separate video + audio streams.
RUN apk add --no-cache ffmpeg

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Persistent config volume for the OAuth token cache + cookies fallback.
VOLUME ["/config"]
ENV YTDLP_CONFIG_DIR=/config

EXPOSE 5000

# Single worker (global in-process state) with threads so the SSE stream and
# the background download thread can run concurrently.
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "1", \
     "--worker-class", "gthread", "--threads", "4", "--timeout", "300", "app:app"]
