# Comfy Media Gallery

Browse **ComfyUI videos** and **Stable Diffusion photos** from **Google Drive**, with a Python server for ffmpeg thumbnails, byte-range streaming, and caching.

## Two ways to run

| Mode | Where | Best for |
|------|--------|----------|
| **Cloud (recommended)** | [Render](https://render.com) deploys from GitHub | Access from anywhere, no Mac running |
| **Local** | Your Mac + Google Drive desktop sync | Fastest, private LAN |

> **GitHub Pages cannot run this server** — it has no Python, ffmpeg, or video streaming. The repo lives on GitHub; the server runs on **Render** (free tier) and reads your Drive folder via API.

---

## Cloud setup (GitHub → Render)

### 1. Google Cloud service account

1. Open [Google Cloud Console](https://console.cloud.google.com/) → create a project.
2. **APIs & Services → Enable APIs** → enable **Google Drive API**.
3. **Credentials → Create credentials → Service account** → create key (JSON). Download the file.
4. Copy the service account email (looks like `something@project.iam.gserviceaccount.com`).

### 2. Share your Drive folder

1. In [Google Drive](https://drive.google.com), open the folder that contains `ComfyUI/` (usually **My Drive**).
2. **Share** → add the service account email → **Viewer**.
3. Copy the folder ID from the URL:  
   `https://drive.google.com/drive/folders/`**`THIS_PART`**

### 3. Deploy on Render (connected to GitHub)

1. Push this repo to GitHub (already at [EitanYarimi/comfy-media](https://github.com/EitanYarimi/comfy-media)).
2. [Render Dashboard](https://dashboard.render.com/) → **New → Blueprint** (or Web Service).
3. Connect the `comfy-media` repo — Render reads `render.yaml`.
4. Set secrets in Render **Environment**:
   - `DRIVE_ROOT_FOLDER_ID` — folder ID from step 2
   - `GOOGLE_SERVICE_ACCOUNT_JSON` — paste the **entire JSON key file** as one line
5. Deploy. Your gallery URL will be like `https://comfy-media-xxxx.onrender.com`.

Open `/index.html` (videos) or `/photos.html`.

**Note:** Free Render services sleep after ~15 min idle; first load may take ~30s to wake up.

---

## Local setup (optional)

For maximum speed when you're on the same Wi‑Fi as your Mac:

```bash
git clone https://github.com/EitanYarimi/comfy-media.git
cd comfy-media
cp config.env.example config.env
chmod +x start.sh
./start.sh
```

Requires Google Drive **desktop app** syncing `ComfyUI/` locally, plus `brew install ffmpeg`.

---

## Configuration

| Variable | Cloud | Local |
|----------|-------|-------|
| `STORAGE_MODE` | `drive` | `local` |
| `DRIVE_ROOT_FOLDER_ID` | Required | — |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | Required | — |
| `MEDIA_ROOT` | — | Google Drive "My Drive" path |
| `VIDEO_DIR` | `ComfyUI/output/video` | same |
| `PHOTO_DIRS` | `ComfyUI/output,...` | same |
| `MEDIA_CACHE_DIR` | `/tmp/...` (ephemeral) | `~/Library/Caches/...` |
| `MEDIA_PREWARM` | `0` (default in Docker) | `1` |

---

## Architecture (cloud)

```text
GitHub repo  ──auto-deploy──▶  Render (Docker)
                                  │
                                  ├─ video_server.py + ffmpeg
                                  ├─ index.html / photos.html
                                  └─ Google Drive API ──▶ your shared folder
                                       └─ local cache for thumbs/streams
```

---

## API

| Endpoint | Description |
|----------|-------------|
| `GET /api/videos?summary=1` | Video count + months |
| `GET /api/videos?month=2026-08&limit=40` | Paginated videos |
| `GET /api/photos?summary=1` | Photo count + months |
| `GET /thumb/{path}` | Photo thumbnail |
| `GET /vthumb/{path}` | Video thumbnail |
| `GET /{path}` | Stream media (Range requests) |

Delete is disabled in cloud/Drive mode.

---

## License

Personal gallery tool — use and modify freely.
