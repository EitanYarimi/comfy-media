# Comfy Media Gallery

Browse **ComfyUI videos** and **Stable Diffusion photos** from **Google Drive**.

In cloud mode the Python server is a **gateway**: it lists your Drive folder, then **streams bytes on demand** when you play a video or open a photo. It does not download whole libraries, run ffmpeg, or pre-warm caches.

## Two ways to run

| Mode | Where | What the server does |
|------|--------|----------------------|
| **Cloud (gateway)** | [Render](https://render.com) from GitHub | List Drive → proxy Range streams + Drive thumbnails |
| **Local** | Your Mac + Drive desktop sync | Fast ffmpeg thumbs and local disk streaming |

> **GitHub Pages cannot run this server.** The repo lives on GitHub; the gateway runs on **Render** and talks to Drive via API.

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

## How the gateway works

```text
Phone / browser
    │  GET /api/videos?summary=1     →  JSON list (Drive metadata only)
    │  GET /vthumb/...clip.mp4       →  Drive thumbnail (small)
    │  GET /...clip.mp4  Range: ...  →  proxy those bytes from Drive
    ▼
Render (video_server.py)
    └─ Google Drive API ──▶ your shared folder
```

Nothing is copied until someone actually watches or opens it. Seeking in a video sends HTTP Range requests; the gateway forwards them to Drive.

---

## Local setup (optional)

For maximum speed on the same Wi‑Fi as your Mac (ffmpeg thumbs, local files):

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

---

## API

| Endpoint | Description |
|----------|-------------|
| `GET /api/videos?summary=1` | Video count + months |
| `GET /api/videos?month=2026-08&limit=40` | Paginated videos |
| `GET /api/photos?summary=1` | Photo count + months |
| `GET /thumb/{path}` | Photo thumbnail (Drive, on demand) |
| `GET /vthumb/{path}` | Video thumbnail (Drive, on demand) |
| `GET /{path}` | Stream media (Range requests proxied to Drive) |

Delete is disabled in cloud/Drive mode.

---

## License

Personal gallery tool — use and modify freely.
