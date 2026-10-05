#!/usr/bin/env python3
"""
Simple video server that serves video files with metadata sorted by date.

Usage:
    # Serve media from your Google Drive folder (recommended):
    MEDIA_ROOT="$HOME/Library/CloudStorage/GoogleDrive-*/My Drive" python3 video_server.py

    # Or copy config.env.example -> config.env and run:
    ./start.sh

    # Custom port:
    python3 video_server.py 9090

Then open: http://localhost:8080/index.html

Environment:
    MEDIA_ROOT   — folder containing ComfyUI/output/ etc. (default: script directory)
    VIDEO_DIR    — video subfolder under MEDIA_ROOT (default: ComfyUI/output/video)
    PHOTO_DIRS   — comma-separated photo folders (default: ComfyUI/output,stable-diffusion-webui/outputs)
    MEDIA_CACHE_DIR — thumbnail/stream cache on local disk (default: ~/Library/Caches/comfy-media-server)
"""

import os
import sys
import json
import time
import shutil
import hashlib
import subprocess
import mimetypes
import io
import tempfile
import re
import random
import threading
from collections import OrderedDict, deque
from datetime import datetime
from http.server import HTTPServer, SimpleHTTPRequestHandler
from socketserver import ThreadingMixIn
from pathlib import Path
from urllib.parse import unquote, parse_qs, urlparse

STORAGE_MODE = os.environ.get('STORAGE_MODE', 'local').lower()
SITE_PASSWORD = os.environ.get('SITE_PASSWORD', '')
AUTH_COOKIE = 'comfy_auth'


def _default_port():
    env = os.environ.get('PORT')
    if env:
        return int(env)
    return 8080


PORT = _default_port()

# Media paths are relative to MEDIA_ROOT (local) or the shared Drive folder (cloud).
_VIDEO_DIR_DEFAULT = 'output/video' if STORAGE_MODE == 'drive' else 'ComfyUI/output/video'
_PHOTO_DIRS_DEFAULT = 'output' if STORAGE_MODE == 'drive' else 'ComfyUI/output,stable-diffusion-webui/outputs'
VIDEO_DIR = os.environ.get('VIDEO_DIR', _VIDEO_DIR_DEFAULT)
PHOTO_DIRS = [
    p.strip()
    for p in os.environ.get('PHOTO_DIRS', _PHOTO_DIRS_DEFAULT).split(',')
    if p.strip()
]

VIDEO_EXTENSIONS = {'.mp4', '.webm', '.ogg', '.mov', '.mkv', '.avi', '.m4v', '.3gp', '.flv', '.wmv'}
IMAGE_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp', '.svg', '.avif', '.heic'}


def _default_cache_root():
    """Keep caches off Google Drive: local disk is faster and avoids sync churn."""
    override = os.environ.get('MEDIA_CACHE_DIR')
    if override:
        return Path(override).expanduser()
    if sys.platform == 'darwin':
        return Path.home() / 'Library' / 'Caches' / 'comfy-media-server'
    return Path.home() / '.cache' / 'comfy-media-server'


def resolve_media_root():
    """Directory containing ComfyUI output folders (often Google Drive My Drive)."""
    script_dir = Path(__file__).resolve().parent
    override = os.environ.get('MEDIA_ROOT')
    if override:
        return Path(override).expanduser().resolve()

    # Repo often lives at My Drive/comfy-media while ComfyUI output is My Drive/ComfyUI/...
    drive_root = script_dir.parent
    video_on_drive = drive_root / 'ComfyUI' / 'output' / 'video'
    video_in_repo = script_dir / _VIDEO_DIR_DEFAULT
    if (
        (script_dir / 'video_server.py').is_file()
        and video_on_drive.is_dir()
        and _has_videos_under(video_on_drive)
        and not _has_videos_under(video_in_repo)
    ):
        print(f'   Auto MEDIA_ROOT: {drive_root}')
        print('   (ComfyUI videos are under My Drive, not inside the repo folder)')
        return drive_root.resolve()

    return script_dir


def _has_videos_under(directory):
    """True if directory tree contains at least one video file."""
    root = Path(directory)
    if not root.is_dir():
        return False
    try:
        for path in root.rglob('*'):
            if path.is_file() and path.suffix.lower() in VIDEO_EXTENSIONS:
                return True
    except OSError:
        return False
    return False


def _warn_local_media_root(script_dir, media_root):
    """Warn when MEDIA_ROOT is the repo clone instead of Google Drive My Drive."""
    script_dir = Path(script_dir).resolve()
    media_root = Path(media_root).resolve()
    if media_root != script_dir:
        return
    if not (script_dir / 'video_server.py').is_file():
        return

    video_here = media_root / VIDEO_DIR
    drive_root = media_root.parent
    video_on_drive = drive_root / 'ComfyUI' / 'output' / 'video'

    here_has = _has_videos_under(video_here)
    drive_has = _has_videos_under(video_on_drive) if video_on_drive.is_dir() else False

    if drive_has and not here_has:
        print('   ⚠️  MEDIA_ROOT is still the comfy-media repo folder — no videos found there.')
        if video_on_drive.is_dir():
            print(f'      ComfyUI videos on Drive: {video_on_drive}')
        print(f'      Currently scanning:       {video_here}')
        print('      Fix — either run:')
        print('         ./start.sh')
        print('      or set My Drive as MEDIA_ROOT, then restart:')
        print(f'         export MEDIA_ROOT="{drive_root}"')


CACHE_ROOT = _default_cache_root()
THUMB_CACHE_DIR = CACHE_ROOT / 'thumbs'
STREAM_CACHE_DIR = CACHE_ROOT / 'streams'
PHOTO_SRC_CACHE_DIR = CACHE_ROOT / 'photo_src'
PHOTO_SRC_CACHE_MAX_BYTES = 4 * 1024 ** 3
_src_trim_calls = 0

# Older versions stored caches inside Google Drive; still read them so the
# thousands of already-generated thumbnails stay usable.
LEGACY_THUMB_CACHE_DIR = Path('.thumb_cache')
LEGACY_STREAM_CACHE_DIR = Path('.stream_cache')

THUMB_SIZE = (150, 150)
PHOTO_THUMB_SIZE = (400, 400)
VIDEO_THUMB_SIZE = (400, 400)
PHOTO_CACHE_TTL = 300
VIDEO_CACHE_TTL = 300

VIDEO_INDEX_PATH = THUMB_CACHE_DIR / 'videos_index_v1.json'
PHOTO_INDEX_PATH = THUMB_CACHE_DIR / 'photos_index_v1.json'
LEGACY_VIDEO_INDEX_PATH = LEGACY_THUMB_CACHE_DIR / 'videos_index_v1.json'
LEGACY_PHOTO_INDEX_PATH = LEGACY_THUMB_CACHE_DIR / 'photos_index_v1.json'

# Thumbnails may be cached in any of these formats depending on ffmpeg build.
THUMB_FORMATS = (('.webp', 'image/webp'), ('.jpg', 'image/jpeg'), ('.png', 'image/png'))

_PREWARM_DEFAULT = '0' if STORAGE_MODE == 'drive' else '1'
PREWARM_ENABLED = os.environ.get('MEDIA_PREWARM', _PREWARM_DEFAULT).lower() not in ('0', 'false', 'no')
THUMB_MEMORY_LIMIT = 64 * 1024 * 1024
STREAM_CACHE_MAX_BYTES = int(os.environ.get('MEDIA_STREAM_CACHE_GB', '20')) * 1024 ** 3
# Drive mode: keep the first N MB of recently played files so the next open starts from disk.
DRIVE_PREFIX_CACHE_DIR = CACHE_ROOT / 'drive_prefix'
DRIVE_PREFIX_BYTES = max(1, int(os.environ.get('DRIVE_PREFIX_CACHE_MB', '4'))) * 1024 * 1024
DRIVE_PREFIX_CACHE_MAX_BYTES = max(
    DRIVE_PREFIX_BYTES,
    int(os.environ.get('DRIVE_PREFIX_CACHE_TOTAL_MB', '512')) * 1024 * 1024,
)
_drive_prefix_lock = threading.Lock()
_drive_prefix_warming = set()

_photo_cache = {'data': None, 'time': 0.0}
_video_cache = {'data': None, 'time': 0.0}
_media_by_month = {'videos': None, 'photos': None}
_media_by_day = {'videos': None, 'photos': None}
_photo_focus_lock = threading.Lock()
_photo_focus = {'month': None, 'day': None, 'gen': 0}
_index_refresh_lock = threading.Lock()
_refresh_flags = {'videos': False, 'photos': False}
_refresh_flags_lock = threading.Lock()
_basename_map = None
_basename_map_lock = threading.Lock()
# Existence-checking 17k Drive files on every process start made the first
# /api/videos wait on File Provider. Trust the disk index above this size.
INDEX_EXISTENCE_CHECK_LIMIT = 2000

_ffmpeg_path = shutil.which('ffmpeg')
_ffprobe_path = shutil.which('ffprobe')
_qlmanage_path = shutil.which('qlmanage') if sys.platform == 'darwin' else None


def _detect_ffmpeg_webp():
    """Many Homebrew ffmpeg builds ship without libwebp; probing once avoids
    a wasted encode attempt on every single thumbnail."""
    if not _ffmpeg_path:
        return False
    try:
        result = subprocess.run(
            [
                _ffmpeg_path, '-hide_banner', '-loglevel', 'error',
                '-f', 'lavfi', '-i', 'color=c=black:s=32x32:d=1',
                '-vframes', '1', '-f', 'webp', 'pipe:1',
            ],
            capture_output=True, timeout=20, check=False,
        )
        return result.returncode == 0 and bool(result.stdout)
    except (OSError, subprocess.SubprocessError):
        return False


_ffmpeg_has_webp = _detect_ffmpeg_webp()

_drive_storage = None


def get_drive_storage():
    global _drive_storage
    if _drive_storage is None:
        from drive_backend import DriveStorage
        _drive_storage = DriveStorage(CACHE_ROOT, VIDEO_DIR, PHOTO_DIRS)
    return _drive_storage


def _invalidate_basename_map():
    global _basename_map
    with _basename_map_lock:
        _basename_map = None


def _video_basename_map():
    """One rglob of VIDEO_DIR, reused so a missing file cannot trigger another."""
    global _basename_map
    with _basename_map_lock:
        if _basename_map is not None:
            return _basename_map
        mapping = {}
        scan_path = Path(VIDEO_DIR)
        if scan_path.is_dir():
            for path in scan_path.rglob('*'):
                try:
                    if path.is_file() and path.suffix.lower() in VIDEO_EXTENSIONS:
                        mapping.setdefault(path.name, []).append(path)
                except OSError:
                    continue
        _basename_map = mapping
        return mapping


def resolve_media_path(rel_path):
    """Return a local Path for serving (local mode only)."""
    rel_path = str(rel_path).replace('\\', '/').lstrip('/')
    filepath = Path(rel_path)
    try:
        if filepath.is_file():
            return filepath
    except OSError:
        pass
    # Index may be stale or VIDEO_DIR changed — unique basename under video dir.
    name = Path(rel_path).name
    if not name:
        return None
    matches = _video_basename_map().get(name) or []
    if len(matches) == 1:
        return matches[0]
    return None


def local_media_exists(rel_path):
    if STORAGE_MODE == 'drive':
        return get_drive_storage().exists(rel_path)
    rel_path = str(rel_path).replace('\\', '/').lstrip('/')
    try:
        if Path(rel_path).is_file():
            return True
    except OSError:
        return False
    return False


def media_exists(rel_path):
    return local_media_exists(rel_path)


def _filter_local_items(items):
    """Drop index entries whose files disappeared (common with Drive sync / stale cache)."""
    if STORAGE_MODE == 'drive' or not items:
        return items
    kept = [item for item in items if local_media_exists(item.get('path') or '')]
    dropped = len(items) - len(kept)
    if dropped:
        print(f'   Skipped {dropped} missing file(s) from local index')
    return kept


def auth_cookie_value():
    return hashlib.sha256(f'comfy-media|{SITE_PASSWORD}'.encode()).hexdigest()


def is_authed(handler):
    if not SITE_PASSWORD:
        return True
    cookie = handler.headers.get('Cookie') or ''
    return f'{AUTH_COOKIE}={auth_cookie_value()}' in cookie


