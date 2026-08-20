# AGENTS.md

## Cursor Cloud specific instructions

This repo is a single Python app: a stdlib-`http.server` media gallery (`video_server.py` + `drive_backend.py`, with `index.html` / `photos.html` as the front end). There is no build step for development — you just run the Python server. See `README.md` for the product overview and full config reference.

### Services & how to run them

- Run the app (dev): `STORAGE_MODE=local MEDIA_ROOT=<media dir> SITE_PASSWORD=<pw> python3 video_server.py [PORT]` (defaults to port `8080`). `./start.sh` is the Mac-oriented convenience wrapper; on this Linux VM run `video_server.py` directly with an explicit `MEDIA_ROOT`.
- Health check: `GET /healthz` returns `ok`. Front end is at `/index.html` (videos) and `/photos.html`.
- Tests: `python3 -m unittest discover -s tests` (plain `unittest`, no extra deps).
- There is no linter/formatter configured. The only CI check (`.github/workflows`) is `docker build -t comfy-media .`, which builds the **production** image — it is not part of the dev loop and Docker is not installed here by default.

### Storage modes (important, non-obvious)

- `STORAGE_MODE=local` reads media from disk under `MEDIA_ROOT`. This is the only mode that runs with no external credentials, so use it for local dev/testing. Default paths are relative to `MEDIA_ROOT`: videos in `ComfyUI/output/video`, photos in `ComfyUI/output` (+ `stable-diffusion-webui/outputs`). The server `chdir`s into `MEDIA_ROOT` and refuses to start if it does not exist.
- `STORAGE_MODE=drive` (the production/Render mode) is a Google Drive gateway and **requires** the secrets `DRIVE_ROOT_FOLDER_ID` and `GOOGLE_SERVICE_ACCOUNT_JSON` (and typically `SITE_PASSWORD`); it exits immediately without them. These are not present in this environment, so drive mode cannot be exercised without adding those secrets.

### Auth & caveats

- If `SITE_PASSWORD` is set, everything except `/healthz` and `/login` requires auth. Log in via `POST /login` with form field `password=<pw>`; it sets the `comfy_auth` cookie. With no `SITE_PASSWORD`, auth is disabled entirely.
- Video thumbnails need `ffmpeg` (already on this VM); without it thumbnails are unavailable but the gallery still works.
- Caches live off the media dir at `~/.cache/comfy-media-server` (override with `MEDIA_CACHE_DIR`). In `local` mode the server also starts background prewarm/faststart threads unless `MEDIA_PREWARM=0`.
