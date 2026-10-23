# StreamSave

A simple, self-hosted YouTube video downloader with a clean web UI. Paste a
URL, pick a quality, and download the video as an MP4. Built on
[yt-dlp](https://github.com/yt-dlp/yt-dlp) and packaged with Docker for easy
hosting on your local network.

One download runs at a time; if someone else starts one while a download is in
progress, they see a friendly "please wait" page. Downloaded files are stored
only temporarily and are removed when you click **Start Over** — nothing piles
up on disk.

## Quick start

```bash
git clone https://github.com/SiMPLEiTSYSTEM/yt-dlp-web.git
cd yt-dlp-web
make up
```

Then open `http://<host-ip>:5000` in a browser and complete the one-time
**YouTube authorization** (see below).

## How authorization works (OAuth2, no browser extension)

On first visit you'll be sent to a setup page:

1. Click **Authorize with YouTube**.
2. The app shows a link (`google.com/device` / `youtube.com/activate`) and a
   short code.
3. Open that link on any device, sign in, and enter the code.
4. Once approved, the app stores an OAuth token and refreshes it automatically —
   you shouldn't need to re-authorize unless the token is revoked.

The token lives in the `ytdlp_config` Docker volume (`/config`), so it survives
container restarts and updates.

## Quality selection

The main page has a quality dropdown:

- **Best Available** (default) — highest resolution yt-dlp can get
- **1080p**, **720p**, **480p** — capped to that height

All downloads prefer H.264 video + AAC audio and are merged to MP4 for broad
compatibility.

## Updating

```bash
make update      # git pull && docker compose up -d --build
```

## Diagnostics

```bash
make health      # prints /health/json
```

Or open `http://<host-ip>:5000/health` for a dashboard showing auth status,
yt-dlp/ffmpeg versions, token age, disk space, uptime, and the last download.

## Troubleshooting

- **Downloads fail with a sign-in / session error** → open **Settings** and
  click **Re-authorize YouTube**.
- **OAuth won't complete** → under **Settings → Advanced** you can upload a
  Netscape-format `cookies.txt` as a fallback auth method.
- **"Selected quality isn't available"** → try **Best Available** or a lower
  quality.

## Make targets

| Command        | Action                              |
| -------------- | ----------------------------------- |
| `make up`      | build & start (detached)            |
| `make down`    | stop                                |
| `make restart` | restart                             |
| `make logs`    | follow logs                         |
| `make health`  | print `/health/json`                |
| `make update`  | git pull + rebuild                  |
| `make rebuild` | down + rebuild                      |
| `make shell`   | shell into the container            |

## Branding

All branding lives in `brand/config.json` (name, tagline, colors, logo flag)
and an optional `brand/logo.png`. The rest of the codebase is brand-agnostic.
