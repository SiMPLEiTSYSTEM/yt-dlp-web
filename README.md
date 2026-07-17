# yt-dlp-web

A small, self-hosted web interface for downloading a single video at a time with [yt-dlp](https://github.com/yt-dlp/yt-dlp). It runs in Docker on a compact Alpine Linux-based Python image, includes `ffmpeg` for video/audio merging, and keeps completed downloads temporary rather than building a media library.

Paste a supported video URL into the browser, watch live download progress, save the finished file, and hit **Start Over** to clean it up.

> [!IMPORTANT]
> A valid `cookies.txt` from a signed-in Google/YouTube account is required before downloads can start. The app asks for it on first launch and stores it in the local `ytdlp_config` directory so it survives container restarts.

## What it does

- Provides a straightforward browser UI for yt-dlp.
- Downloads at most **one job at a time**. If somebody else opens the app while a download is running, they see a busy page until it is available.
- Shows live progress, speed, ETA, and any download errors.
- Downloads individual videos only—playlists are deliberately disabled.
- Prefers an MP4 result with H.264 video and AAC audio where available, capped at 1080p.
- Uses `ffmpeg` to merge separate video and audio streams when needed.
- Gives the downloaded file a filename based on the source video title.
- Runs behind Gunicorn with one worker, which is intentional: the download state is held in-process so the one-at-a-time rule remains reliable.

## Temporary storage behavior

This project is not a long-term storage service.

Downloads are written to `/tmp/ytdlp_downloads` inside the container and exposed on the host through `./ytdlp_downloads`. The app clears this directory when you click **Start Over**, and it also clears it before starting the next download. The result remains present only until one of those cleanup actions occurs, so download the file to your own device before resetting the app.

The `./ytdlp_config` directory is different: it persists the required `cookies.txt` file across restarts.

## Quick start

### 1. Create a working directory

Create a directory for the Compose file and enter it:

```bash
mkdir yt-dlp-web
cd yt-dlp-web
```

### 2. Save the Compose file

Create `docker-compose.yml` with the configuration below. It pulls the published image and exposes the web UI on port `8080`.

```yaml
services:
  yt-dlp-web:
    image: ghcr.io/simpleitsystem/yt-dlp-web
    container_name: yt-dlp-web
    ports:
      - "8080:5000"
    restart: unless-stopped
    volumes:
      - ./ytdlp_downloads:/tmp/ytdlp_downloads
      - ./ytdlp_config:/config
```

### 3. Start the service

```bash
docker-compose up -d
```

Then open:

- On the Docker host: `http://localhost:8080`
- From another device on your network: `http://<host-ip>:8080`

### 4. Upload `cookies.txt`

On the first visit, the app opens its setup page. Upload a YouTube/Google `cookies.txt` file in Netscape cookie-file format. Once saved, it is stored as:

```text
./ytdlp_config/cookies.txt
```

The application makes a temporary writable copy of this file during each download, then deletes that copy afterward. This avoids modifying the original cookie file.

### 5. Download a video

1. Paste a supported video URL into the page.
2. Start the download and follow the progress display.
3. Save the completed file using the download link.
4. Select **Start Over** when you are finished to remove the temporary download and return to the URL form.

## Cookie notes

The cookies file is mandatory because it authenticates yt-dlp with YouTube and allows access to the available high-resolution formats. Cookies expire eventually, so re-upload a fresh `cookies.txt` if downloads start failing after the service has been working normally.

## Build from source

If you cloned this repository and want to build the included Dockerfile locally instead of using the published image:

```bash
docker build -t yt-dlp-web .
```

Then change the service in `docker-compose.yml` from:

```yaml
image: ghcr.io/simpleitsystem/yt-dlp-web
```

to:

```yaml
image: yt-dlp-web
```

and start it normally:

```bash
docker compose up -d
```

The image is based on `python:3.11-alpine`, installs `ffmpeg`, and runs the Flask application through Gunicorn. Its build process also upgrades yt-dlp, so rebuilding is a convenient way to refresh yt-dlp when a site changes.

## Project layout

```text
.
├── app.py                  # Flask routes, cookie setup, download state, and cleanup
├── Dockerfile              # Alpine-based application image with ffmpeg
├── docker-compose.yml      # Ready-to-run deployment configuration
├── requirements.txt        # Python dependencies
└── templates/              # Web UI templates
```

## A few intentional limits

This is designed as a lightweight, personal or trusted-network downloader—not a queueing system or shared media archive.

- **One active download:** this prevents competing jobs from fighting over the app's single state and temporary output location.
- **No playlist downloads:** each request is treated as one video.
- **1080p maximum:** the yt-dlp format selection is capped at 1080p.
- **No automatic deletion timer:** cleanup happens on **Start Over** or before the next download begins. If the host is shut down before that, check `./ytdlp_downloads` and remove anything left there manually.

## Troubleshooting

### The setup page keeps appearing

The service cannot find a usable `cookies.txt`. Upload a non-empty Netscape-format cookie file and ensure `./ytdlp_config` is writable by Docker. Do not make the `/config` volume read-only.

### A download fails

First, refresh and re-upload the cookies file—expired or invalid cookies are a common cause. Then inspect the container logs:

```bash
docker compose logs -f
```

### The page says the downloader is busy

Another download is in progress. The busy page polls the app and becomes available once that job finishes or fails.

### A file is still in `ytdlp_downloads`

Click **Start Over** in the UI, start another download, or remove the contents of `./ytdlp_downloads` manually. Completed files are not intended to remain there.

## Responsible use

Only download content you are permitted to access and save. You are responsible for complying with the terms of the source site and any applicable laws.
