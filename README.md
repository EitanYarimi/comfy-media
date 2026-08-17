# Comfy Media Gallery

Fast local media browser for **ComfyUI videos and Stable Diffusion photos** stored in **Google Drive**.

The Python server handles thumbnail generation (ffmpeg), byte-range video streaming, faststart MP4 optimization, and paginated APIs — the HTML clients are thin viewers.

## Architecture

```text
Google Drive (My Drive)          Mac (this repo)
├── ComfyUI/output/       ←──   video_server.py  ←──  index.html / photos.html
├── ComfyUI/output/video/
└── stable-diffusion-webui/outputs/

Local cache (not synced): ~/Library/Caches/comfy-media-server/
```

**GitHub hosts the code.** The server runs on your Mac and reads media from your synced Google Drive folder. Thumbnails and stream caches stay on local disk for speed.

## Quick start

```bash
git clone https://github.com/EitanYarimi/comfy-media.git
cd comfy-media

cp config.env.example config.env   # edit MEDIA_ROOT if needed
chmod +x start.sh
./start.sh
```

Open **http://localhost:8080/index.html** (videos) or **/photos.html**.

On your phone (same Wi‑Fi): use the LAN URL printed in the terminal, e.g. `http://192.168.x.x:8080/index.html`.

## Requirements

- Python 3.9+
- [Pillow](https://pypi.org/project/pillow/) — `pip install -r requirements.txt`
- **ffmpeg** (recommended) — `brew install ffmpeg` for video thumbnails and faststart streaming
- Google Drive desktop app syncing your `ComfyUI/` and `stable-diffusion-webui/` folders

## Configuration

| Variable | Default | Purpose |
|----------|---------|---------|
| `MEDIA_ROOT` | script directory | Google Drive **My Drive** folder |
| `VIDEO_DIR` | `ComfyUI/output/video` | Video scan path (under `MEDIA_ROOT`) |
| `PHOTO_DIRS` | `ComfyUI/output`, `stable-diffusion-webui/outputs` | Photo scan paths |
| `MEDIA_CACHE_DIR` | `~/Library/Caches/comfy-media-server` | Thumbnails + stream cache |
| `MEDIA_PREWARM` | `1` | Background thumbnail generation |
| `MEDIA_STREAM_CACHE_GB` | `20` | Max faststart cache size |

## API

| Endpoint | Description |
|----------|-------------|
| `GET /api/videos?summary=1` | Video count + month breakdown |
| `GET /api/videos?month=2026-08&offset=0&limit=40` | Paginated videos |
| `GET /api/photos?summary=1` | Photo count + months |
| `GET /thumb/{path}` | Photo thumbnail |
| `GET /vthumb/{path}` | Video thumbnail |
| `GET /{path}` | Stream media (Range requests supported) |
| `DELETE /{path}` | Delete file (use with care) |

## Why not GitHub Pages alone?

GitHub Pages is static hosting — it cannot run `video_server.py`, ffmpeg, or serve large video files with range requests. This project keeps the **performance-critical server on your Mac** (reading Google Drive) and uses GitHub to **version and share the app code**.

## License

Private personal gallery tool — use and modify as you like.