LOGIN_PAGE = b"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Comfy Media</title>
<style>
body{font-family:-apple-system,BlinkMacSystemFont,sans-serif;background:#0a0a0a;color:#e0e0e0;
min-height:100vh;display:flex;align-items:center;justify-content:center;margin:0}
form{background:#111;border:1px solid #222;border-radius:16px;padding:28px;width:min(360px,90vw)}
h1{font-size:1.1rem;margin:0 0 16px}
input,button{width:100%;padding:12px 14px;border-radius:10px;border:1px solid #2a2a2a;font-size:1rem;box-sizing:border-box}
input{background:#1a1a1a;color:#eee;margin-bottom:12px}
button{background:#e60023;border-color:#e60023;color:#fff;font-weight:600;cursor:pointer}
p{color:#888;font-size:.8rem;margin:0 0 16px}
</style></head><body>
<form method="post" action="/login">
<h1>Comfy Media</h1>
<p>Enter the site password to open the gallery.</p>
<input type="password" name="password" placeholder="Password" autofocus>
<button type="submit">Enter</button>
</form></body></html>
"""


UNAUTHORIZED_JSON = b'{"error":"unauthorized"}'
HEALTHZ_BODY = b'ok\n'


def send_http_bytes(handler, status, data=b'', content_type=None, extra_headers=None):
    """Always set Content-Length. HTTP/1.1 keep-alive clients hang without it."""
    if data is None:
        data = b''
    elif isinstance(data, str):
        data = data.encode()
    handler.send_response(status)
    if content_type:
        handler.send_header('Content-Type', content_type)
    handler.send_header('Content-Length', str(len(data)))
    if extra_headers:
        for key, value in extra_headers.items():
            if value is not None:
                handler.send_header(key, value)
    handler.end_headers()
    if handler.command != 'HEAD' and data:
        safe_write(handler.wfile, data)


def send_http_empty(handler, status, extra_headers=None):
    send_http_bytes(handler, status, b'', extra_headers=extra_headers)


def send_http_json(handler, status, obj, extra_headers=None):
    send_http_bytes(
        handler, status, json.dumps(obj).encode(), 'application/json', extra_headers
    )


APP_HTML_PAGES = {
    '/': 'index.html',
    '/index.html': 'index.html',
    '/photos.html': 'photos.html',
    '/run.html': 'run.html',
}


def serve_app_html(handler, bare_path):
    """Serve gallery HTML from the app folder, not MEDIA_ROOT cwd."""
    name = APP_HTML_PAGES.get(bare_path)
    if not name:
        return False
    html_path = Path(__file__).resolve().parent / name
    if not html_path.is_file():
        send_http_bytes(
            handler, 404, f'Missing app file {name}\n'.encode(), 'text/plain; charset=utf-8'
        )
        return True
    data = html_path.read_bytes()
    handler.send_response(200)
    handler.send_header('Content-Type', 'text/html; charset=utf-8')
    handler.send_header('Content-Length', str(len(data)))
    handler.send_header('Cache-Control', 'no-store, no-cache, must-revalidate')
    handler.send_header('X-Comfy-App-Dir', str(html_path.parent))
    handler.end_headers()
    if handler.command != 'HEAD':
        safe_write(handler.wfile, data)
    return True


def send_login_page(handler, status=200):
    send_http_bytes(
        handler,
        status,
        LOGIN_PAGE,
        'text/html; charset=utf-8',
        extra_headers={'Cache-Control': 'no-store'},
    )


def require_site_auth(handler):
    if is_authed(handler):
        return True
    if handler.command == 'GET':
        send_login_page(handler)
    else:
        send_http_bytes(handler, 401, UNAUTHORIZED_JSON, 'application/json')
    return False


def vthumb_available():
    if STORAGE_MODE == 'drive':
        return True
    return bool(_ffmpeg_path or _qlmanage_path)


# Limit concurrent thumb work so listing/streaming aren't starved (esp. on Render).
_vthumb_semaphore = threading.Semaphore(3)
_drive_thumb_semaphore = threading.Semaphore(3)
_photo_thumb_semaphore = threading.Semaphore(8)
_hydrate_semaphore = threading.Semaphore(8)
DRIVE_GRID_THUMB_SIZE = 220
_photo_ondemand = 0
_photo_ondemand_lock = threading.Lock()
_thumb_inflight = {}
_thumb_inflight_lock = threading.Lock()
_active_streams = 0
_active_streams_lock = threading.Lock()
_faststart_jobs = set()
_faststart_queue = deque()
_faststart_lock = threading.Lock()
_moov_end_cache = {}


def _load_disk_index(path, legacy_path=None):
    cwd = Path(os.getcwd()).resolve()
    for candidate in (path, legacy_path):
        if candidate is None:
            continue
        try:
            data = json.loads(candidate.read_text())
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            continue
        items = data.get('items')
        if not isinstance(items, list):
            continue
        # Skip indexes built for a different MEDIA_ROOT (shared cache dir).
        saved_root = data.get('media_root')
        if saved_root:
            try:
                if Path(saved_root).expanduser().resolve() != cwd:
                    print(f'   Ignoring index from other MEDIA_ROOT: {saved_root}')
                    continue
            except OSError:
                continue
        saved = float(data.get('saved', 0))
        # Legacy copies are treated as stale so they refresh in background.
        return items, data.get('months'), (saved if candidate is path else 0.0)
    return None, None, 0.0


def _save_disk_index(path, items, months):
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            'items': items,
            'months': months,
            'saved': time.time(),
            'media_root': str(Path(os.getcwd()).resolve()),
        }))
    except OSError:
        pass


def _build_time_indexes(items):
    """Month and day maps so a filter never walks the whole library."""
    by_month = {}
    by_day = {}
    for item in items or []:
        mk = item_month_key(item['modified'])
        dk = item_day_key(item['modified'])
        by_month.setdefault(mk, []).append(item)
        by_day.setdefault(dk, []).append(item)
    return by_month, by_day


def _build_month_index(items):
    return _build_time_indexes(items)[0]


def _set_month_index(kind, items):
    if items is None:
        _media_by_month[kind] = None
        _media_by_day[kind] = None
        return
    by_month, by_day = _build_time_indexes(items)
    _media_by_month[kind] = by_month
    _media_by_day[kind] = by_day


def _refresh_in_progress(kind):
    with _refresh_flags_lock:
        return bool(_refresh_flags.get(kind))


def _schedule_media_refresh(kind):
    """Scan Google Drive on a worker thread — never on an HTTP request."""
    with _refresh_flags_lock:
        if _refresh_flags.get(kind):
            return False
        _refresh_flags[kind] = True
    target = _refresh_videos_background if kind == 'videos' else _refresh_photos_background

    def run():
        try:
            target()
        finally:
            with _refresh_flags_lock:
                _refresh_flags[kind] = False

    threading.Thread(target=run, daemon=True, name=f'refresh-{kind}').start()
    return True


def _commit_scan(kind, items):
    """Keep a larger existing index if Google Drive returned a partial listing."""
    cache = _video_cache if kind == 'videos' else _photo_cache
    old = cache['data'] or []
    if old and len(items) < int(len(old) * 0.9):
        print(
            f'   Keeping existing {kind} index ({len(old)} items); '
            f'scan returned {len(items)} — likely a partial Drive listing'
        )
        cache['time'] = time.time()
        return old
    months = media_month_summary(items)
    cache['data'] = items
    cache['time'] = time.time()
    _set_month_index(kind, items)
    path = VIDEO_INDEX_PATH if kind == 'videos' else PHOTO_INDEX_PATH
    _save_disk_index(path, items, months)
    if kind == 'videos':
        _invalidate_basename_map()
    return items


def _refresh_videos_background():
    with _index_refresh_lock:
        try:
            _commit_scan('videos', scan_videos('.'))
        except OSError:
            pass


def _refresh_photos_background():
    with _index_refresh_lock:
        try:
            _commit_scan('photos', scan_photos('.'))
        except OSError:
            pass


def _load_local_media(kind, force=False):
    """Memory/disk index first; full Drive walk only when we have nothing to show."""
    cache = _video_cache if kind == 'videos' else _photo_cache
    ttl = VIDEO_CACHE_TTL if kind == 'videos' else PHOTO_CACHE_TTL
    index_path = VIDEO_INDEX_PATH if kind == 'videos' else PHOTO_INDEX_PATH
    legacy_path = LEGACY_VIDEO_INDEX_PATH if kind == 'videos' else LEGACY_PHOTO_INDEX_PATH
    now = time.time()
    cached = cache['data']

    if cached is None:
        items, _months, saved = _load_disk_index(index_path, legacy_path)
        if items:
            kept = _filter_local_items(items) if len(items) <= INDEX_EXISTENCE_CHECK_LIMIT else items
            if kept:
                cached = kept
                cache['data'] = kept
                cache['time'] = saved or now
                _set_month_index(kind, kept)
            else:
                print(f'   {kind[:-1].title()} index entries missing on disk — rescanning…')

    if cached is not None:
        if force:
            _schedule_media_refresh(kind)
        return cached

    scanner = scan_videos if kind == 'videos' else scan_photos
    return _commit_scan(kind, scanner('.'))


def get_photos_cached(force=False):
    """Return cached photo list; load disk index instantly, refresh in background."""
    if STORAGE_MODE == 'drive':
        photos = get_drive_storage().scan_photos(refresh=force)
        _photo_cache['data'] = photos
        _photo_cache['time'] = time.time()
        _set_month_index('photos', photos)
        return photos
    return _load_local_media('photos', force=force)


def invalidate_photo_cache():
    _photo_cache['data'] = None
    _photo_cache['time'] = 0.0
    _media_by_month['photos'] = None
    _media_by_day['photos'] = None


def get_videos_cached(force=False):
    """Return cached video list; load disk index instantly, refresh in background."""
    if STORAGE_MODE == 'drive':
        videos = get_drive_storage().scan_videos()
        _video_cache['data'] = videos
        _video_cache['time'] = time.time()
        _set_month_index('videos', videos)
        return videos
    return _load_local_media('videos', force=force)


def invalidate_video_cache():
    _video_cache['data'] = None
    _video_cache['time'] = 0.0
    _media_by_month['videos'] = None
    _media_by_day['videos'] = None


def invalidate_media_cache():
    invalidate_photo_cache()
    invalidate_video_cache()


def _norm_rel(rel_path):
    return str(rel_path).replace('\\', '/').lstrip('/')


def _photo_thumb_on_disk(rel_path):
    """True if a grid thumb exists, without reading the bytes into memory."""
    key = _cache_key_from_rel(rel_path, ':400webp')
    if _thumb_memory_get(key) is not None:
        return True
    for cache_dir in (THUMB_CACHE_DIR, LEGACY_THUMB_CACHE_DIR):
        for ext, _mime in THUMB_FORMATS:
            try:
                if (cache_dir / (key + ext)).is_file():
                    return True
            except OSError:
                continue
    return False


def _delete_cached_thumbs(rel_path, filepath=None):
    keys = [
        _cache_key_from_rel(rel_path, ':400webp'),
        _cache_key_from_rel(rel_path, ':v400webp'),
    ]
    if filepath is not None:
        keys.append(_cache_key_from_rel(str(filepath), ':400webp'))
        keys.append(_cache_key_from_rel(str(filepath), ':v400webp'))
    for key in set(keys):
        with _thumb_memory_lock:
            _thumb_memory.pop(key, None)
        for cache_dir in (THUMB_CACHE_DIR, LEGACY_THUMB_CACHE_DIR):
            for ext in ('.webp', '.jpg', '.png', '.jpeg', '.meta.json'):
                try:
                    (cache_dir / (key + ext)).unlink(missing_ok=True)
                except OSError:
                    pass


def _remove_indexed_media(rel_path):
    """Drop one file from the in-memory + disk index so it cannot reappear."""
    rel = _norm_rel(rel_path)
    suffix = Path(rel).suffix.lower()
    kinds = []
    if suffix in IMAGE_EXTENSIONS:
        kinds.append('photos')
    if suffix in VIDEO_EXTENSIONS:
        kinds.append('videos')
    if not kinds:
        kinds = ['photos', 'videos']
    removed = False
    for kind in kinds:
        cache = _video_cache if kind == 'videos' else _photo_cache
        items = cache.get('data')
        if items is None:
            index_path = VIDEO_INDEX_PATH if kind == 'videos' else PHOTO_INDEX_PATH
            legacy_path = LEGACY_VIDEO_INDEX_PATH if kind == 'videos' else LEGACY_PHOTO_INDEX_PATH
            items, _months, saved = _load_disk_index(index_path, legacy_path)
            items = items or []
            if items:
                cache['data'] = items
                cache['time'] = saved or time.time()
        kept = [
            item for item in (items or [])
            if _norm_rel(item.get('path', '')) != rel
        ]
        if len(kept) == len(items or []):
            continue
        cache['data'] = kept
        cache['time'] = time.time()
        _set_month_index(kind, kept)
        index_path = VIDEO_INDEX_PATH if kind == 'videos' else PHOTO_INDEX_PATH
        _save_disk_index(index_path, kept, media_month_summary(kept))
        if kind == 'videos':
            _invalidate_basename_map()
        removed = True
    return removed


def delete_local_media(rel_path):
    """Unlink a local file, drop it from the library index, and purge thumbs."""
    rel = _norm_rel(rel_path)
    if not rel or '..' in rel.split('/'):
        return False
    filepath = resolve_media_path(rel) or Path(rel)
    removed_file = False
    try:
        if filepath.is_file():
            filepath.unlink()
            removed_file = True
    except OSError:
        raise
    removed_index = _remove_indexed_media(rel)
    _delete_cached_thumbs(rel, filepath)
    return removed_file or removed_index


def _video_cache_key(filepath):
    return hashlib.md5((str(filepath.resolve()) + ':v400webp').encode()).hexdigest()


def _cache_key_from_rel(rel_path, variant):
    """Hash an index path without statting Google Drive (abspath doesn't touch the file)."""
    rel_path = str(rel_path).replace('\\', '/')
    if not os.path.isabs(rel_path):
        rel_path = rel_path.lstrip('/')
    return hashlib.md5((os.path.abspath(rel_path) + variant).encode()).hexdigest()


def _mtime_from_query(query):
    """Thumb URLs send JS milliseconds in ?m=; ignore junk."""
    raw = (query or {}).get('m', [''])[0]
    try:
        value = float(raw or 0)
    except (TypeError, ValueError):
        return None
    if value > 1e12:
        value /= 1000.0
    return value or None


def get_disk_thumb(cache_key, src_mtime=None):
    """Serve a cached thumb from memory/disk without touching the original media file."""
    cached = _thumb_memory_get(cache_key)
    if cached is not None:
        data, mime = cached
        if data and _thumb_magic_ok(data):
            return cached
        with _thumb_memory_lock:
            _thumb_memory.pop(cache_key, None)

    for cache_dir in (THUMB_CACHE_DIR, LEGACY_THUMB_CACHE_DIR):
        for ext, mime in THUMB_FORMATS:
            cache_path = cache_dir / (cache_key + ext)
            try:
                st = cache_path.stat()
                if src_mtime and st.st_mtime < src_mtime:
                    continue
                data = cache_path.read_bytes()
            except OSError:
                continue
            if data and _thumb_magic_ok(data):
                _thumb_memory_put(cache_key, data, mime)
                return data, mime
    return None


def _stream_cache_path(filepath):
    key = hashlib.md5(str(filepath.resolve()).encode()).hexdigest()
    return STREAM_CACHE_DIR / (key + filepath.suffix.lower())


def _legacy_stream_cache_path(filepath):
    key = hashlib.md5(str(filepath.resolve()).encode()).hexdigest()
    return LEGACY_STREAM_CACHE_DIR / (key + filepath.suffix.lower())


def moov_at_end(filepath):
    """True when MP4 metadata is at file tail (slow streaming start)."""
    resolved = str(filepath.resolve())
    cached = _moov_end_cache.get(resolved)
    if cached is not None:
        return cached
    try:
        size = filepath.stat().st_size
        with open(filepath, 'rb') as f:
            head = f.read(min(512 * 1024, size))
            f.seek(max(0, size - 512 * 1024))
            tail = f.read(min(512 * 1024, size))
        result = b'moov' in tail and b'moov' not in head
    except OSError:
        result = False
    _moov_end_cache[resolved] = result
    return result


def schedule_faststart(filepath):
    """Queue a faststart rebuild. A single idle worker performs the transcode so
    it never competes with the playback request that discovered the problem."""
    if not _ffmpeg_path:
        return
    if _faststart_cache_valid(filepath, _stream_cache_path(filepath)):
        return
    if _faststart_cache_valid(filepath, _legacy_stream_cache_path(filepath)):
        return
    with _faststart_lock:
        key = str(filepath.resolve())
        if key in _faststart_jobs:
            return
        _faststart_jobs.add(key)
        _faststart_queue.append(filepath)


def _build_faststart(filepath):
    cache = _stream_cache_path(filepath)
    tmp = cache.with_suffix('.tmp' + cache.suffix)
    try:
        STREAM_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        tmp.unlink(missing_ok=True)
        result = subprocess.run(
            [
                _ffmpeg_path, '-hide_banner', '-loglevel', 'error',
                '-i', str(filepath),
                '-c', 'copy', '-movflags', '+faststart',
                str(tmp),
            ],
            capture_output=True, timeout=900, check=False,
        )
        if result.returncode == 0 and tmp.exists() and tmp.stat().st_size > 0:
            tmp.replace(cache)
        else:
            tmp.unlink(missing_ok=True)
    except (OSError, subprocess.SubprocessError):
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def prune_stream_cache():
    """Faststart copies are full-size, so keep the cache under a size budget."""
    try:
        files = [(f, f.stat()) for f in STREAM_CACHE_DIR.glob('*') if f.is_file()]
    except OSError:
        return
    total = sum(st.st_size for _f, st in files)
    if total <= STREAM_CACHE_MAX_BYTES:
        return
    files.sort(key=lambda pair: pair[1].st_atime)
    for f, st in files:
        if total <= STREAM_CACHE_MAX_BYTES:
            break
        try:
            f.unlink()
            total -= st.st_size
        except OSError:
            continue


def _drive_prefix_paths(file_id):
    safe = re.sub(r'[^A-Za-z0-9_-]', '_', str(file_id))[:120]
    return (
        DRIVE_PREFIX_CACHE_DIR / f'{safe}.bin',
        DRIVE_PREFIX_CACHE_DIR / f'{safe}.json',
    )


def prune_drive_prefix_cache():
    try:
        files = [
            (f, f.stat())
            for f in DRIVE_PREFIX_CACHE_DIR.glob('*.bin')
            if f.is_file()
        ]
    except OSError:
        return
    total = sum(st.st_size for _f, st in files)
    if total <= DRIVE_PREFIX_CACHE_MAX_BYTES:
        return
    files.sort(key=lambda pair: pair[1].st_atime)
    for path, st in files:
        if total <= DRIVE_PREFIX_CACHE_MAX_BYTES:
            break
        try:
            path.unlink(missing_ok=True)
            path.with_suffix('.json').unlink(missing_ok=True)
            total -= st.st_size
        except OSError:
            continue


def get_drive_prefix_cache(file_id, file_size):
    """Return (path, prefix_len) when a valid head cache exists for this Drive file."""
    bin_path, meta_path = _drive_prefix_paths(file_id)
    try:
        if not bin_path.is_file():
            return None
        meta = {}
        if meta_path.is_file():
            meta = json.loads(meta_path.read_text(encoding='utf-8'))
        if int(meta.get('file_size') or 0) != int(file_size):
            return None
        prefix_len = bin_path.stat().st_size
        if prefix_len <= 0:
            return None
        # Bump atime for LRU pruning.
        try:
            os.utime(bin_path, None)
        except OSError:
            pass
        return bin_path, prefix_len
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return None


def save_drive_prefix_cache(file_id, file_size, data):
    if not data:
        return
    try:
        DRIVE_PREFIX_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        bin_path, meta_path = _drive_prefix_paths(file_id)
        tmp = bin_path.with_suffix('.tmp')
        tmp.write_bytes(data)
        tmp.replace(bin_path)
        meta_path.write_text(
            json.dumps({'file_size': int(file_size), 'saved': time.time()}),
            encoding='utf-8',
        )
        prune_drive_prefix_cache()
    except OSError as exc:
        print(f'   Drive prefix cache write failed: {exc}')


def warm_drive_prefix_cache(file_id, file_size):
    """Download the first DRIVE_PREFIX_BYTES of a Drive file into disk cache."""
    file_size = int(file_size or 0)
    if file_size <= 0:
        return False
    wanted = min(file_size, DRIVE_PREFIX_BYTES)
    existing = get_drive_prefix_cache(file_id, file_size)
    if existing and existing[1] >= wanted:
        return True
    with _drive_prefix_lock:
        if file_id in _drive_prefix_warming:
            return False
        _drive_prefix_warming.add(file_id)
    try:
        existing = get_drive_prefix_cache(file_id, file_size)
        if existing and existing[1] >= wanted:
            return True
        end = wanted - 1
        drive = get_drive_storage()
        resp = drive.open_media(file_id, f'bytes=0-{end}', timeout=60)
        try:
            if resp.status_code not in (200, 206):
                return False
            chunks = []
            total = 0
            for chunk in resp.iter_content(256 * 1024):
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
                if total >= wanted:
                    break
            data = b''.join(chunks)[:wanted]
            if not data:
                return False
            save_drive_prefix_cache(file_id, file_size, data)
            return True
        finally:
            resp.close()
    except Exception as exc:
        print(f'   Drive prefix warm failed: {exc}')
        return False
    finally:
        with _drive_prefix_lock:
            _drive_prefix_warming.discard(file_id)


def schedule_drive_prefix_warm(file_id, file_size):
    wanted = min(int(file_size or 0), DRIVE_PREFIX_BYTES)
    if wanted <= 0:
        return
    existing = get_drive_prefix_cache(file_id, file_size)
    if existing and existing[1] >= wanted:
        return
    with _drive_prefix_lock:
        if file_id in _drive_prefix_warming:
            return
    threading.Thread(
        target=warm_drive_prefix_cache,
        args=(file_id, file_size),
        daemon=True,
    ).start()


def _serve_drive_prefix_range(handler, prefix_path, file_size, content_type, start, end):
    length = end - start + 1
    handler.send_response(206)
    handler.send_header('Content-Type', content_type)
    handler.send_header('Content-Length', str(length))
    handler.send_header('Content-Range', f'bytes {start}-{end}/{file_size}')
    handler.send_header('Accept-Ranges', 'bytes')
    handler.send_header('Cache-Control', 'public, max-age=3600')
    handler.send_header('X-Cache', 'HIT')
    handler.end_headers()
    with open(prefix_path, 'rb') as f:
        f.seek(start)
        remaining = length
        while remaining > 0:
            chunk = f.read(min(256 * 1024, remaining))
            if not chunk or not safe_write(handler.wfile, chunk):
                break
            remaining -= len(chunk)


def _faststart_worker():
    while True:
        with _active_streams_lock:
            busy = _active_streams > 0
        if busy:
            time.sleep(2)
            continue

        with _faststart_lock:
            filepath = _faststart_queue.popleft() if _faststart_queue else None

        if filepath is None:
            time.sleep(2)
            continue

        try:
            if filepath.is_file() and not _faststart_cache_valid(filepath, _stream_cache_path(filepath)):
                _build_faststart(filepath)
                prune_stream_cache()
        except OSError:
            pass
        finally:
            with _faststart_lock:
                _faststart_jobs.discard(str(filepath.resolve()))


def _faststart_cache_valid(filepath, cache):
    """Only serve faststart copy when it looks complete."""
    try:
        src_size = filepath.stat().st_size
        cache_stat = cache.stat()
        if cache_stat.st_mtime < filepath.stat().st_mtime:
            return False
        if cache_stat.st_size < max(4096, int(src_size * 0.5)):
            return False
        with open(cache, 'rb') as f:
            head = f.read(min(512 * 1024, cache_stat.st_size))
        return b'moov' in head
    except OSError:
        return False


def resolve_stream_path(filepath):
    """Prefer faststart cache; schedule build when moov is at end."""
    filepath = Path(filepath)
    for cache in (_stream_cache_path(filepath), _legacy_stream_cache_path(filepath)):
        if _faststart_cache_valid(filepath, cache):
            return cache
    if filepath.suffix.lower() == '.mp4' and moov_at_end(filepath):
        schedule_faststart(filepath)
    return filepath


_thumb_memory = OrderedDict()
_thumb_memory_bytes = 0
_thumb_memory_lock = threading.Lock()

# Thumb URLs carry ?v=<pipeline>&m=<mtime>, so a given URL never changes content.
# Revalidating them made every filter change re-fetch the whole grid.
THUMB_CACHE_HEADER = 'public, max-age=31536000, immutable'

# Files ffmpeg cannot thumbnail (cloud stubs, truncated writes) otherwise re-run
# the full seek/encode search on every grid render.
THUMB_FAILURE_TTL = 600
_thumb_failures = {}
_thumb_failures_lock = threading.Lock()


def _thumb_failure_key(cache_key, filepath):
    try:
        return cache_key, filepath.stat().st_mtime
    except OSError:
        return cache_key, None


def _thumb_failed_recently(cache_key, filepath):
    key = _thumb_failure_key(cache_key, filepath)
    with _thumb_failures_lock:
        failed_at = _thumb_failures.get(key)
        if failed_at is None:
            return False
        if time.time() - failed_at < THUMB_FAILURE_TTL:
            return True
        _thumb_failures.pop(key, None)
        return False


def _remember_thumb_failure(cache_key, filepath):
    key = _thumb_failure_key(cache_key, filepath)
    with _thumb_failures_lock:
        if len(_thumb_failures) > 5000:
            _thumb_failures.clear()
        _thumb_failures[key] = time.time()


def _thumb_memory_get(cache_key):
    with _thumb_memory_lock:
        entry = _thumb_memory.get(cache_key)
        if entry is not None:
            _thumb_memory.move_to_end(cache_key)
        return entry


def _thumb_memory_put(cache_key, data, mime):
    global _thumb_memory_bytes
    with _thumb_memory_lock:
        if cache_key in _thumb_memory:
            _thumb_memory_bytes -= len(_thumb_memory[cache_key][0])
        _thumb_memory[cache_key] = (data, mime)
        _thumb_memory.move_to_end(cache_key)
        _thumb_memory_bytes += len(data)
        while _thumb_memory_bytes > THUMB_MEMORY_LIMIT and _thumb_memory:
            _, (old_data, _mime) = _thumb_memory.popitem(last=False)
            _thumb_memory_bytes -= len(old_data)


def _thumb_magic_ok(data):
    """True if bytes look like a real image container (not JSON/HTML/empty)."""
    if not data or len(data) < 64:
        return False
    if data[:2] == b'\xff\xd8':
        return True  # JPEG
    if data[:8] == b'\x89PNG\r\n\x1a\n':
        return True  # PNG
    if len(data) >= 12 and data[:4] == b'RIFF' and data[8:12] == b'WEBP':
        return True
    return False


def _thumb_detail_score(data):
    """Higher = more visual detail. Solid color frames score near 0."""
    try:
        from PIL import Image
        import statistics
        img = Image.open(io.BytesIO(data)).convert('RGB')
        if img.width < 8 or img.height < 8:
            return 0.0
        sample = img.resize((24, 24), getattr(Image, 'Resampling', Image).BILINEAR)
        pixels = [sample.getpixel((x, y)) for y in range(sample.height) for x in range(sample.width)]
        return max(statistics.pstdev([p[c] for p in pixels]) for c in range(3))
    except Exception:
        return 10.0  # assume ok if we can't score


def _thumb_has_visual_content(data):
    """Reject solid-color garbage frames (common with incomplete Drive cloud files)."""
    if not _thumb_magic_ok(data):
        return False
    return _thumb_detail_score(data) >= 6.0


def _thumb_is_usable(data):
    return bool(data) and _thumb_magic_ok(data) and _thumb_has_visual_content(data)


def get_cached_video_thumbnail(filepath):
    """Return cached thumbnail bytes from memory, local cache, or legacy cache."""
    cache_key = _video_cache_key(filepath)
    cached = _thumb_memory_get(cache_key)
    if cached is not None:
        data, mime = cached
        if _thumb_is_usable(data):
            return cached
        with _thumb_memory_lock:
            _thumb_memory.pop(cache_key, None)

    try:
        src_mtime = filepath.stat().st_mtime
    except OSError:
        return None

    for cache_dir in (THUMB_CACHE_DIR, LEGACY_THUMB_CACHE_DIR):
        for ext, mime in THUMB_FORMATS:
            cache_path = cache_dir / (cache_key + ext)
            try:
                if cache_path.stat().st_mtime >= src_mtime:
                    data = cache_path.read_bytes()
                    if _thumb_is_usable(data):
                        _thumb_memory_put(cache_key, data, mime)
                        return data, mime
                    # Drop solid-color / corrupt cache so we can regenerate.
                    try:
                        cache_path.unlink()
                    except OSError:
                        pass
            except OSError:
                continue
    return None


def _read_thumb_meta(cache_key):
    """Merge the sidecar caches, newest location winning."""
    meta = {}
    for candidate in (
        LEGACY_THUMB_CACHE_DIR / (cache_key + '.meta.json'),
        THUMB_CACHE_DIR / (cache_key + '.meta.json'),
    ):
        try:
            loaded = json.loads(candidate.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        if isinstance(loaded, dict):
            meta.update(loaded)
    return meta


def _update_thumb_meta(cache_key, updates):
    """Sidecars hold duration and orientation, so never overwrite the whole file."""
    meta = _read_thumb_meta(cache_key)
    meta.update(updates)
    try:
        THUMB_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        (THUMB_CACHE_DIR / (cache_key + '.meta.json')).write_text(json.dumps(meta))
    except OSError:
        pass
    return meta


def get_video_duration(filepath):
    """Return video duration in seconds, using sidecar cache when available."""
    cache_key = _video_cache_key(filepath)
    meta = _read_thumb_meta(cache_key)
    if meta.get('duration') is not None:
        return meta['duration']
    if not _ffprobe_path:
        return None
    try:
        result = subprocess.run(
            [
                _ffprobe_path, '-v', 'error', '-show_entries', 'format=duration',
                '-of', 'default=noprint_wrappers=1:nokey=1', str(filepath),
            ],
            capture_output=True, text=True, timeout=30, check=False,
        )
        if result.returncode == 0 and result.stdout.strip():
            duration = float(result.stdout.strip())
            _update_thumb_meta(cache_key, {'duration': duration})
            return duration
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    return None


ORIENTATIONS = ('horizontal', 'vertical')
# Probing is only for clips that have never been thumbnailed; the walk stops here
# so one Random click can never turn into thousands of ffprobe runs.
ORIENTATION_PROBE_BUDGET = 24

_orientation_cache = {}
_orientation_lock = threading.Lock()


def orientation_from_size(width, height):
    """Square counts as horizontal — it frames like one and keeps the split binary."""
    try:
        width, height = int(width or 0), int(height or 0)
    except (TypeError, ValueError):
        return None
    if width <= 0 or height <= 0:
        return None
    return 'vertical' if height > width else 'horizontal'


def normalize_orientation(value):
    value = (value or '').strip().lower()
    aliases = {
        'landscape': 'horizontal', 'wide': 'horizontal', 'h': 'horizontal',
        'portrait': 'vertical', 'tall': 'vertical', 'v': 'vertical',
    }
    value = aliases.get(value, value)
    return value if value in ORIENTATIONS else None


def _orientation_from_thumb(filepath):
    """Thumbnails keep the source aspect ratio, so a cached one answers for free."""
    cached = get_cached_video_thumbnail(filepath)
    if not cached:
        return None
    try:
        from PIL import Image
        with Image.open(io.BytesIO(cached[0])) as img:
            return orientation_from_size(img.width, img.height)
    except Exception:
        return None


def _orientation_from_ffprobe(filepath):
    if not _ffprobe_path:
        return None
    try:
        result = subprocess.run(
            [
                _ffprobe_path, '-v', 'error', '-select_streams', 'v:0',
                '-show_entries', 'stream=width,height:stream_tags=rotate:stream_side_data=rotation',
                '-of', 'json', str(filepath),
            ],
            capture_output=True, text=True, timeout=30, check=False,
        )
        if result.returncode != 0 or not result.stdout.strip():
            return None
        streams = json.loads(result.stdout).get('streams') or []
        if not streams:
            return None
        stream = streams[0]
        width, height = stream.get('width'), stream.get('height')
        rotation = (stream.get('tags') or {}).get('rotate')
        for side in stream.get('side_data_list') or []:
            if side.get('rotation') is not None:
                rotation = side['rotation']
        try:
            # Phone footage is stored landscape with a rotation flag.
            if rotation is not None and abs(int(float(rotation))) % 180 == 90:
                width, height = height, width
        except (TypeError, ValueError):
            pass
        return orientation_from_size(width, height)
    except (OSError, ValueError, json.JSONDecodeError, subprocess.SubprocessError):
        return None


def video_orientation(item, allow_probe=False):
    """Resolve 'horizontal'/'vertical' for a media item, cheapest source first.

    Returns None when it cannot be determined without more work than allowed.
    """
    known = orientation_from_size(item.get('width'), item.get('height'))
    if known:
        return known
    rel = item.get('path')
    if not rel:
        return None
    with _orientation_lock:
        cached = _orientation_cache.get(rel)
    if cached:
        return cached
    if STORAGE_MODE == 'drive':
        return None
    filepath = resolve_media_path(rel)
    try:
        if not filepath or not filepath.is_file():
            return None
    except OSError:
        return None
    cache_key = _video_cache_key(filepath)
    found = normalize_orientation(_read_thumb_meta(cache_key).get('orientation'))
    if not found:
        found = _orientation_from_thumb(filepath)
    if not found and allow_probe:
        found = _orientation_from_ffprobe(filepath)
    if found:
        _update_thumb_meta(cache_key, {'orientation': found})
        with _orientation_lock:
            _orientation_cache[rel] = found
    return found


def _store_thumb(cache_key, ext, data, mime):
    if not _thumb_is_usable(data):
        return None
    try:
        THUMB_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        (THUMB_CACHE_DIR / (cache_key + ext)).write_bytes(data)
    except OSError:
        pass
    _thumb_memory_put(cache_key, data, mime)
    return data, mime


def _thumb_from_qlmanage(filepath, cache_key):
    """macOS Quick Look thumbnail — works without ffmpeg."""
    if not _qlmanage_path:
        return None
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            result = subprocess.run(
                [
                    _qlmanage_path, '-t',
                    '-s', str(VIDEO_THUMB_SIZE[0]),
                    '-o', tmpdir,
                    str(filepath.resolve()),
                ],
                capture_output=True,
                timeout=90,
                check=False,
            )
            if result.returncode != 0:
                return None
            pngs = sorted(Path(tmpdir).glob('*.png'))
            if not pngs:
                return None
            return _store_thumb(cache_key, '.png', pngs[0].read_bytes(), 'image/png')
    except (OSError, subprocess.SubprocessError):
        return None


def _thumb_from_ffmpeg(filepath, cache_key):
    if not _ffmpeg_path:
        return None

    # Skip obvious cloud stubs / empty files — ffmpeg often yields solid-color junk.
    try:
        if filepath.stat().st_size < 8192:
            return None
    except OSError:
        return None

    # Fixed seek avoids slow ffprobe on first thumbnail (Google Drive latency).
    # Prefer the seek that yields the most visual detail (skips solid red/black intros).
    scale = f'scale={VIDEO_THUMB_SIZE[0]}:{VIDEO_THUMB_SIZE[1]}:force_original_aspect_ratio=decrease'
    encode_attempts = []
    if _ffmpeg_has_webp:
        encode_attempts.append((['-f', 'webp', '-quality', '75'], '.webp', 'image/webp'))
    encode_attempts.append((['-f', 'image2pipe', '-vcodec', 'mjpeg', '-q:v', '4'], '.jpg', 'image/jpeg'))

    best = None
    best_score = -1.0
    for seek in ('0.5', '1.0', '2.0', '0.1', '0'):
        for encode_args, ext, mime in encode_attempts:
            try:
                result = subprocess.run(
                    [
                        _ffmpeg_path, '-hide_banner', '-loglevel', 'error',
                        '-ss', seek, '-i', str(filepath),
                        '-an', '-sn', '-vframes', '1', '-vf', scale,
                        *encode_args, 'pipe:1',
                    ],
                    capture_output=True, timeout=60, check=False,
                )
            except (OSError, subprocess.SubprocessError):
                continue
            data = result.stdout if result.returncode == 0 else None
            if not data or not _thumb_magic_ok(data):
                continue
            score = _thumb_detail_score(data)
            if score > best_score:
                best_score = score
                best = (ext, data, mime)
            # The other encoder would re-encode the same frame, so its score is
            # effectively identical — move to the next seek instead.
            break
        if best_score >= 18.0:
            break

    if not best or best_score < 6.0:
        return None
    ext, data, mime = best
    return _store_thumb(cache_key, ext, data, mime)


def _record_thumb_orientation(cache_key, data):
    """Prewarm already decoded the frame, so bank the orientation while it is here."""
    if _read_thumb_meta(cache_key).get('orientation'):
        return
    try:
        from PIL import Image
        with Image.open(io.BytesIO(data)) as img:
            found = orientation_from_size(img.width, img.height)
    except Exception:
        return
    if found:
        _update_thumb_meta(cache_key, {'orientation': found})


def generate_video_thumbnail(filepath):
    """Generate or load cached video thumbnail. Returns (data, mime) or None."""
    cached = get_cached_video_thumbnail(filepath)
    if cached:
        return cached

    cache_key = _video_cache_key(filepath)
    if _thumb_failed_recently(cache_key, filepath):
        return None

    with _vthumb_semaphore:
        cached = get_cached_video_thumbnail(filepath)
        if cached:
            return cached
        if _thumb_failed_recently(cache_key, filepath):
            return None
        result = _thumb_from_ffmpeg(filepath, cache_key)
        if not result:
            result = _thumb_from_qlmanage(filepath, cache_key)
        if result:
            _record_thumb_orientation(cache_key, result[0])
        else:
            _remember_thumb_failure(cache_key, filepath)
        return result


def _photo_cache_key(filepath):
    """Hash without Path.resolve() — that stats Google Drive on every lookup."""
    return _cache_key_from_rel(str(filepath), ':400webp')


def _photo_ondemand_begin():
    global _photo_ondemand
    with _photo_ondemand_lock:
        _photo_ondemand += 1


def _photo_ondemand_end():
    global _photo_ondemand
    with _photo_ondemand_lock:
        _photo_ondemand = max(0, _photo_ondemand - 1)


def _photo_ondemand_busy():
    with _photo_ondemand_lock:
        return _photo_ondemand > 0


class _InflightThumb:
    def __init__(self):
        self.event = threading.Event()
        self.result = None


def _single_flight_photo(cache_key, factory):
    """One Drive decode per photo; browser + prewarm share the result."""
    cached = get_disk_thumb(cache_key)
    if cached:
        return cached
    owner = False
    with _thumb_inflight_lock:
        slot = _thumb_inflight.get(cache_key)
        if slot is None:
            slot = _InflightThumb()
            _thumb_inflight[cache_key] = slot
            owner = True
    if not owner:
        slot.event.wait(timeout=180)
        return slot.result or get_disk_thumb(cache_key)
    try:
        slot.result = factory()
        return slot.result
    finally:
        slot.event.set()
        with _thumb_inflight_lock:
            if _thumb_inflight.get(cache_key) is slot:
                _thumb_inflight.pop(cache_key, None)


def _src_cache_path(filepath):
    key = hashlib.md5(os.path.abspath(str(filepath)).encode()).hexdigest()
    suffix = Path(str(filepath)).suffix.lower() or '.bin'
    return PHOTO_SRC_CACHE_DIR / (key + suffix)


def _trim_src_cache():
    try:
        files = [p for p in PHOTO_SRC_CACHE_DIR.iterdir() if p.is_file() and not p.name.endswith('.part')]
    except OSError:
        return
    try:
        total = sum(p.stat().st_size for p in files)
    except OSError:
        return
    if total <= PHOTO_SRC_CACHE_MAX_BYTES:
        return
    files.sort(key=lambda p: p.stat().st_mtime)
    for path in files:
        if total <= int(PHOTO_SRC_CACHE_MAX_BYTES * 0.8):
            break
        try:
            size = path.stat().st_size
            path.unlink()
            total -= size
        except OSError:
            continue


def hydrate_photo_source(filepath, wait=True):
    """Copy a Drive PNG onto local SSD so PIL does not wait on File Provider.

    wait=False is for ahead-of-scroll warming: skip rather than steal a
    slot from a /thumb/ the browser is already waiting on.
    """
    dest = _src_cache_path(filepath)
    global _src_trim_calls
    try:
        if dest.is_file() and dest.stat().st_size > 32:
            return dest
    except OSError:
        pass
    acquired = _hydrate_semaphore.acquire(timeout=None if wait else 0)
    if not acquired:
        return Path(filepath)
    try:
        try:
            if dest.is_file() and dest.stat().st_size > 32:
                return dest
        except OSError:
            pass
        PHOTO_SRC_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(dest.name + '.part')
        with open(filepath, 'rb') as src, open(tmp, 'wb') as out:
            shutil.copyfileobj(src, out, 1024 * 1024)
        tmp.replace(dest)
        _src_trim_calls += 1
        if _src_trim_calls % 20 == 0:
            _trim_src_cache()
        return dest
    except OSError:
        return Path(filepath)
    finally:
        _hydrate_semaphore.release()


def _render_photo_thumbnail(filepath, cache_key):
    try:
        from PIL import Image
        source = hydrate_photo_source(filepath, wait=True)
        img = Image.open(source)
        if getattr(img, 'format', None) == 'JPEG' and hasattr(img, 'draft'):
            try:
                img.draft('RGB', PHOTO_THUMB_SIZE)
            except Exception:
                pass
        if img.mode not in ('RGB', 'L'):
            img = img.convert('RGB')
        img.thumbnail(PHOTO_THUMB_SIZE, Image.BILINEAR)
        buf = io.BytesIO()
        img.save(buf, format='WEBP', quality=70, method=0)
        stored = _store_thumb(cache_key, '.webp', buf.getvalue(), 'image/webp')
        if stored:
            return stored
        if img.mode != 'RGB':
            img = img.convert('RGB')
        buf = io.BytesIO()
        img.save(buf, format='JPEG', quality=70)
        return _store_thumb(cache_key, '.jpg', buf.getvalue(), 'image/jpeg')
    except Exception:
        return None


def generate_photo_thumbnail(filepath, background=False):
    """Resize a still to the grid size. Returns (data, mime) or None.

    background=True is for prewarm: skip rather than delay a /thumb/ the
    grid is waiting on. Opening a PNG off Google Drive File Provider is the
    slow part (~1–5s cold); don't spend those slots on off-screen files.
    """
    cache_key = _photo_cache_key(filepath)
    cached = get_disk_thumb(cache_key)
    if cached:
        return cached
    if background:
        if _photo_ondemand_busy():
            return None
        with _thumb_inflight_lock:
            slot = _thumb_inflight.get(cache_key)
        if slot is not None:
            slot.event.wait(timeout=180)
            return slot.result or get_disk_thumb(cache_key)
        acquired = _photo_thumb_semaphore.acquire(timeout=0.2)
        if not acquired:
            return None
        try:
            if _photo_ondemand_busy():
                return get_disk_thumb(cache_key)
            cached = get_disk_thumb(cache_key)
            if cached:
                return cached
            return _render_photo_thumbnail(filepath, cache_key)
        finally:
            _photo_thumb_semaphore.release()

    def factory():
        cached = get_disk_thumb(cache_key)
        if cached:
            return cached
        with _photo_thumb_semaphore:
            cached = get_disk_thumb(cache_key)
            if cached:
                return cached
            return _render_photo_thumbnail(filepath, cache_key)

    _photo_ondemand_begin()
    try:
        return _single_flight_photo(cache_key, factory)
    finally:
        _photo_ondemand_end()


def generate_media_thumbnail(filepath, background=False):
    suffix = filepath.suffix.lower()
    if suffix in IMAGE_EXTENSIONS:
        return generate_photo_thumbnail(filepath, background=background)
    if suffix in VIDEO_EXTENSIONS:
        return generate_video_thumbnail(filepath)
    return None


_prewarm_state = {'done': 0, 'missing': None, 'running': False}
_photo_thumb_stats_lock = threading.Lock()
_photo_thumb_stats = {'cached': 0, 'total': 0}
_photo_thumb_slices = {}

# Paths the browser is actually showing right now. Filtering jumps to a month or
# day the sequential prewarm walk has not reached yet, so those grids used to run
# ffmpeg on demand for every tile.
_warm_queue = []
_warm_seen = set()
_warm_lock = threading.Lock()
WARM_QUEUE_LIMIT = 600


def queue_warm_paths(rel_paths, reset=False, front=False):
    """Queue visible video paths ahead of the sequential prewarm walk.

    A new view resets the queue so stale months stop competing; later pages of
    the same view append, which keeps warming in the order they are scrolled.
    """
    added = 0
    with _warm_lock:
        if reset:
            _warm_queue.clear()
            _warm_seen.clear()
        for rel in rel_paths:
            if not rel or rel in _warm_seen:
                continue
            _warm_seen.add(rel)
            if front:
                _warm_queue.insert(0, rel)
            else:
                _warm_queue.append(rel)
            added += 1
        while len(_warm_queue) > WARM_QUEUE_LIMIT:
            _warm_seen.discard(_warm_queue.pop())
    return added


def promote_warm_path(rel):
    """Put a visible miss at the front, even if listing already queued it."""
    if not rel:
        return
    with _warm_lock:
        try:
            _warm_queue.remove(rel)
        except ValueError:
            pass
        _warm_seen.add(rel)
        _warm_queue.insert(0, rel)
        while len(_warm_queue) > WARM_QUEUE_LIMIT:
            dropped = _warm_queue.pop()
            if dropped != rel:
                _warm_seen.discard(dropped)


def lookup_photo_thumb(rel, src_mtime=None):
    """Serve a cached still, or queue a miss. Never decode Drive on this thread.

    Video /vthumb/ can ffmpeg a frame on the request because that is fast.
    Opening a Drive PNG is not — it holds Chrome's 6 connections for seconds
    and the grid looks frozen. Workers encode; the client retries with <img>.
    If a worker is already encoding this file, wait briefly so the first
    retry can return 200 instead of another 503.
    """
    cache_key = _cache_key_from_rel(rel, ':400webp')
    found = get_disk_thumb(cache_key, src_mtime)
    if found:
        return found
    queue_warm_paths([rel])
    with _thumb_inflight_lock:
        slot = _thumb_inflight.get(cache_key)
    if slot is not None:
        slot.event.wait(timeout=1.25)
        return get_disk_thumb(cache_key, src_mtime)
    return None


def _warm_queue_pending():
    with _warm_lock:
        return bool(_warm_queue)


def _next_warm_path():
    with _warm_lock:
        if not _warm_queue:
            return None
        rel = _warm_queue.pop(0)
        _warm_seen.discard(rel)
        return rel


def _drain_warm_queue():
    """Encode visible stills on worker threads so /thumb/ stays cache-only."""
    while True:
        rel = _next_warm_path()
        if rel is None:
            return
        with _active_streams_lock:
            streaming = _active_streams > 0
        rel_l = rel.lower()
        is_photo = Path(rel_l).suffix in IMAGE_EXTENSIONS
        if streaming and not is_photo:
            queue_warm_paths([rel], reset=False, front=True)
            return
        variant = ':400webp' if is_photo else ':v400webp'
        if get_disk_thumb(_cache_key_from_rel(rel, variant)):
            continue
        filepath = resolve_media_path(rel)
        try:
            if not filepath or not filepath.is_file():
                continue
        except OSError:
            continue
        if is_photo:
            # Blocking encode: marks ondemand so the library walk yields.
            result = generate_photo_thumbnail(filepath)
        else:
            result = generate_media_thumbnail(filepath, background=True)
        if result:
            _prewarm_state['done'] += 1
        elif is_photo:
            queue_warm_paths([rel], reset=False, front=True)
            return


def _warm_photo_listing(page, offset, limit, month=None, q=None, day=None):
    """Start Drive copies for the tiles in this response and aim prewarm at this view."""
    set_photo_focus(month, day)
    if STORAGE_MODE == 'drive' or not PREWARM_ENABLED:
        return
    paths = [item['path'] for item in (page or []) if item.get('path')]
    if not paths:
        return
    queue_warm_paths(paths, reset=(offset == 0))


def _warm_queue_worker():
    """Keep visible-grid paths ahead of the sequential library walk."""
    while True:
        try:
            _drain_warm_queue()
        except Exception:
            pass
        time.sleep(0.05)


def _prewarm_worker(worker_id=0, worker_count=1):
    """Generate missing video thumbnails while nothing is streaming, so the
    grid serves cache hits instead of running ffmpeg during browsing."""
    time.sleep(8 + worker_id)
    while True:
        try:
            videos = get_videos_cached()
        except OSError:
            time.sleep(60)
            continue

        pending = 0
        for position, item in enumerate(videos):
            # What the user is looking at outranks the sequential walk.
            _drain_warm_queue()
            if position % worker_count != worker_id:
                continue
            # Never compete with an active playback session.
            while True:
                with _active_streams_lock:
                    busy = _active_streams > 0
                if not busy:
                    break
                time.sleep(3)

            if get_disk_thumb(_cache_key_from_rel(item['path'], ':v400webp')):
                continue
            filepath = resolve_media_path(item['path'])
            try:
                if not filepath or not filepath.is_file():
                    continue
                if get_cached_video_thumbnail(filepath):
                    continue
            except OSError:
                continue

            pending += 1
            _prewarm_state['running'] = True
            generate_video_thumbnail(filepath)
            _prewarm_state['done'] += 1
            _prewarm_state['running'] = False
            time.sleep(0.05)

        if pending:
            print(f'   [prewarm] worker {worker_id}: generated {pending} thumbnails')
        # Stay responsive to the grid instead of sleeping through a filter change.
        for _ in range(60):
            _drain_warm_queue()
            time.sleep(5)


def photo_thumb_progress():
    with _photo_thumb_stats_lock:
        cached = _photo_thumb_stats.get('cached', 0)
        if _photo_thumb_slices:
            cached = max(cached, sum(hits for hits, _seen in _photo_thumb_slices.values()))
        total = _photo_thumb_stats.get('total', 0)
    return {
        'cached': cached,
        'total': total,
        'running': bool(PREWARM_ENABLED and STORAGE_MODE != 'drive'),
    }


def _note_photo_slice(worker_id, hits, seen, total=None):
    with _photo_thumb_stats_lock:
        _photo_thumb_slices[worker_id] = (hits, seen)
        if total is not None:
            _photo_thumb_stats['total'] = total
        _photo_thumb_stats['cached'] = sum(h for h, _s in _photo_thumb_slices.values())


def _prewarm_photos_worker(worker_id=0, worker_count=1):
    """Walk every still newest-first and write grid thumbs to local disk.

    Yields while the browser is waiting on /thumb/ so the visible month stays
    first, then resumes until the whole library is cached.
    """
    time.sleep(2 + worker_id)
    while True:
        try:
            photos, focus_gen = _focused_photo_items()
        except OSError:
            time.sleep(60)
            continue
        pending = 0
        hits = 0
        seen = 0
        interrupted = False
        _note_photo_slice(worker_id, 0, 0, total=len(photos))
        for position, item in enumerate(photos):
            _drain_warm_queue()
            with _photo_focus_lock:
                if _photo_focus.get('gen', 0) != focus_gen:
                    interrupted = True
                    break
            if position % worker_count != worker_id:
                continue
            seen += 1
            rel = item.get('path') or ''
            while not _photo_thumb_on_disk(rel):
                while _photo_ondemand_busy() or _warm_queue_pending():
                    _drain_warm_queue()
                    time.sleep(0.05)
                with _active_streams_lock:
                    streaming = _active_streams > 0
                if streaming:
                    time.sleep(1)
                    continue
                filepath = resolve_media_path(rel)
                try:
                    if not filepath or not filepath.is_file():
                        break
                except OSError:
                    break
                result = generate_photo_thumbnail(filepath, background=True)
                if result:
                    pending += 1
                    _prewarm_state['done'] += 1
                    break
                if not _photo_ondemand_busy():
                    break
                time.sleep(0.1)
            if _photo_thumb_on_disk(rel):
                hits += 1
            if seen % 25 == 0:
                _note_photo_slice(worker_id, hits, seen, total=len(photos))
        _note_photo_slice(worker_id, hits, seen, total=len(photos))
        if pending:
            print(
                f'   [prewarm] photos {worker_id}: generated {pending} '
                f'({hits}/{seen} cached in this slice)'
            )
        if interrupted:
            continue
        for _ in range(8):
            _drain_warm_queue()
            with _photo_focus_lock:
                if _photo_focus.get('gen', 0) != focus_gen:
                    break
            time.sleep(2)


class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True

    def process_request_thread(self, request, client_address):
        try:
            self.finish_request(request, client_address)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass
        except Exception:
            self.handle_error(request, client_address)
        finally:
            self.shutdown_request(request)


def extract_image_metadata(filepath):
    """Extract ComfyUI metadata from PNG tEXt/zTXt chunks or WebP EXIF."""
    meta = {
        'file': str(filepath),
        'size': filepath.stat().st_size,
        'modified': filepath.stat().st_mtime,
    }

    suffix = filepath.suffix.lower()

    try:
        from PIL import Image
        from PIL.PngImagePlugin import PngInfo
        img = Image.open(filepath)
        meta['format'] = img.format
        meta['dimensions'] = f'{img.width}x{img.height}'
        meta['mode'] = img.mode

        if suffix == '.png':
            # PNG stores ComfyUI data in tEXt chunks
            if hasattr(img, 'text'):
                for key, value in img.text.items():
                    # Try to parse as JSON (prompt, workflow)
                    try:
                        meta[key] = json.loads(value)
                    except (json.JSONDecodeError, TypeError):
                        meta[key] = value[:2000] if len(value) > 2000 else value

        elif suffix in ('.webp', '.jpg', '.jpeg'):
            # WebP/JPEG may store in EXIF UserComment
            exif = img.getexif()
            if exif:
                # UserComment tag (0x9286)
                user_comment = exif.get(0x9286, '')
                if user_comment:
                    try:
                        meta['userComment'] = json.loads(user_comment)
                    except:
                        meta['userComment'] = str(user_comment)[:2000]
            # Also check info dict
            if hasattr(img, 'info'):
                for key in ('prompt', 'workflow', 'parameters'):
                    if key in img.info:
                        try:
                            meta[key] = json.loads(img.info[key])
                        except:
                            meta[key] = str(img.info[key])[:2000]

        img.close()
    except ImportError:
        meta['error'] = 'Pillow not installed'
    except Exception as e:
        meta['error'] = str(e)

    return meta


def _is_comfy_link(value):
    return (
        isinstance(value, list)
        and len(value) == 2
        and isinstance(value[0], (str, int))
        and isinstance(value[1], int)
    )


def _parse_serialized_link(value):
    """Subgraph widgets serialize links as '1011:266,0' instead of ['1011:266', 0]."""
    if not isinstance(value, str):
        return None
    m = re.match(r'^(\d+(?::\d+)+),(\d+)$', value.strip())
    if not m:
        return None
    return [m.group(1), int(m.group(2))]


def _is_string_primitive(class_type):
    ct = class_type or ''
    return ct.startswith('PrimitiveString')


def coerce_comfy_prompt(obj):
    """Return a ComfyUI API prompt dict, or None."""
    if isinstance(obj, str):
        text = obj.strip()
        if not text:
            return None
        try:
            obj = json.loads(text)
        except json.JSONDecodeError:
            return None
    if not isinstance(obj, dict) or not obj:
        return None
    if any(isinstance(v, dict) and v.get('class_type') for v in obj.values()):
        return obj
    inner = obj.get('prompt')
    if isinstance(inner, dict) and any(
        isinstance(v, dict) and v.get('class_type') for v in inner.values()
    ):
        return inner
    return None


def _node_title(node):
    meta = node.get('_meta') if isinstance(node, dict) else None
    if isinstance(meta, dict) and meta.get('title'):
        return str(meta['title'])
    return ''


def _is_sampler_node(class_type):
    ct = class_type or ''
    return 'KSampler' in ct or ct.endswith('Sampler') or 'SamplerCustom' in ct


def _is_text_encode_node(class_type):
    ct = class_type or ''
    return (
        'CLIPTextEncode' in ct
        or 'TextEncode' in ct
        or 'PromptEncode' in ct
        or 'WildcardEncode' in ct
        or 'CLIPText' in ct
        or 'Hunyuan' in ct
        or 'WanVideo' in ct
        or 'WanText' in ct
    )


def _is_video_prompt_context(class_type, title):
    blob = f'{class_type or ""} {title or ""}'.lower()
    if any(skip in blob for skip in ('loadvideo', 'savevideo', 'previewvideo')):
        if not any(keep in blob for keep in ('textencode', 'cliptext', 'promptencode')):
            return False
    return any(token in blob for token in ('video', 'wan', 'hunyuan', 'i2v', 't2v', 'ltx'))


def _is_generic_prompt_title(title):
    t = (title or '').strip()
    if not t:
        return True
    low = t.lower()
    if 'clip text encode' in low or 'cliptextencode' in low.replace(' ', ''):
        return True
    return bool(re.match(r'^(prompt|value|positive|negative)$', t, re.I))


_JUNK_PROMPT_RE = re.compile(r'^\d+:\d+(?:,\d+)*$')
_JUNK_SHORT_NUM_RE = re.compile(r'^\d+([.:]\d+){1,4}$')


def _is_junk_prompt_value(value):
    if value is None:
        return False
    if isinstance(value, bool) or isinstance(value, (int, float)):
        return True
    if isinstance(value, (list, tuple, dict)):
        return True
    if not isinstance(value, str):
        return True
    s = value.strip()
    if not s:
        return False
    if _JUNK_PROMPT_RE.match(s):
        return True
    if len(s) < 24 and _JUNK_SHORT_NUM_RE.match(s):
        return True
    return False


def _is_clip_encode_name(class_type='', title='', label=''):
    blob = f'{class_type or ""} {title or ""} {label or ""}'.lower()
    return 'cliptextencode' in class_type or 'clip text encode' in blob


def _is_prompt_input_key(key, class_type='', title='', value=None, nid=None, pos_ids=None, neg_ids=None):
    if _is_junk_prompt_value(value):
        return False
    if key in {
        'text', 'text_g', 'text_l', 'prompt', 'positive', 'negative',
        'positive_prompt', 'negative_prompt', 'wildcard',
        'text_positive', 'text_negative',
    }:
        return True
    if key != 'value':
        return False
    if isinstance(value, bool) or isinstance(value, (int, float)):
        return False
    if class_type.startswith('PrimitiveInt') or class_type.startswith('PrimitiveBoolean') or class_type.startswith('PrimitiveFloat'):
        return False
    pos_ids = pos_ids or set()
    neg_ids = neg_ids or set()
    nid = str(nid or '')
    if nid in pos_ids or nid in neg_ids:
        return True
    if _is_string_primitive(class_type):
        return isinstance(value, str)
    if _is_text_encode_node(class_type) or _is_video_prompt_context(class_type, title):
        return True
    if title and re.search(r'prompt|positive|negative|video', title, re.I):
        return True
    return isinstance(value, str) and len(value) > 40


def _collect_linked_ids(prompt, start_ids, hops=8):
    found = {str(i) for i in start_ids}
    queue = list(found)
    depth = 0
    while queue and depth < hops:
        nxt = []
        for nid in queue:
            node = prompt.get(nid)
            if node is None and nid.isdigit():
                node = prompt.get(int(nid))
            if not isinstance(node, dict):
                continue
            for value in (node.get('inputs') or {}).values():
                if not _is_comfy_link(value):
                    continue
                sid = str(value[0])
                if sid in found:
                    continue
                found.add(sid)
                nxt.append(sid)
        queue = nxt
        depth += 1
    return found


def _prompt_role(nid, key, title, pos_ids, neg_ids, class_type=''):
    if str(nid) in pos_ids:
        return 'positive'
    if str(nid) in neg_ids:
        return 'negative'
    blob = f'{key or ""} {title or ""} {class_type or ""}'.lower()
    if 'negative' in blob or blob.endswith('neg'):
        return 'negative'
    if 'positive' in blob:
        return 'positive'
    if key in ('text_g', 'text_l'):
        return 'positive'
    if key == 'value' and (
        _is_text_encode_node(class_type)
        or _is_video_prompt_context(class_type, title)
        or re.search(r'prompt|video', title or '', re.I)
    ):
        return 'positive'
    if key == 'text' and (
        _is_video_prompt_context(class_type, title)
        or re.match(r'^prompt$', (title or '').strip(), re.I)
    ):
        return 'positive'
    return 'prompt'


def _is_image_encode_field(class_type='', key=''):
    return 'CLIPTextEncode' in (class_type or '') and key in {
        'text', 'text_g', 'text_l', 'prompt', 'positive_prompt', 'negative_prompt',
    }


def _prompt_label(role, title, nid, key, class_type='', image=False):
    video = _is_video_prompt_context(class_type, title) or key == 'value'
    use_image = image and _is_image_encode_field(class_type, key) and not video
    if role == 'positive':
        role_name = 'Video positive' if video else ('Image positive' if use_image else 'Positive')
    elif role == 'negative':
        role_name = 'Video negative' if video else ('Image negative' if use_image else 'Negative')
    else:
        role_name = 'Video prompt' if video else ('Image prompt' if use_image else 'Prompt')
    title = (title or '').strip()
    if title and not _is_generic_prompt_title(title) and role_name.lower() in title.lower():
        return title
    if title and not _is_generic_prompt_title(title):
        return f'{role_name} · {title}'
    return role_name


def _is_stock_clip_text_field(field):
    ct = field.get('class_type') or ''
    if 'CLIPTextEncode' in ct:
        return True
    blob = f"{ct} {field.get('label') or ''}".lower()
    return 'clip text encode' in blob


def _graph_has_video_prompt(prompts):
    for f in prompts:
        ct = f.get('class_type') or ''
        if f.get('key') == 'value' and not _is_junk_prompt_value(f.get('value')):
            return True
        if _is_video_prompt_context(ct, f.get('label') or ''):
            return True
        if 'PrimitiveString' in ct:
            return True
    return False


def _demote_stock_clip_text_fields(prompts, advanced):
    has_video = _graph_has_video_prompt(prompts)
    kept = []
    for field in prompts:
        if _is_junk_prompt_value(field.get('value')):
            field['section'] = 'advanced'
            advanced.append(field)
            continue
        clip = _is_stock_clip_text_field(field)
        key = field.get('key') or ''
        if clip and key not in {
            'text', 'text_g', 'text_l', 'value', 'prompt',
            'positive', 'negative', 'positive_prompt', 'negative_prompt',
        }:
            field['section'] = 'advanced'
            field['label'] = f"CLIP Text Encode · {key or 'text'}"
            advanced.append(field)
            continue
        if clip and (
            has_video
            or 'clip text encode' in (field.get('label') or '').lower()
        ):
            field['label'] = _prompt_label(
                field.get('role'), '', field.get('node_id'), key,
                field.get('class_type') or '', image=has_video,
            )
        kept.append(field)
    return kept, advanced


def _dedupe_prompt_labels(fields):
    counts = {}
    for f in fields:
        if f.get('section') == 'prompts':
            counts[f['label']] = counts.get(f['label'], 0) + 1
    for f in fields:
        if f.get('section') == 'prompts' and counts.get(f['label'], 0) > 1:
            f['label'] = f"{f['label']} · #{f['node_id']}"


def _is_advanced_node(class_type):
    ct = class_type or ''
    needles = (
        'VAE', 'ControlNet', 'IPAdapter', 'InstantID', 'InsightFace',
        'Preview', 'SaveImage', 'SaveVideo', 'VHS_', 'LoadImage',
        'LoadVideo', 'ConditioningCombine', 'ConditioningConcat',
        'ConditioningSetArea',
    )
    return any(n in ct for n in needles)


def _field_input_type(key, value):
    if isinstance(value, bool):
        return 'checkbox'
    if isinstance(value, int):
        return 'number'
    if isinstance(value, float):
        return 'number'
    if key in ('seed', 'steps', 'cfg', 'denoise', 'width', 'height', 'batch_size'):
        return 'number'
    if isinstance(value, str) and len(value) > 80:
        return 'textarea'
    return 'text'


def prompt_to_fields(prompt):
    """Turn an API prompt into form fields. CLIPTextEncode first."""
    if not isinstance(prompt, dict):
        return []
    pos_seeds = []
    neg_seeds = []
    for node in prompt.values():
        if not isinstance(node, dict):
            continue
        inputs = node.get('inputs') or {}
        if not _is_sampler_node(node.get('class_type') or ''):
            continue
        pos = inputs.get('positive')
        neg = inputs.get('negative')
        if _is_comfy_link(pos):
            pos_seeds.append(str(pos[0]))
        if _is_comfy_link(neg):
            neg_seeds.append(str(neg[0]))
    pos_ids = _collect_linked_ids(prompt, pos_seeds)
    neg_ids = _collect_linked_ids(prompt, neg_seeds)

    prompts = []
    widgets = []
    advanced = []
    for nid, node in prompt.items():
        if not isinstance(node, dict):
            continue
        nid = str(nid)
        ct = node.get('class_type') or ''
        inputs = node.get('inputs') or {}
        title = _node_title(node)
        prompt_keys = []
        for key, value in inputs.items():
            if _is_comfy_link(value) or _parse_serialized_link(value):
                continue
            if isinstance(value, bool) or isinstance(value, (int, float)):
                continue
            if _is_prompt_input_key(key, class_type=ct, title=title, value=value, nid=nid, pos_ids=pos_ids, neg_ids=neg_ids):
                prompt_keys.append(key)
        used = set(prompt_keys)
        for key in prompt_keys:
            role = _prompt_role(nid, key, title, pos_ids, neg_ids, ct)
            prompts.append({
                'node_id': nid,
                'key': key,
                'label': _prompt_label(role, title, nid, key, ct),
                'role': role,
                'type': 'textarea',
                'section': 'prompts',
                'value': inputs.get(key, ''),
                'class_type': ct,
            })
        if (_is_text_encode_node(ct) or _is_string_primitive(ct)) and prompt_keys:
            continue
        section = 'advanced' if (_is_advanced_node(ct) or _is_clip_encode_name(ct, title)) else 'widgets'
        bucket = advanced if section == 'advanced' else widgets
        for key, value in inputs.items():
            if key in used or _is_comfy_link(value) or _parse_serialized_link(value):
                continue
            bucket.append({
                'node_id': nid,
                'key': key,
                'label': f'{title or ct} · {key}',
                'role': key,
                'type': _field_input_type(key, value),
                'section': section,
                'value': value,
                'class_type': ct,
            })

    role_rank = {'positive': 0, 'negative': 1, 'prompt': 2}
    def _kind_rank(field):
        label = field.get('label') or ''
        if label.startswith('Image'):
            return 0
        if label.startswith('Video') or field.get('key') == 'value':
            return 1
        return 0
    def _key_rank(field):
        key = field.get('key') or ''
        if key in ('text', 'text_g', 'text_l'):
            return 0
        if key == 'value':
            return 1
        if 'prompt' in key:
            return 1
        return 2
    prompts, advanced = _demote_stock_clip_text_fields(prompts, advanced)
    prompts.sort(key=lambda f: (
        role_rank.get(f['role'], 9), _kind_rank(f), _key_rank(f), f['node_id'], f.get('key') or '',
    ))
    fields = prompts + widgets + advanced
    _dedupe_prompt_labels(fields)
    return fields


def _read_json_file(path):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None


def _prompt_from_image_file(filepath):
    meta = extract_image_metadata(filepath)
    for key in ('prompt', 'workflow', 'parameters', 'userComment'):
        prompt = coerce_comfy_prompt(meta.get(key))
        if prompt:
            return prompt, f'image:{key}'
    return None, None


def _prompt_from_sidecar(filepath):
    path = Path(filepath)
    candidates = [
        Path(str(path) + '.json'),
        path.with_suffix('.json'),
        path.with_name(path.stem + '.prompt.json'),
    ]
    seen = set()
    for candidate in candidates:
        resolved = str(candidate)
        if resolved in seen:
            continue
        seen.add(resolved)
        if not candidate.is_file():
            continue
        prompt = coerce_comfy_prompt(_read_json_file(candidate))
        if prompt:
            return prompt, f'sidecar:{candidate.name}'
    return None, None


def _prompt_from_ffprobe(filepath):
    if not _ffprobe_path:
        return None, None
    try:
        result = subprocess.run(
            [
                _ffprobe_path, '-v', 'quiet', '-print_format', 'json',
                '-show_format', '-show_streams', str(filepath),
            ],
            capture_output=True, text=True, timeout=20,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None, None
    if result.returncode != 0 or not result.stdout:
        return None, None
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None, None
    blobs = []
    fmt = data.get('format') or {}
    tags = fmt.get('tags') or {}
    blobs.extend(tags.values())
    for stream in data.get('streams') or []:
        blobs.extend((stream.get('tags') or {}).values())
    for blob in blobs:
        prompt = coerce_comfy_prompt(blob)
        if prompt:
            return prompt, 'ffprobe'
    return None, None


def _prompt_from_sibling_image(filepath):
    path = Path(filepath)
    for suffix in ('.png', '.webp', '.jpg', '.jpeg'):
        sibling = path.with_suffix(suffix)
        if sibling.is_file():
            prompt, source = _prompt_from_image_file(sibling)
            if prompt:
                return prompt, f'sibling:{sibling.name}'
    return None, None


def extract_comfy_workflow(filepath):
    """Load API prompt + form fields from a media file. Missing prompt is not an error."""
    path = Path(filepath)
    suffix = path.suffix.lower()
    prompt = None
    source = None
    if suffix in IMAGE_EXTENSIONS:
        prompt, source = _prompt_from_image_file(path)
        if not prompt:
            prompt, source = _prompt_from_sidecar(path)
    else:
        prompt, source = _prompt_from_sidecar(path)
        if not prompt:
            prompt, source = _prompt_from_ffprobe(path)
        if not prompt:
            prompt, source = _prompt_from_sibling_image(path)
    payload = {
        'path': str(path),
        'name': path.name,
        'prompt': prompt,
        'fields': prompt_to_fields(prompt) if prompt else [],
        'source': source,
    }
    if not prompt:
        payload['error'] = 'No ComfyUI prompt found in this file'
    return payload


_PNG_SIGNATURE = b'\x89PNG\r\n\x1a\n'
DRIVE_SIDECAR_MAX_BYTES = 8 * 1024 * 1024
DRIVE_IMAGE_PREFIX_BYTES = 8 * 1024 * 1024
DRIVE_IMAGE_MAX_BYTES = 48 * 1024 * 1024
DRIVE_VIDEO_PROBE_BYTES = 4 * 1024 * 1024
_NO_PROMPT_ERROR = 'No ComfyUI prompt found in this file'


def read_png_text_chunks(data):
    """Read PNG tEXt/zTXt/iTXt chunks without decoding pixels.

    Returns (texts, finished). finished is true once IDAT or IEND is reached,
    so a prefix that already includes those chunks does not need the rest of the file.
    """
    texts = {}
    if not isinstance(data, (bytes, bytearray)) or not data.startswith(_PNG_SIGNATURE):
        return texts, False
    import zlib
    pos = len(_PNG_SIGNATURE)
    size = len(data)
    while pos + 8 <= size:
        length = int.from_bytes(data[pos:pos + 4], 'big')
        if length > 64 * 1024 * 1024:
            return texts, False
        ctype = bytes(data[pos + 4:pos + 8])
        start = pos + 8
        end = start + length
        if end + 4 > size:
            return texts, False
        chunk = bytes(data[start:end])
        if ctype in (b'tEXt', b'zTXt', b'iTXt'):
            parsed = _decode_png_text_chunk(ctype, chunk, zlib)
            if parsed:
                key, value = parsed
                texts.setdefault(key, value)
        elif ctype in (b'IDAT', b'IEND'):
            return texts, True
        pos = end + 4
    return texts, False


def _decode_png_text_chunk(ctype, chunk, zlib_mod):
    try:
        if ctype == b'tEXt':
            key, _, value = chunk.partition(b'\x00')
            if not key:
                return None
            return key.decode('latin1', 'replace'), value.decode('utf-8', 'replace')
        if ctype == b'zTXt':
            key, _, rest = chunk.partition(b'\x00')
            if not key or len(rest) < 2:
                return None
            value = zlib_mod.decompress(rest[1:])
            return key.decode('latin1', 'replace'), value.decode('utf-8', 'replace')
        if ctype == b'iTXt':
            sep = chunk.find(b'\x00')
            if sep < 0 or sep + 3 > len(chunk):
                return None
            key = chunk[:sep]
            comp_flag = chunk[sep + 1]
            rest = chunk[sep + 3:]
            _lang, sep2, rest = rest.partition(b'\x00')
            if sep2 != b'\x00':
                return None
            _translated, sep3, text = rest.partition(b'\x00')
            if sep3 != b'\x00':
                return None
            if comp_flag == 1:
                text = zlib_mod.decompress(text)
            return key.decode('latin1', 'replace'), text.decode('utf-8', 'replace')
    except Exception:
        return None
    return None


def drive_companion_names(filename):
    """Sidecar JSON names, then same-stem stills, matching the local extractor."""
    path = Path(filename)
    sidecars = []
    for name in (f'{filename}.json', f'{path.stem}.json', f'{path.stem}.prompt.json'):
        if name and name not in sidecars:
            sidecars.append(name)
    siblings = []
    for ext in ('.png', '.webp', '.jpg', '.jpeg'):
        name = f'{path.stem}{ext}'
        if name not in siblings and name != filename:
            siblings.append(name)
    return sidecars, siblings


def _workflow_cache_file(file_id):
    digest = hashlib.sha256(str(file_id).encode()).hexdigest()
    return CACHE_ROOT / 'drive_workflow' / f'{digest}.json'


def _read_workflow_cache(meta):
    file_id = meta.get('id')
    if not file_id:
        return None
    try:
        data = json.loads(_workflow_cache_file(file_id).read_text(encoding='utf-8'))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    if data.get('size') != int(meta.get('size') or 0):
        return None
    if data.get('modified') != meta.get('modified'):
        return None
    payload = data.get('payload')
    return payload if isinstance(payload, dict) and payload.get('prompt') else None


def _write_workflow_cache(meta, payload):
    file_id = meta.get('id')
    if not file_id or not payload or not payload.get('prompt'):
        return
    path = _workflow_cache_file(file_id)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            'size': int(meta.get('size') or 0),
            'modified': meta.get('modified'),
            'payload': payload,
        }), encoding='utf-8')
    except OSError:
        pass


def _empty_workflow(rel, name, error=_NO_PROMPT_ERROR):
    return {
        'path': rel,
        'name': name,
        'prompt': None,
        'fields': [],
        'source': None,
        'error': error,
    }


def _workflow_from_prompt(rel, name, prompt, source):
    return {
        'path': rel,
        'name': name,
        'prompt': prompt,
        'fields': prompt_to_fields(prompt) if prompt else [],
        'source': source,
    }


def _workflow_from_image_bytes(name, data):
    suffix = Path(name).suffix.lower() or '.img'
    fd, tmp_name = tempfile.mkstemp(suffix=suffix)
    try:
        with os.fdopen(fd, 'wb') as handle:
            handle.write(data)
        return extract_comfy_workflow(tmp_name)
    finally:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass


def _prompt_from_image_bytes(name, data, prefix_only):
    """Return (prompt, source, done). done means a larger download will not help."""
    if not data:
        return None, None, False
    if Path(name).suffix.lower() == '.png' or data.startswith(_PNG_SIGNATURE):
        texts, finished = read_png_text_chunks(data)
        for key in ('prompt', 'workflow', 'parameters'):
            prompt = coerce_comfy_prompt(texts.get(key))
            if prompt:
                return prompt, f'image:{key}', True
        if finished or not prefix_only:
            return None, None, True
        return None, None, False
    try:
        payload = _workflow_from_image_bytes(name, data)
    except Exception:
        payload = None
    if payload and payload.get('prompt'):
        return payload['prompt'], payload.get('source'), True
    if prefix_only:
        return None, None, False
    return None, None, True


def _download_drive_image_prompt(drive, item, name):
    file_id = item.get('id')
    size = int(item.get('size') or 0)
    if not file_id:
        return None, None
    if size and size <= DRIVE_IMAGE_PREFIX_BYTES:
        raw = drive.download_bytes(file_id, max(size, 1))
        if not raw:
            return None, None
        prompt, source, _done = _prompt_from_image_bytes(name, raw, prefix_only=False)
        return prompt, source
    raw = drive.download_bytes(
        file_id,
        DRIVE_IMAGE_PREFIX_BYTES,
        range_header=f'bytes=0-{DRIVE_IMAGE_PREFIX_BYTES - 1}',
    )
    prompt, source, done = _prompt_from_image_bytes(name, raw, prefix_only=True)
    if prompt or done:
        return prompt, source
    if size and size > DRIVE_IMAGE_MAX_BYTES:
        return None, None
    raw = drive.download_bytes(file_id, DRIVE_IMAGE_MAX_BYTES)
    if not raw:
        return None, None
    prompt, source, _done = _prompt_from_image_bytes(name, raw, prefix_only=False)
    return prompt, source


def _balanced_json_object(text, start, limit=2_000_000):
    if start < 0 or start >= len(text) or text[start] != '{':
        return None
    depth = 0
    in_str = False
    escaped = False
    end_limit = min(len(text), start + limit)
    for index in range(start, end_limit):
        ch = text[index]
        if in_str:
            if escaped:
                escaped = False
            elif ch == '\\':
                escaped = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == '{':
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0:
                return text[start:index + 1]
    return None


def prompt_from_media_bytes(data):
    """Find a ComfyUI API prompt embedded in a video or other binary blob."""
    if not data or b'class_type' not in data:
        return None
    text = data.decode('utf-8', errors='ignore')
    needle = '"class_type"'
    pos = 0
    while True:
        at = text.find(needle, pos)
        if at < 0:
            return None
        start = at
        for _hop in range(40):
            start = text.rfind('{', 0, start)
            if start < 0:
                break
            snippet = _balanced_json_object(text, start)
            if not snippet:
                continue
            prompt = coerce_comfy_prompt(snippet)
            if prompt:
                return prompt
        pos = at + len(needle)


def _download_drive_probe_chunks(drive, item):
    """Head and tail of a video. Comfy metadata sits in moov, which may be either end."""
    file_id = item.get('id')
    if not file_id:
        return []
    size = int(item.get('size') or 0)
    if size and size <= DRIVE_VIDEO_PROBE_BYTES:
        raw = drive.download_bytes(file_id, max(size, 1))
        return [raw] if raw else []
    chunks = []
    head = drive.download_bytes(
        file_id,
        DRIVE_VIDEO_PROBE_BYTES,
        range_header=f'bytes=0-{DRIVE_VIDEO_PROBE_BYTES - 1}',
    )
    if head:
        chunks.append(head)
    if size > DRIVE_VIDEO_PROBE_BYTES:
        start = size - DRIVE_VIDEO_PROBE_BYTES
        tail = drive.download_bytes(
            file_id,
            DRIVE_VIDEO_PROBE_BYTES,
            range_header=f'bytes={start}-{size - 1}',
        )
        if tail:
            chunks.append(tail)
    return chunks


def _prompt_from_drive_video(drive, item):
    for chunk in _download_drive_probe_chunks(drive, item):
        prompt = prompt_from_media_bytes(chunk)
        if prompt:
            return prompt
    return None


def _prompt_from_drive_sidecar(drive, item):
    size = int(item.get('size') or 0)
    if size > DRIVE_SIDECAR_MAX_BYTES or not item.get('id'):
        return None
    raw = drive.download_bytes(item['id'], DRIVE_SIDECAR_MAX_BYTES)
    if not raw:
        return None
    return coerce_comfy_prompt(raw.decode('utf-8', errors='replace'))


def extract_drive_workflow(drive, rel):
    """Load a ComfyUI prompt for a file in the shared Drive folder.

    Returns None when the media file itself is missing. Images are read from
    PNG text chunks (a prefix is enough). Videos use a sidecar JSON, prompt
    text embedded in the file, or a same-stem still in the same Drive folder.
    """
    meta = drive.get_meta(rel)
    if not meta or not meta.get('id'):
        return None
    name = meta.get('name') or Path(rel).name
    cached = _read_workflow_cache(meta)
    if cached:
        payload = dict(cached)
        payload['path'] = rel
        payload['name'] = name
        return payload

    suffix = Path(name).suffix.lower()
    sidecars, siblings = drive_companion_names(name)
    wanted = list(sidecars)
    if suffix not in IMAGE_EXTENSIONS:
        wanted.extend(siblings)
    try:
        found = drive.find_named_files(meta['id'], wanted) or {}
    except Exception:
        found = {}

    prompt = None
    source = None
    too_big = False
    if suffix in IMAGE_EXTENSIONS:
        size = int(meta.get('size') or 0)
        if size > DRIVE_IMAGE_MAX_BYTES:
            too_big = True
        else:
            prompt, source = _download_drive_image_prompt(drive, meta, name)
        if not prompt:
            for sidecar_name in sidecars:
                item = found.get(sidecar_name)
                if not item:
                    continue
                prompt = _prompt_from_drive_sidecar(drive, item)
                if prompt:
                    source = f"sidecar:{item.get('name') or sidecar_name}"
                    too_big = False
                    break
    else:
        for sidecar_name in sidecars:
            item = found.get(sidecar_name)
            if not item:
                continue
            prompt = _prompt_from_drive_sidecar(drive, item)
            if prompt:
                source = f"sidecar:{item.get('name') or sidecar_name}"
                break
        if not prompt:
            prompt = _prompt_from_drive_video(drive, meta)
            if prompt:
                source = 'video:metadata'
        if not prompt:
            for sibling_name in siblings:
                item = found.get(sibling_name)
                if not item:
                    continue
                if int(item.get('size') or 0) > DRIVE_IMAGE_MAX_BYTES:
                    too_big = True
                    continue
                prompt, source = _download_drive_image_prompt(drive, item, sibling_name)
                if prompt:
                    source = f'sibling:{sibling_name}'
                    too_big = False
                    break

    if not prompt:
        error = _NO_PROMPT_ERROR
        if too_big:
            error = (
                'This file is too large to read its ComfyUI prompt in cloud mode. '
                'Add a sidecar JSON next to it, or paste the API prompt.'
            )
        return _empty_workflow(rel, name, error)

    payload = _workflow_from_prompt(rel, name, prompt, source)
    _write_workflow_cache(meta, payload)
    return payload


def scan_videos(root_dir):
    """Scan VIDEO_DIR for video files, return paths relative to CWD for serving."""
    if STORAGE_MODE == 'drive':
        return get_drive_storage().scan_videos()
    videos = []
    scan_path = Path(VIDEO_DIR)
    if not scan_path.exists():
        return videos
    for path in scan_path.rglob('*'):
        if path.is_file() and path.suffix.lower() in VIDEO_EXTENSIONS:
            stat = path.stat()
            rel_path = str(path)  # relative to CWD, e.g. "ComfyUI/output/video/clip.mp4"
            entry = {
                'name': path.name,
                'path': rel_path,
                'size': stat.st_size,
                'modified': stat.st_mtime,
            }
            videos.append(entry)
    videos.sort(key=lambda v: v['modified'], reverse=True)
    return videos


def scan_photos(root_dir):
    """Scan PHOTO_DIRS for image files, return paths for serving."""
    if STORAGE_MODE == 'drive':
        return get_drive_storage().scan_photos()
    photos = []
    for photo_dir in PHOTO_DIRS:
        scan_path = Path(photo_dir)
        if not scan_path.exists():
            continue
        for path in scan_path.rglob('*'):
            if path.is_file() and path.suffix.lower() in IMAGE_EXTENSIONS:
                stat = path.stat()
                rel_path = str(path)  # relative to CWD for serving
                photos.append({
                    'name': path.name,
                    'path': rel_path,
                    'size': stat.st_size,
                    'modified': stat.st_mtime,
                })
    photos.sort(key=lambda v: v['modified'], reverse=True)
    return photos


def item_month_key(modified_ts):
    dt = datetime.fromtimestamp(modified_ts)
    return f'{dt.year}-{dt.month:02d}'


def item_day_key(modified_ts):
    dt = datetime.fromtimestamp(modified_ts)
    return f'{dt.year}-{dt.month:02d}-{dt.day:02d}'


def media_month_summary(items):
    counts = {}
    for item in items:
        key = item_month_key(item['modified'])
        counts[key] = counts.get(key, 0) + 1
    return [{'month': k, 'count': counts[k]} for k in sorted(counts.keys(), reverse=True)]


def media_day_summary(items, month=None):
    """Per-day counts, newest first. Limited to one month when given."""
    counts = {}
    for item in items:
        key = item_day_key(item['modified'])
        if month and not key.startswith(f'{month}-'):
            continue
        counts[key] = counts.get(key, 0) + 1
    return [{'day': k, 'count': counts[k]} for k in sorted(counts.keys(), reverse=True)]


def indexed_month_summary(kind, items=None):
    index = _media_by_month.get(kind)
    if not index and items is not None:
        _set_month_index(kind, items)
        index = _media_by_month.get(kind)
    if not index:
        return media_month_summary(items or [])
    return [{'month': k, 'count': len(index[k])} for k in sorted(index.keys(), reverse=True)]


def indexed_day_summary(kind, month, items=None):
    if not month:
        return None
    by_month = _media_by_month.get(kind)
    if not by_month and items is not None:
        _set_month_index(kind, items)
        by_month = _media_by_month.get(kind)
    if not by_month:
        return media_day_summary(items or [], month)
    return media_day_summary(by_month.get(month) or [])


def filter_media_items(items, month=None, q=None, kind='videos', day=None):
    """Return all items matching month/day/q filters (no pagination)."""
    filtered = items
    if month and not q:
        index = _media_by_month.get(kind)
        if index is None:
            _set_month_index(kind, items)
            index = _media_by_month.get(kind) or {}
        filtered = index.get(month, [])
    else:
        if month:
            filtered = [i for i in filtered if item_month_key(i['modified']) == month]
        if q:
            ql = q.lower()
            filtered = [i for i in filtered if ql in i['name'].lower()]
    if day and not q:
        day_index = _media_by_day.get(kind)
        if day_index is None:
            _set_month_index(kind, items)
            day_index = _media_by_day.get(kind) or {}
        filtered = day_index.get(day) or []
        if month and not str(day).startswith(f'{month}-'):
            return []
        return filtered
    if day:
        filtered = [i for i in filtered if item_day_key(i['modified']) == day]
    return filtered


def set_photo_focus(month=None, day=None):
    """Aim background thumb workers at the month/day the grid is showing."""
    month = month or None
    day = day or None
    with _photo_focus_lock:
        if _photo_focus['month'] == month and _photo_focus['day'] == day:
            return False
        _photo_focus['month'] = month
        _photo_focus['day'] = day
        _photo_focus['gen'] += 1
        return True


def _focused_photo_items():
    photos = get_photos_cached()
    with _photo_focus_lock:
        month = _photo_focus.get('month')
        day = _photo_focus.get('day')
        gen = _photo_focus.get('gen', 0)
    if month or day:
        return filter_media_items(photos, month=month, day=day, kind='photos'), gen
    return photos, gen


def pick_random_item(items, exclude_path=None):
    """Pick one random media item, preferring not to repeat exclude_path."""
    if not items:
        return None
    pool = items
    if exclude_path:
        filtered = [i for i in items if i.get('path') != exclude_path]
        if filtered:
            pool = filtered
    return random.choice(pool)


def pick_random_oriented_video(items, orientation, exclude_path=None):
    """Random pick restricted to horizontal or vertical clips.

    Orientation is resolved lazily while walking a shuffled pool: already known
    clips answer instantly, and only a bounded tail gets probed with ffprobe.
    Returns (item, matched) where matched is False when nothing could be
    confirmed and the caller is getting an unfiltered pick instead.
    """
    orientation = normalize_orientation(orientation)
    if not orientation:
        return pick_random_item(items, exclude_path=exclude_path), False
    if not items:
        return None, False
    pool = [i for i in items if i.get('path') != exclude_path] or list(items)
    random.shuffle(pool)
    unknown = []
    for item in pool:
        known = video_orientation(item, allow_probe=False)
        if known == orientation:
            return item, True
        if known is None:
            unknown.append(item)
    for item in unknown[:ORIENTATION_PROBE_BUDGET]:
        if video_orientation(item, allow_probe=True) == orientation:
            return item, True
    return None, False


def get_random_video(exclude_path=None, month=None, q=None, day=None, orientation=None):
    """Server-side random pick for All/month/day/search without shipping the full list."""
    if STORAGE_MODE == 'drive':
        drive = get_drive_storage()
        result = drive._videos_from_memory(
            month=month or None,
            q=q or None,
            offset=0,
            limit=10**9,
            day=day or None,
        )
        items = result.get('loaded') or []
        total = result.get('total', len(items))
        indexing = bool(result.get('indexing'))
        error = result.get('error')
    else:
        videos = get_videos_cached()
        items = filter_media_items(videos, month=month or None, q=q or None, kind='videos', day=day or None)
        total = len(items)
        indexing = False
        error = None
    orientation = normalize_orientation(orientation)
    if orientation:
        picked, matched = pick_random_oriented_video(items, orientation, exclude_path=exclude_path)
        if not picked and items and not indexing:
            error = error or f'No {orientation} videos in this view'
    else:
        picked, matched = pick_random_item(items, exclude_path=exclude_path), False
    return {
        'video': picked,
        'total': total,
        'indexing': indexing,
        'error': error,
        'orientation': orientation,
        'orientationMatched': matched,
        'ffmpeg': bool(_ffmpeg_path),
        'vthumb': vthumb_available(),
    }


def get_random_photo(exclude_path=None, month=None, q=None, day=None):
    if STORAGE_MODE == 'drive':
        drive = get_drive_storage()
        result = drive._photos_from_memory(
            month=month or None,
            q=q or None,
            offset=0,
            limit=10**9,
            day=day or None,
        )
        items = result.get('loaded') or []
        total = result.get('total', len(items))
        indexing = bool(result.get('indexing'))
        error = result.get('error')
    else:
        photos = get_photos_cached()
        items = filter_media_items(photos, month=month or None, q=q or None, kind='photos', day=day or None)
        total = len(items)
        indexing = False
        error = None
    picked = pick_random_item(items, exclude_path=exclude_path)
    return {
        'photo': picked,
        'total': total,
        'indexing': indexing,
        'error': error,
    }


def photo_month_catalog(month, force=False):
    """Every still in a month, plus per-day counts. One payload for the grid."""
    photos = get_photos_cached(force=force)
    items = filter_media_items(photos, month=month, kind='photos')
    return {
        'total': len(items),
        'month': month,
        'photos': items,
        'days': media_day_summary(items, month),
        'indexing': _refresh_in_progress('photos'),
        'hasMore': False,
        'error': None,
        'catalog': True,
    }


def paginate_media(items, month=None, q=None, offset=0, limit=40, kind='videos', day=None):
    filtered = filter_media_items(items, month=month, q=q, kind=kind, day=day)
    total = len(filtered)
    page = filtered[offset:offset + limit]
    return total, page


def parse_media_api_query(query):
    force = query.get('refresh', [''])[0].lower() in ('1', 'true', 'yes')
    summary = query.get('summary', [''])[0].lower() in ('1', 'true', 'yes')
    month = query.get('month', [''])[0]
    day = query.get('day', [''])[0]
    q = query.get('q', [''])[0]
    try:
        offset = max(0, int(query.get('offset', ['0'])[0] or 0))
    except ValueError:
        offset = 0
    try:
        limit = min(5000, max(1, int(query.get('limit', ['40'])[0] or 40)))
    except ValueError:
        limit = 40
    return force, summary, month, q, offset, limit, day


_RANGE_RE = re.compile(r'bytes=(\d*)-(\d*)')


def parse_range_header(range_header, file_size):
    """Parse Range header; return (start, end), None, or 'unsatisfiable'."""
    if not range_header:
        return None
    m = _RANGE_RE.match(range_header.strip())
    if not m:
        return None
    start_s, end_s = m.groups()
    if not start_s and not end_s:
        return None
    if not start_s:
        suffix = int(end_s)
        start = max(0, file_size - suffix)
        end = file_size - 1
    else:
        start = int(start_s)
        end = int(end_s) if end_s else file_size - 1
    end = min(end, file_size - 1)
    if start >= file_size or start > end:
        return 'unsatisfiable'
    return start, end


def serve_ranged_file(handler, filepath):
    """Serve a file with HTTP Range support (206) for video streaming."""
    global _active_streams
    filepath = resolve_stream_path(Path(filepath))
    if not filepath.is_file():
        handler.send_error(404)
        return

    with _active_streams_lock:
        _active_streams += 1
    try:
        _serve_ranged_file_body(handler, filepath)
    finally:
        with _active_streams_lock:
            _active_streams -= 1


def _serve_ranged_file_body(handler, filepath):
    try:
        file_size = filepath.stat().st_size
    except OSError:
        handler.send_error(404)
        return
    content_type = mimetypes.guess_type(str(filepath))[0] or 'application/octet-stream'
    parsed = parse_range_header(handler.headers.get('Range'), file_size)

    if parsed == 'unsatisfiable':
        send_http_empty(handler, 416, extra_headers={'Content-Range': f'bytes */{file_size}'})
        return

    try:
        src_file = open(filepath, 'rb')
    except OSError:
        handler.send_error(503, 'File temporarily unavailable')
        return

    with src_file as f:
        if parsed:
            start, end = parsed
            length = end - start + 1
            handler.send_response(206)
            handler.send_header('Content-Type', content_type)
            handler.send_header('Content-Length', str(length))
            handler.send_header('Content-Range', f'bytes {start}-{end}/{file_size}')
            handler.send_header('Accept-Ranges', 'bytes')
            handler.send_header('Cache-Control', 'public, max-age=3600')
            handler.end_headers()
            f.seek(start)
            remaining = length
            while remaining > 0:
                chunk = f.read(min(256 * 1024, remaining))
                if not chunk or not safe_write(handler.wfile, chunk):
                    break
                remaining -= len(chunk)
            return

        handler.send_response(200)
        handler.send_header('Content-Type', content_type)
        handler.send_header('Content-Length', str(file_size))
        handler.send_header('Accept-Ranges', 'bytes')
        handler.send_header('Cache-Control', 'public, max-age=3600')
        handler.end_headers()
        while chunk := f.read(256 * 1024):
            if not safe_write(handler.wfile, chunk):
                break


def serve_drive_media(handler, rel_path):
    """Proxy a Drive file on demand with Range support — no full download."""
    global _active_streams
    drive = get_drive_storage()
    meta = drive.get_meta(rel_path)
    if not meta:
        handler.send_error(404)
        return
    file_size = int(meta.get('size') or 0)
    content_type = (
        meta.get('mime')
        or mimetypes.guess_type(meta['name'])[0]
        or 'application/octet-stream'
    )
    range_header = handler.headers.get('Range')
    parsed = parse_range_header(range_header, file_size) if file_size else None
    if parsed == 'unsatisfiable':
        send_http_empty(handler, 416, extra_headers={'Content-Range': f'bytes */{file_size}'})
        return

    cached = get_drive_prefix_cache(meta['id'], file_size) if file_size else None
    if parsed and cached:
        prefix_path, prefix_len = cached
        start, end = parsed
        if end < prefix_len:
            _serve_drive_prefix_range(
                handler, prefix_path, file_size, content_type, start, end
            )
            return

    # Warm / top-up the head so later opens can start from disk.
    if file_size:
        schedule_drive_prefix_warm(meta['id'], file_size)

    outgoing_range = None
    if parsed:
        start, end = parsed
        outgoing_range = f'bytes={start}-{end}'
    elif range_header:
        outgoing_range = range_header

    with _active_streams_lock:
        _active_streams += 1
    resp = None
    try:
        resp = drive.open_media(meta['id'], outgoing_range)
        if resp.status_code not in (200, 206):
            handler.send_error(502, f'Drive returned {resp.status_code}')
            return
        handler.send_response(resp.status_code)
        handler.send_header('Content-Type', content_type)
        content_length = resp.headers.get('Content-Length')
        if content_length:
            handler.send_header('Content-Length', content_length)
        content_range = resp.headers.get('Content-Range')
        if content_range:
            handler.send_header('Content-Range', content_range)
        elif parsed:
            start, end = parsed
            handler.send_header('Content-Range', f'bytes {start}-{end}/{file_size}')
        handler.send_header('Accept-Ranges', 'bytes')
        handler.send_header('Cache-Control', 'public, max-age=3600')
        handler.send_header('X-Cache', 'MISS')
        handler.end_headers()

        # Capture the start of the file while streaming so later opens hit disk.
        capture = bytearray()
        should_capture = (
            file_size > 0
            and not cached
            and (parsed is None or (isinstance(parsed, tuple) and parsed[0] == 0))
        )
        capture_limit = min(file_size, DRIVE_PREFIX_BYTES) if should_capture else 0

        for chunk in resp.iter_content(256 * 1024):
            if not chunk:
                break
            if capture_limit and len(capture) < capture_limit:
                need = capture_limit - len(capture)
                capture.extend(chunk[:need])
            if not safe_write(handler.wfile, chunk):
                break
        if capture_limit and len(capture) >= min(file_size, 64 * 1024):
            save_drive_prefix_cache(meta['id'], file_size, bytes(capture))
    except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
        pass
    except Exception:
        try:
            handler.send_error(502, 'Drive stream failed')
        except Exception:
            pass
    finally:
        if resp is not None:
            resp.close()
        with _active_streams_lock:
            _active_streams -= 1


def serve_drive_thumb(handler, rel_path):
    """Serve Drive's generated thumbnail; never downloads the source file."""
    with _drive_thumb_semaphore:
        found = get_drive_storage().fetch_thumbnail(rel_path, size=DRIVE_GRID_THUMB_SIZE)
    if not found:
        send_http_empty(handler, 404)
        return
    data, mime = found
    handler.send_response(200)
    handler.send_header('Content-Type', mime)
    handler.send_header('Content-Length', str(len(data)))
    handler.send_header('Cache-Control', THUMB_CACHE_HEADER)
    handler.end_headers()
    safe_write(handler.wfile, data)


def serve_media(handler, rel_path):
    if STORAGE_MODE == 'drive':
        serve_drive_media(handler, rel_path)
        return
    filepath = resolve_media_path(rel_path)
    if filepath:
        suffix = Path(str(filepath)).suffix.lower()
        if suffix in IMAGE_EXTENSIONS:
            local = _src_cache_path(filepath)
            try:
                if local.is_file() and local.stat().st_size > 32:
                    serve_ranged_file(handler, local)
                    return
            except OSError:
                pass
        serve_ranged_file(handler, filepath)
        return
    handler.send_error(404)


def respond_json(handler, obj):
    send_http_json(handler, 200, obj)


def safe_write(wfile, data):
    """Write to client; ignore disconnects (common on mobile when scrolling away)."""
    try:
        wfile.write(data)
        return True
    except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
        return False


class VideoHandler(SimpleHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    # Small responses (thumbnails, JSON) otherwise stall on Nagle + delayed ACK.
    disable_nagle_algorithm = True

    def copyfile(self, source, outputfile):
        """Stream files without crashing when the client closes early."""
        try:
            shutil.copyfileobj(source, outputfile, length=64 * 1024)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError) as e:
            if getattr(e, 'errno', None) not in (32, 54, 104, None):
                raise

    def handle_one_request(self):
        try:
            super().handle_one_request()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    def log_message(self, format, *args):
        # Skip noisy logs for expected client disconnects during streaming
        try:
            if args and isinstance(args[-1], int) and args[-1] in (32, 54, 104):
                return
        except (IndexError, TypeError):
            pass
        super().log_message(format, *args)
    def end_headers(self):
        # CORS for all requests
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, HEAD, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Range')
        self.send_header('Access-Control-Expose-Headers', 'Content-Length, Content-Range, Accept-Ranges')
        super().end_headers()

    def do_HEAD(self):
        bare = unquote(self.path).split('?', 1)[0]
        if bare == '/healthz':
            send_http_bytes(self, 200, HEALTHZ_BODY, 'text/plain')
            return
        if SITE_PASSWORD and not is_authed(self) and bare not in ('/login',):
            send_http_bytes(self, 401, UNAUTHORIZED_JSON, 'application/json')
            return
        if serve_app_html(self, bare):
            return
        path = unquote(self.path).split('?', 1)[0]
        rel = path.lstrip('/')
        if STORAGE_MODE == 'drive' and rel and '..' not in rel:
            suffix = Path(rel).suffix.lower()
            if suffix in VIDEO_EXTENSIONS or suffix in IMAGE_EXTENSIONS:
                meta = get_drive_storage().get_meta(rel)
                if meta:
                    ctype = (
                        meta.get('mime')
                        or mimetypes.guess_type(meta['name'])[0]
                        or 'application/octet-stream'
                    )
                    self.send_response(200)
                    self.send_header('Content-Type', ctype)
                    self.send_header('Content-Length', str(int(meta.get('size') or 0)))
                    self.send_header('Accept-Ranges', 'bytes')
                    self.end_headers()
                    return
        super().do_HEAD()

    def do_OPTIONS(self):
        send_http_empty(self, 204)

    def do_POST(self):
        bare = unquote(self.path).split('?', 1)[0]
        if bare != '/login':
            send_http_empty(self, 404)
            return
        length = int(self.headers.get('Content-Length') or 0)
        body = self.rfile.read(length) if length else b''
        params = parse_qs(body.decode('utf-8', errors='replace'))
        password = (params.get('password') or [''])[0]
        if SITE_PASSWORD and password == SITE_PASSWORD:
            send_http_empty(self, 303, extra_headers={
                'Set-Cookie': f'{AUTH_COOKIE}={auth_cookie_value()}; Path=/; HttpOnly; SameSite=Lax',
                'Location': '/index.html',
            })
            return
        send_login_page(self, status=401)

    def do_DELETE(self):
        if SITE_PASSWORD and not is_authed(self):
            send_http_bytes(self, 401, UNAUTHORIZED_JSON, 'application/json')
            return
        if STORAGE_MODE == 'drive':
            send_http_json(self, 403, {'error': 'Delete disabled in cloud/Drive mode'})
            return
        path = unquote(self.path).lstrip('/')
        if not path or '..' in path.split('/'):
            send_http_empty(self, 400)
            return
        try:
            deleted = delete_local_media(path)
        except OSError as exc:
            send_http_json(self, 500, {'error': str(exc)})
            return
        if deleted:
            send_http_json(self, 200, {'deleted': path})
        else:
            send_http_empty(self, 404)

    def do_GET(self):
        path = unquote(self.path)
        bare_path = path.split('?', 1)[0]

        if bare_path == '/healthz':
            send_http_bytes(self, 200, HEALTHZ_BODY, 'text/plain')
            return

        if bare_path == '/login':
            send_login_page(self)
            return

        if SITE_PASSWORD and not is_authed(self):
            if bare_path in APP_HTML_PAGES:
                send_login_page(self)
            else:
                send_http_bytes(self, 401, UNAUTHORIZED_JSON, 'application/json')
            return

        # HTML apps — always from the app folder (never MEDIA_ROOT).
        if serve_app_html(self, bare_path):
            return

        path = bare_path if bare_path != path else path

        # Warm Drive prefix cache for the next video (no body returned to the client).
        if path.split('?', 1)[0] == '/api/prefetch':
            query = parse_qs(urlparse(self.path).query)
            rel = unquote((query.get('path') or [''])[0]).lstrip('/')
            if not rel or '..' in rel:
                send_http_json(self, 400, {'ok': False, 'error': 'path required'})
                return
            if STORAGE_MODE != 'drive':
                # Local files are already on disk; nothing to warm.
                respond_json(self, {'ok': True, 'cached': True, 'mode': 'local'})
                return
            drive = get_drive_storage()
            meta = drive.get_meta(rel)
            if not meta:
                send_http_json(self, 404, {'ok': False, 'error': 'not found'})
                return
            file_size = int(meta.get('size') or 0)
            cached = get_drive_prefix_cache(meta['id'], file_size)
            if cached:
                respond_json(self, {
                    'ok': True,
                    'cached': True,
                    'bytes': cached[1],
                    'fileSize': file_size,
                })
                return
            ok = warm_drive_prefix_cache(meta['id'], file_size)
            cached = get_drive_prefix_cache(meta['id'], file_size)
            respond_json(self, {
                'ok': bool(ok and cached),
                'cached': bool(cached),
                'bytes': cached[1] if cached else 0,
                'fileSize': file_size,
            })
            return

        # API endpoint: returns JSON list of videos sorted by date
        if path.split('?', 1)[0] == '/api/videos':
            query = parse_qs(urlparse(self.path).query)
            force, summary, month, q, offset, limit, day = parse_media_api_query(query)
            if STORAGE_MODE == 'drive':
                drive = get_drive_storage()
                result = drive.list_videos(
                    month=month or None,
                    q=q or None,
                    offset=offset,
                    limit=limit,
                    summary=summary,
                    refresh=force,
                    day=day or None,
                )
                error = result.get('error') or drive.last_error
                indexing = bool(result.get('indexing') or drive.videos_indexing)
                if summary:
                    loaded = result.get('loaded') or []
                    months = result.get('months')
                    if not months:
                        months = media_month_summary(loaded)
                    respond_json(self, {
                        'total': result.get('total', len(loaded)),
                        'months': months,
                        'days': result.get('days') if month else None,
                        'ffmpeg': bool(_ffmpeg_path),
                        'vthumb': vthumb_available(),
                        'indexing': indexing,
                        'hasMore': bool(result.get('hasMore')),
                        'error': error,
                    })
                    return
                respond_json(self, {
                    'total': result.get('total', 0),
                    'offset': offset,
                    'limit': limit,
                    'videos': result.get('videos') or [],
                    'ffmpeg': bool(_ffmpeg_path),
                    'vthumb': vthumb_available(),
                    'indexing': indexing,
                    'hasMore': bool(result.get('hasMore')),
                    'error': error,
                })
                return
            videos = get_videos_cached(force=force)
            indexing = _refresh_in_progress('videos')
            if summary:
                respond_json(self, {
                    'total': len(videos),
                    'months': indexed_month_summary('videos', videos),
                    'days': indexed_day_summary('videos', month, videos) if month else None,
                    'ffmpeg': bool(_ffmpeg_path),
                    'vthumb': vthumb_available(),
                    'indexing': indexing,
                    'hasMore': False,
                    'error': None,
                })
                return
            total, page = paginate_media(videos, month=month or None, q=q or None, offset=offset, limit=limit, kind='videos', day=day or None)
            respond_json(self, {
                'total': total,
                'offset': offset,
                'limit': limit,
                'videos': page,
                'ffmpeg': bool(_ffmpeg_path),
                'vthumb': vthumb_available(),
                'indexing': indexing,
                'hasMore': (offset + limit) < total,
                'error': None,
            })
            return

        # API endpoint: returns JSON list of photos sorted by date
        if path.split('?', 1)[0] == '/api/photos':
            query = parse_qs(urlparse(self.path).query)
            force, summary, month, q, offset, limit, day = parse_media_api_query(query)
            catalog = query.get('catalog', [''])[0].lower() in ('1', 'true', 'yes')
            if STORAGE_MODE == 'drive':
                drive = get_drive_storage()
                result = drive.list_photos(
                    month=month or None,
                    q=q or None,
                    offset=offset,
                    limit=limit,
                    summary=summary,
                    refresh=force,
                    day=day or None,
                )
                error = result.get('error') or drive.last_error
                indexing = bool(result.get('indexing') or drive.photos_indexing)
                if summary:
                    loaded = result.get('loaded') or []
                    months = result.get('months')
                    if not months:
                        months = media_month_summary(loaded)
                    respond_json(self, {
                        'total': result.get('total', len(loaded)),
                        'months': months,
                        'days': result.get('days') if month else None,
                        'indexing': indexing,
                        'hasMore': bool(result.get('hasMore')),
                        'error': error,
                    })
                    return
                respond_json(self, {
                    'total': result.get('total', 0),
                    'offset': offset,
                    'limit': limit,
                    'photos': result.get('photos') or [],
                    'indexing': indexing,
                    'hasMore': bool(result.get('hasMore')),
                    'error': error,
                })
                return
            photos = get_photos_cached(force=force)
            indexing = _refresh_in_progress('photos')
            if catalog and month and not q:
                payload = photo_month_catalog(month, force=force)
                payload['indexing'] = indexing
                respond_json(self, payload)
                return
            if summary:
                if month:
                    set_photo_focus(month, None)
                respond_json(self, {
                    'total': len(photos),
                    'months': indexed_month_summary('photos', photos),
                    'days': indexed_day_summary('photos', month, photos) if month else None,
                    'indexing': indexing,
                    'hasMore': False,
                    'error': None,
                })
                return
            total, page = paginate_media(photos, month=month or None, q=q or None, offset=offset, limit=limit, kind='photos', day=day or None)
            _warm_photo_listing(page, offset, limit, month=month, q=q, day=day)
            respond_json(self, {
                'total': total,
                'offset': offset,
                'limit': limit,
                'photos': page,
                'indexing': indexing,
                'hasMore': (offset + limit) < total,
                'error': None,
            })
            return

        # Tell the prewarm workers which tiles are on screen right now.
        if path.split('?', 1)[0] == '/api/warm':
            if STORAGE_MODE == 'drive' or not PREWARM_ENABLED:
                respond_json(self, {'queued': 0, 'prewarm': False})
                return
            query = parse_qs(urlparse(self.path).query)
            raw = query.get('paths', [''])[0]
            reset = query.get('reset', [''])[0].lower() in ('1', 'true', 'yes')
            paths = [p for p in (raw.split('\n') if raw else []) if p.strip()][:200]
            queued = queue_warm_paths([p.strip() for p in paths], reset=reset)
            respond_json(self, {'queued': queued, 'prewarm': True})
            return

        if path.split('?', 1)[0] == '/api/thumbs':
            photos = photo_thumb_progress()
            respond_json(self, {'photos': photos})
            return

        # One random video from the library (All / optional month / search)
        if path.split('?', 1)[0] == '/api/random/video':
            query = parse_qs(urlparse(self.path).query)
            exclude = query.get('exclude', [''])[0] or None
            month = query.get('month', [''])[0] or None
            day = query.get('day', [''])[0] or None
            q = query.get('q', [''])[0] or None
            orientation = query.get('orientation', [''])[0] or None
            result = get_random_video(
                exclude_path=exclude, month=month, q=q, day=day, orientation=orientation,
            )
            if not result.get('video'):
                result = {
                    'video': None,
                    'total': result.get('total', 0),
                    'indexing': result.get('indexing', False),
                    'error': result.get('error') or 'No videos available',
                    'orientation': result.get('orientation'),
                    'orientationMatched': False,
                    'ffmpeg': result.get('ffmpeg'),
                    'vthumb': result.get('vthumb'),
                }
            respond_json(self, result)
            return

        if path.split('?', 1)[0] == '/api/random/photo':
            query = parse_qs(urlparse(self.path).query)
            exclude = query.get('exclude', [''])[0] or None
            month = query.get('month', [''])[0] or None
            day = query.get('day', [''])[0] or None
            q = query.get('q', [''])[0] or None
            result = get_random_photo(exclude_path=exclude, month=month, q=q, day=day)
            if not result.get('photo'):
                result = {
                    'photo': None,
                    'total': result.get('total', 0),
                    'indexing': result.get('indexing', False),
                    'error': result.get('error') or 'No photos available',
                }
            respond_json(self, result)
            return

        if path.split('?', 1)[0] == '/api/workflow':
            query = parse_qs(urlparse(self.path).query)
            rel = unquote((query.get('path') or [''])[0]).lstrip('/')
            if not rel or '..' in rel.split('/'):
                send_http_json(self, 400, {'error': 'path required'})
                return
            if STORAGE_MODE == 'drive':
                try:
                    payload = extract_drive_workflow(get_drive_storage(), rel)
                except Exception as exc:
                    send_http_json(self, 502, {
                        'error': f'Could not read workflow from Drive: {exc}',
                        'prompt': None,
                        'fields': [],
                        'source': None,
                        'path': rel,
                    })
                    return
                if payload is None:
                    send_http_empty(self, 404)
                    return
                respond_json(self, payload)
                return
            filepath = resolve_media_path(rel)
            if not filepath or not filepath.is_file():
                send_http_empty(self, 404)
                return
            respond_json(self, extract_comfy_workflow(filepath))
            return

        # API endpoint: returns metadata from PNG/WebP (ComfyUI prompt, workflow, etc.)
        if path.startswith('/api/metadata/'):
            rel = unquote(path[14:])
            if STORAGE_MODE == 'drive':
                meta = get_drive_storage().get_meta(rel)
                if not meta:
                    send_http_empty(self, 404)
                    return
                respond_json(self, {
                    'file': rel,
                    'size': meta.get('size', 0),
                    'modified': meta.get('modified', 0),
                    'format': Path(meta['name']).suffix.lstrip('.').upper() or None,
                    'note': 'Gateway mode returns Drive metadata only (file is not downloaded).',
                })
                return
            filepath = resolve_media_path(rel)
            if filepath and filepath.is_file():
                meta = extract_image_metadata(filepath)
                data = json.dumps(meta).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            else:
                send_http_empty(self, 404)
            return

        # Serve files by absolute path (for photos outside CWD)
        if path.startswith('/file/'):
            rel = unquote(path[6:])
            if media_exists(rel):
                serve_media(self, rel)
                return
            send_http_empty(self, 404)
            return

        # Thumbnail endpoint: /thumb/path/to/image.jpg
        if path.startswith('/thumb/'):
            rel = unquote(path[7:])
            if STORAGE_MODE == 'drive':
                # fetch_thumbnail looks up meta itself — avoid a second Drive round-trip.
                serve_drive_thumb(self, rel)
                return
            query = parse_qs(urlparse(self.path).query)
            src_mtime = _mtime_from_query(query)
            found = lookup_photo_thumb(rel, src_mtime)
            if found:
                data, mime = found
                self.send_response(200)
                self.send_header('Content-Type', mime)
                self.send_header('Content-Length', str(len(data)))
                self.send_header('Cache-Control', THUMB_CACHE_HEADER)
                self.end_headers()
                safe_write(self.wfile, data)
                return
            send_http_empty(self, 503, extra_headers={
                'Retry-After': '1',
                'Cache-Control': 'no-store',
            })
            return

        # Video thumbnail endpoint: /vthumb/path/to/video.mp4
        if path.startswith('/vthumb/'):
            rel = unquote(path[8:])
            if STORAGE_MODE == 'drive':
                serve_drive_thumb(self, rel)
                return
            query = parse_qs(urlparse(self.path).query)
            src_mtime = _mtime_from_query(query)
            cache_key = _cache_key_from_rel(rel, ':v400webp')
            cached = get_disk_thumb(cache_key, src_mtime)
            if cached:
                data, mime = cached
                self.send_response(200)
                self.send_header('Content-Type', mime)
                self.send_header('Content-Length', str(len(data)))
                self.send_header('Cache-Control', THUMB_CACHE_HEADER)
                self.end_headers()
                safe_write(self.wfile, data)
                return
            filepath = resolve_media_path(rel)
            if filepath and filepath.is_file():
                cached = get_cached_video_thumbnail(filepath)
                if cached:
                    data, mime = cached
                else:
                    with _active_streams_lock:
                        streams_busy = _active_streams > 0
                    if streams_busy:
                        send_http_json(
                            self,
                            503,
                            {
                                'error': 'Thumbnail deferred — video streaming in progress',
                                'retry': True,
                            },
                            extra_headers={'Retry-After': '5'},
                        )
                        return
                    thumb = generate_video_thumbnail(filepath)
                    if not thumb:
                        send_http_json(self, 503, {
                            'error': 'Could not generate video thumbnail',
                            'ffmpeg': bool(_ffmpeg_path),
                            'vthumb': vthumb_available(),
                        })
                        return
                    data, mime = thumb
                self.send_response(200)
                self.send_header('Content-Type', mime)
                self.send_header('Content-Length', str(len(data)))
                self.send_header('Cache-Control', THUMB_CACHE_HEADER)
                self.end_headers()
                safe_write(self.wfile, data)
                return
            else:
                send_http_empty(self, 404)
                return

        # Stream video/images with byte-range support (required for mobile playback)
        rel_path = bare_path.lstrip('/')
        if rel_path and '..' not in rel_path:
            suffix = Path(rel_path).suffix.lower()
            if suffix in VIDEO_EXTENSIONS or suffix in IMAGE_EXTENSIONS:
                if media_exists(rel_path):
                    serve_media(self, rel_path)
                    return
                if STORAGE_MODE != 'drive':
                    tried = Path(rel_path)
                    print(
                        f'   404 local media: {tried} '
                        f'(abs={tried.resolve() if tried.exists() else Path.cwd() / tried})'
                    )

        # Serve other static files (HTML, images) normally
        try:
            super().do_GET()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass


if __name__ == '__main__':
    if len(sys.argv) > 1:
        PORT = int(sys.argv[1])
    script_dir = os.path.dirname(os.path.abspath(__file__))
    if STORAGE_MODE == 'drive':
        os.chdir(script_dir)
        media_root = script_dir
        if not os.environ.get('DRIVE_ROOT_FOLDER_ID'):
            print('❌ DRIVE_ROOT_FOLDER_ID is required when STORAGE_MODE=drive')
            sys.exit(1)
        if not os.environ.get('GOOGLE_SERVICE_ACCOUNT_JSON'):
            print('❌ GOOGLE_SERVICE_ACCOUNT_JSON is required when STORAGE_MODE=drive')
            sys.exit(1)
    else:
        media_root = resolve_media_root()
        if not media_root.is_dir():
            print(f'❌ MEDIA_ROOT does not exist: {media_root}')
            print('   Set MEDIA_ROOT to your Google Drive "My Drive" folder in config.env or start.sh')
            sys.exit(1)
        os.chdir(media_root)
    import socket
    lan_ip = 'localhost'
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('8.8.8.8', 80))
        lan_ip = s.getsockname()[0]
        s.close()
    except OSError:
        pass
    try:
        httpd = ThreadedHTTPServer(('0.0.0.0', PORT), VideoHandler)
    except OSError as exc:
        if getattr(exc, 'errno', None) != 48:
            raise
        print(f'❌ Port {PORT} is already in use — another video_server.py is probably running.')
        print(f'   See what has it:  lsof -nP -iTCP:{PORT} -sTCP:LISTEN')
        print(f'   Stop it:          pkill -f video_server.py')
        print(f'   Or use a another port:  python3 video_server.py {PORT + 1}')
        sys.exit(1)

    print(f'🎬 Video server running at http://localhost:{PORT}')
    print(f'   Phone/tablet: http://{lan_ip}:{PORT}/index.html')
    print(f'   Storage: {STORAGE_MODE}')
    print(f'   App files: {script_dir}')
    if STORAGE_MODE == 'drive':
        print(f'   Drive folder: {os.environ.get("DRIVE_ROOT_FOLDER_ID")}')
        print(f'   Videos: {VIDEO_DIR}')
        print(f'   Photos: {PHOTO_DIRS}')
        print('   Gateway: one-time Drive scan saved to disk; Refresh checks current month only')

        def _boot_drive_index():
            try:
                get_drive_storage().ensure_full_index()
            except Exception as exc:
                print(f'   Drive index bootstrap failed: {exc}')

        threading.Thread(target=_boot_drive_index, daemon=True).start()
    else:
        print(f'   MEDIA_ROOT: {os.getcwd()}')
        print(f'   Videos: {os.path.abspath(VIDEO_DIR)}')
        print(f'   Photos: {[os.path.abspath(d) if not Path(d).is_absolute() else d for d in PHOTO_DIRS]}')
        _warn_local_media_root(script_dir, media_root)
        cwd_index = Path(os.getcwd()) / 'index.html'
        app_index = Path(script_dir) / 'index.html'
        if cwd_index.is_file() and cwd_index.resolve() != app_index.resolve():
            print('   ⚠️  Found index.html inside MEDIA_ROOT — it is ignored; app HTML comes from App files.')
            print(f'      Ignored: {cwd_index}')
            print(f'      Serving: {app_index}')
    if _ffmpeg_path:
        codec = 'webp' if _ffmpeg_has_webp else 'jpeg (no libwebp in this ffmpeg)'
        print(f'   Video thumbnails: ffmpeg -> {codec}')
    elif _qlmanage_path:
        print(f'   Video thumbnails: Quick Look ({_qlmanage_path})')
    else:
        print('   Video thumbnails: unavailable (install ffmpeg: brew install ffmpeg)')
    print(f'   Cache (local disk): {CACHE_ROOT}')
    if STORAGE_MODE == 'drive':
        print(
            f'   Drive prefix cache: {DRIVE_PREFIX_BYTES // (1024 * 1024)}MB/file, '
            f'max {DRIVE_PREFIX_CACHE_MAX_BYTES // (1024 * 1024)}MB'
        )
    if STORAGE_MODE != 'drive':
        def _load_index_background():
            print('   Loading video index...')
            t0 = time.time()
            try:
                video_count = len(get_videos_cached())
                print(f'   Ready: {video_count} videos ({(time.time() - t0) * 1000:.0f} ms)')
                if video_count == 0:
                    print('   ⚠️  0 videos found. Open http://localhost:%s/ and check:' % PORT)
                    print(f'      • MEDIA_ROOT should be your Google Drive "My Drive" (run ./start.sh)')
                    print(f'      • Videos expected under: {os.path.abspath(VIDEO_DIR)}')
                    print('      • Hard-refresh the browser (Cmd+Shift+R)')
                else:
                    print('   If the grid shows solid red/pink squares, clear bad thumbs and restart:')
                    print(f'      rm -rf "{CACHE_ROOT / "thumbs"}"')
                    print('      MEDIA_PREWARM=0 ./start.sh')
            except Exception as exc:
                print(f'   Video index failed: {exc}')

        threading.Thread(target=_load_index_background, daemon=True).start()
        threading.Thread(target=_faststart_worker, daemon=True).start()
        if PREWARM_ENABLED:
            prewarm_workers = 2
            for worker_id in range(prewarm_workers):
                threading.Thread(
                    target=_prewarm_worker, args=(worker_id, prewarm_workers), daemon=True
                ).start()
            photo_workers = 4
            for worker_id in range(photo_workers):
                threading.Thread(
                    target=_prewarm_photos_worker, args=(worker_id, photo_workers), daemon=True
                ).start()
            warm_workers = 8
            for _ in range(warm_workers):
                threading.Thread(target=_warm_queue_worker, daemon=True).start()
            print(
                f'   Thumbnail prewarm: on, {prewarm_workers} video + {photo_workers} photo '
                f'+ {warm_workers} visible-queue workers '
                f'(MEDIA_PREWARM=0 to disable)'
            )
    print(f'   Press Ctrl+C to stop')
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print('\n   Stopped.')
