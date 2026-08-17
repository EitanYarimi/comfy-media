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
import threading
from collections import OrderedDict, deque
from datetime import datetime
from http.server import HTTPServer, SimpleHTTPRequestHandler
from socketserver import ThreadingMixIn
from pathlib import Path
from urllib.parse import unquote, parse_qs, urlparse

STORAGE_MODE = os.environ.get('STORAGE_MODE', 'local').lower()
PORT = int(os.environ.get('PORT', sys.argv[1] if len(sys.argv) > 1 else 8080))

# Media paths are relative to MEDIA_ROOT (see __main__)
VIDEO_DIR = os.environ.get('VIDEO_DIR', 'ComfyUI/output/video')
PHOTO_DIRS = [
    p.strip()
    for p in os.environ.get(
        'PHOTO_DIRS', 'ComfyUI/output,stable-diffusion-webui/outputs'
    ).split(',')
    if p.strip()
]

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
    return script_dir


CACHE_ROOT = _default_cache_root()
THUMB_CACHE_DIR = CACHE_ROOT / 'thumbs'
STREAM_CACHE_DIR = CACHE_ROOT / 'streams'

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

_photo_cache = {'data': None, 'time': 0.0}
_video_cache = {'data': None, 'time': 0.0}
_media_by_month = {'videos': None, 'photos': None}
_index_refresh_lock = threading.Lock()

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


def resolve_media_path(rel_path):
    """Return a local Path for serving (local mode only)."""
    rel_path = str(rel_path).replace('\\', '/').lstrip('/')
    filepath = Path(rel_path)
    return filepath if filepath.is_file() else None


def media_exists(rel_path):
    rel_path = str(rel_path).replace('\\', '/').lstrip('/')
    if STORAGE_MODE == 'drive':
        return get_drive_storage().exists(rel_path)
    filepath = Path(rel_path)
    return filepath.is_file()


def vthumb_available():
    if STORAGE_MODE == 'drive':
        return True
    return bool(_ffmpeg_path or _qlmanage_path)


# Limit concurrent vthumb generation so video streaming isn't starved on Google Drive.
_vthumb_semaphore = threading.Semaphore(3)
_active_streams = 0
_active_streams_lock = threading.Lock()
_faststart_jobs = set()
_faststart_queue = deque()
_faststart_lock = threading.Lock()
_moov_end_cache = {}


def _load_disk_index(path, legacy_path=None):
    for candidate in (path, legacy_path):
        if candidate is None:
            continue
        try:
            data = json.loads(candidate.read_text())
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            continue
        items = data.get('items')
        if isinstance(items, list):
            saved = float(data.get('saved', 0))
            # Legacy copies are treated as stale so they refresh in background.
            return items, data.get('months'), (saved if candidate is path else 0.0)
    return None, None, 0.0


def _save_disk_index(path, items, months):
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({'items': items, 'months': months, 'saved': time.time()}))
    except OSError:
        pass


def _build_month_index(items):
    by_month = {}
    for item in items:
        key = item_month_key(item['modified'])
        by_month.setdefault(key, []).append(item)
    return by_month


def _refresh_videos_background():
    with _index_refresh_lock:
        try:
            videos = scan_videos('.')
            months = media_month_summary(videos)
            _video_cache['data'] = videos
            _video_cache['time'] = time.time()
            _media_by_month['videos'] = _build_month_index(videos)
            _save_disk_index(VIDEO_INDEX_PATH, videos, months)
        except OSError:
            pass


def _refresh_photos_background():
    with _index_refresh_lock:
        try:
            photos = scan_photos('.')
            months = media_month_summary(photos)
            _photo_cache['data'] = photos
            _photo_cache['time'] = time.time()
            _media_by_month['photos'] = _build_month_index(photos)
            _save_disk_index(PHOTO_INDEX_PATH, photos, months)
        except OSError:
            pass


def get_photos_cached(force=False):
    """Return cached photo list; load disk index instantly, refresh in background."""
    now = time.time()
    if (
        not force
        and _photo_cache['data'] is not None
        and (now - _photo_cache['time']) < PHOTO_CACHE_TTL
    ):
        return _photo_cache['data']

    if not force and _photo_cache['data'] is None:
        items, months, saved = _load_disk_index(PHOTO_INDEX_PATH, LEGACY_PHOTO_INDEX_PATH)
        if items:
            _photo_cache['data'] = items
            _photo_cache['time'] = now
            _media_by_month['photos'] = _build_month_index(items)
            if saved and (now - saved) > PHOTO_CACHE_TTL:
                threading.Thread(target=_refresh_photos_background, daemon=True).start()
            return items

    photos = scan_photos('.')
    months = media_month_summary(photos)
    _photo_cache['data'] = photos
    _photo_cache['time'] = now
    _media_by_month['photos'] = _build_month_index(photos)
    _save_disk_index(PHOTO_INDEX_PATH, photos, months)
    return photos


def invalidate_photo_cache():
    _photo_cache['data'] = None
    _photo_cache['time'] = 0.0
    _media_by_month['photos'] = None


def get_videos_cached(force=False):
    """Return cached video list; load disk index instantly, refresh in background."""
    now = time.time()
    if (
        not force
        and _video_cache['data'] is not None
        and (now - _video_cache['time']) < VIDEO_CACHE_TTL
    ):
        return _video_cache['data']

    if not force and _video_cache['data'] is None:
        items, months, saved = _load_disk_index(VIDEO_INDEX_PATH, LEGACY_VIDEO_INDEX_PATH)
        if items:
            _video_cache['data'] = items
            _video_cache['time'] = now
            _media_by_month['videos'] = _build_month_index(items)
            if saved and (now - saved) > VIDEO_CACHE_TTL:
                threading.Thread(target=_refresh_videos_background, daemon=True).start()
            return items

    videos = scan_videos('.')
    months = media_month_summary(videos)
    _video_cache['data'] = videos
    _video_cache['time'] = now
    _media_by_month['videos'] = _build_month_index(videos)
    _save_disk_index(VIDEO_INDEX_PATH, videos, months)
    return videos


def invalidate_video_cache():
    _video_cache['data'] = None
    _video_cache['time'] = 0.0
    _media_by_month['videos'] = None


def invalidate_media_cache():
    invalidate_photo_cache()
    invalidate_video_cache()


def _video_cache_key(filepath):
    return hashlib.md5((str(filepath.resolve()) + ':v400webp').encode()).hexdigest()


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


def get_cached_video_thumbnail(filepath):
    """Return cached thumbnail bytes from memory, local cache, or legacy cache."""
    cache_key = _video_cache_key(filepath)
    cached = _thumb_memory_get(cache_key)
    if cached is not None:
        return cached

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
                    _thumb_memory_put(cache_key, data, mime)
                    return data, mime
            except OSError:
                continue
    return None


def get_video_duration(filepath):
    """Return video duration in seconds, using sidecar cache when available."""
    cache_key = _video_cache_key(filepath)
    meta_path = THUMB_CACHE_DIR / (cache_key + '.meta.json')
    for candidate in (meta_path, LEGACY_THUMB_CACHE_DIR / (cache_key + '.meta.json')):
        try:
            meta = json.loads(candidate.read_text())
        except (json.JSONDecodeError, OSError):
            continue
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
            THUMB_CACHE_DIR.mkdir(parents=True, exist_ok=True)
            meta_path.write_text(json.dumps({'duration': duration}))
            return duration
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    return None


def _store_thumb(cache_key, ext, data, mime):
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

    # Fixed seek avoids slow ffprobe on first thumbnail (Google Drive latency).
    scale = f'scale={VIDEO_THUMB_SIZE[0]}:{VIDEO_THUMB_SIZE[1]}:force_original_aspect_ratio=decrease'
    attempts = []
    if _ffmpeg_has_webp:
        attempts.append((['-f', 'webp', '-quality', '75'], '.webp', 'image/webp'))
    attempts.append((['-f', 'image2pipe', '-vcodec', 'mjpeg', '-q:v', '4'], '.jpg', 'image/jpeg'))

    for encode_args, ext, mime in attempts:
        try:
            result = subprocess.run(
                [
                    _ffmpeg_path, '-hide_banner', '-loglevel', 'error',
                    '-ss', '1.0', '-i', str(filepath),
                    '-an', '-sn', '-vframes', '1', '-vf', scale,
                    *encode_args, 'pipe:1',
                ],
                capture_output=True, timeout=60, check=False,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if result.returncode == 0 and result.stdout:
            return _store_thumb(cache_key, ext, result.stdout, mime)
    return None


def generate_video_thumbnail(filepath):
    """Generate or load cached video thumbnail. Returns (data, mime) or None."""
    cached = get_cached_video_thumbnail(filepath)
    if cached:
        return cached

    with _vthumb_semaphore:
        cached = get_cached_video_thumbnail(filepath)
        if cached:
            return cached
        cache_key = _video_cache_key(filepath)
        result = _thumb_from_ffmpeg(filepath, cache_key)
        if result:
            return result
        return _thumb_from_qlmanage(filepath, cache_key)


_prewarm_state = {'done': 0, 'missing': None, 'running': False}


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
            if position % worker_count != worker_id:
                continue
            # Never compete with an active playback session.
            while True:
                with _active_streams_lock:
                    busy = _active_streams > 0
                if not busy:
                    break
                time.sleep(3)

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
        time.sleep(300)


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


VIDEO_EXTENSIONS = {'.mp4', '.webm', '.ogg', '.mov', '.mkv', '.avi', '.m4v', '.3gp', '.flv', '.wmv'}
IMAGE_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp', '.svg', '.avif', '.heic'}


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


def media_month_summary(items):
    counts = {}
    for item in items:
        key = item_month_key(item['modified'])
        counts[key] = counts.get(key, 0) + 1
    return [{'month': k, 'count': counts[k]} for k in sorted(counts.keys(), reverse=True)]


def paginate_media(items, month=None, q=None, offset=0, limit=40, kind='videos'):
    filtered = items
    if month and not q:
        index = _media_by_month.get(kind)
        if index is None:
            index = _build_month_index(items)
            _media_by_month[kind] = index
        filtered = index.get(month, [])
    else:
        if month:
            filtered = [i for i in filtered if item_month_key(i['modified']) == month]
        if q:
            ql = q.lower()
            filtered = [i for i in filtered if ql in i['name'].lower()]
    total = len(filtered)
    page = filtered[offset:offset + limit]
    return total, page


def parse_media_api_query(query):
    force = query.get('refresh', [''])[0].lower() in ('1', 'true', 'yes')
    summary = query.get('summary', [''])[0].lower() in ('1', 'true', 'yes')
    month = query.get('month', [''])[0]
    q = query.get('q', [''])[0]
    try:
        offset = max(0, int(query.get('offset', ['0'])[0] or 0))
    except ValueError:
        offset = 0
    try:
        limit = min(5000, max(1, int(query.get('limit', ['40'])[0] or 40)))
    except ValueError:
        limit = 40
    return force, summary, month, q, offset, limit


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
        handler.send_response(416)
        handler.send_header('Content-Range', f'bytes */{file_size}')
        handler.end_headers()
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
        handler.send_response(416)
        handler.send_header('Content-Range', f'bytes */{file_size}')
        handler.end_headers()
        return

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
        handler.end_headers()
        for chunk in resp.iter_content(256 * 1024):
            if not chunk or not safe_write(handler.wfile, chunk):
                break
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
    found = get_drive_storage().fetch_thumbnail(rel_path, size=400)
    if not found:
        handler.send_error(404)
        return
    data, mime = found
    handler.send_response(200)
    handler.send_header('Content-Type', mime)
    handler.send_header('Content-Length', str(len(data)))
    handler.send_header('Cache-Control', 'public, max-age=86400')
    handler.end_headers()
    safe_write(handler.wfile, data)


def serve_media(handler, rel_path):
    if STORAGE_MODE == 'drive':
        serve_drive_media(handler, rel_path)
        return
    filepath = resolve_media_path(rel_path)
    if filepath:
        serve_ranged_file(handler, filepath)
        return
    handler.send_error(404)


def respond_json(handler, obj):
    data = json.dumps(obj).encode()
    handler.send_response(200)
    handler.send_header('Content-Type', 'application/json')
    handler.send_header('Content-Length', str(len(data)))
    handler.end_headers()
    safe_write(handler.wfile, data)


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
                    if meta.get('size'):
                        self.send_header('Content-Length', str(meta['size']))
                    self.send_header('Accept-Ranges', 'bytes')
                    self.end_headers()
                    return
        super().do_HEAD()

    def do_OPTIONS(self):
        self.send_response(204)
        self.end_headers()

    def do_DELETE(self):
        if STORAGE_MODE == 'drive':
            self.send_response(403)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({'error': 'Delete disabled in cloud/Drive mode'}).encode())
            return
        path = unquote(self.path).lstrip('/')
        if not path or '..' in path:
            self.send_response(400)
            self.end_headers()
            return
        filepath = Path(path)
        if filepath.exists() and filepath.is_file():
            filepath.unlink()
            invalidate_media_cache()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({'deleted': path}).encode())
        else:
            self.send_response(404)
            self.end_headers()

    def do_GET(self):
        path = unquote(self.path)
        bare_path = path.split('?', 1)[0]

        # HTML apps — always no-cache so phones pick up updates
        if bare_path in ('/', '/index.html', '/photos.html'):
            html_name = 'photos.html' if bare_path == '/photos.html' else 'index.html'
            script_dir = Path(__file__).parent
            for search_dir in [Path('.'), script_dir]:
                html_path = search_dir / html_name
                if html_path.exists():
                    data = html_path.read_bytes()
                    self.send_response(200)
                    self.send_header('Content-Type', 'text/html; charset=utf-8')
                    self.send_header('Content-Length', str(len(data)))
                    self.send_header('Cache-Control', 'no-store, no-cache, must-revalidate')
                    self.end_headers()
                    safe_write(self.wfile, data)
                    return
            if bare_path == '/':
                super().do_GET()
                return

        path = bare_path if bare_path != path else path

        # API endpoint: returns JSON list of videos sorted by date
        if path.split('?', 1)[0] == '/api/videos':
            query = parse_qs(urlparse(self.path).query)
            force, summary, month, q, offset, limit = parse_media_api_query(query)
            videos = get_videos_cached(force=force)
            if summary:
                respond_json(self, {
                    'total': len(videos),
                    'months': media_month_summary(videos),
                    'ffmpeg': bool(_ffmpeg_path),
                    'vthumb': vthumb_available(),
                })
                return
            total, page = paginate_media(videos, month=month or None, q=q or None, offset=offset, limit=limit, kind='videos')
            respond_json(self, {
                'total': total,
                'offset': offset,
                'limit': limit,
                'videos': page,
                'ffmpeg': bool(_ffmpeg_path),
                'vthumb': vthumb_available(),
            })
            return

        # API endpoint: returns JSON list of photos sorted by date
        if path.split('?', 1)[0] == '/api/photos':
            query = parse_qs(urlparse(self.path).query)
            force, summary, month, q, offset, limit = parse_media_api_query(query)
            photos = get_photos_cached(force=force)
            if summary:
                respond_json(self, {'total': len(photos), 'months': media_month_summary(photos)})
                return
            total, page = paginate_media(photos, month=month or None, q=q or None, offset=offset, limit=limit, kind='photos')
            respond_json(self, {'total': total, 'offset': offset, 'limit': limit, 'photos': page})
            return

        # API endpoint: returns metadata from PNG/WebP (ComfyUI prompt, workflow, etc.)
        if path.startswith('/api/metadata/'):
            rel = unquote(path[14:])
            if STORAGE_MODE == 'drive':
                meta = get_drive_storage().get_meta(rel)
                if not meta:
                    self.send_response(404)
                    self.end_headers()
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
                self.send_response(404)
                self.end_headers()
            return

        # Serve files by absolute path (for photos outside CWD)
        if path.startswith('/file/'):
            rel = unquote(path[6:])
            if media_exists(rel):
                serve_media(self, rel)
                return
            self.send_response(404)
            self.end_headers()
            return

        # Thumbnail endpoint: /thumb/path/to/image.jpg
        if path.startswith('/thumb/'):
            rel = unquote(path[7:])
            if STORAGE_MODE == 'drive':
                if media_exists(rel):
                    serve_drive_thumb(self, rel)
                    return
                self.send_response(404)
                self.end_headers()
                return
            filepath = resolve_media_path(rel)
            if filepath and filepath.is_file():
                try:
                    from PIL import Image

                    cache_key = hashlib.md5(
                        (str(filepath.resolve()) + ':400webp').encode()
                    ).hexdigest()

                    found = None
                    memory_hit = _thumb_memory_get(cache_key)
                    if memory_hit is not None:
                        found = memory_hit
                    else:
                        src_mtime = filepath.stat().st_mtime
                        for cache_dir in (THUMB_CACHE_DIR, LEGACY_THUMB_CACHE_DIR):
                            for ext, ext_mime in THUMB_FORMATS:
                                candidate = cache_dir / (cache_key + ext)
                                try:
                                    if candidate.stat().st_mtime >= src_mtime:
                                        found = (candidate.read_bytes(), ext_mime)
                                        _thumb_memory_put(cache_key, *found)
                                        break
                                except OSError:
                                    continue
                            if found:
                                break

                    if found:
                        data, mime = found
                    else:
                        img = Image.open(filepath)
                        img.thumbnail(PHOTO_THUMB_SIZE, Image.LANCZOS)
                        buf = io.BytesIO()
                        try:
                            img.save(buf, format='WEBP', quality=75)
                            mime, ext = 'image/webp', '.webp'
                        except Exception:
                            if img.mode in ('RGBA', 'P'):
                                img = img.convert('RGB')
                            buf = io.BytesIO()
                            img.save(buf, format='JPEG', quality=70)
                            mime, ext = 'image/jpeg', '.jpg'
                        data, mime = _store_thumb(cache_key, ext, buf.getvalue(), mime)

                    self.send_response(200)
                    self.send_header('Content-Type', mime)
                    self.send_header('Content-Length', str(len(data)))
                    self.send_header('Cache-Control', 'public, max-age=86400')
                    self.end_headers()
                    safe_write(self.wfile, data)
                except ImportError:
                    super().do_GET()
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    pass
                except Exception:
                    self.send_response(500)
                    self.end_headers()
                return
            else:
                self.send_response(404)
                self.end_headers()
                return

        # Video thumbnail endpoint: /vthumb/path/to/video.mp4
        if path.startswith('/vthumb/'):
            rel = unquote(path[8:])
            if STORAGE_MODE == 'drive':
                if media_exists(rel):
                    serve_drive_thumb(self, rel)
                    return
                self.send_response(404)
                self.end_headers()
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
                        self.send_response(503)
                        self.send_header('Content-Type', 'application/json')
                        self.send_header('Retry-After', '5')
                        self.end_headers()
                        safe_write(self.wfile, json.dumps({
                            'error': 'Thumbnail deferred — video streaming in progress',
                            'retry': True,
                        }).encode())
                        return
                    thumb = generate_video_thumbnail(filepath)
                    if not thumb:
                        self.send_response(503)
                        self.send_header('Content-Type', 'application/json')
                        self.end_headers()
                        safe_write(self.wfile, json.dumps({
                            'error': 'Could not generate video thumbnail',
                            'ffmpeg': bool(_ffmpeg_path),
                            'vthumb': vthumb_available(),
                        }).encode())
                        return
                    data, mime = thumb
                self.send_response(200)
                self.send_header('Content-Type', mime)
                self.send_header('Content-Length', str(len(data)))
                self.send_header('Cache-Control', 'public, max-age=86400')
                self.end_headers()
                safe_write(self.wfile, data)
                return
            else:
                self.send_response(404)
                self.end_headers()
                return

        # Stream video/images with byte-range support (required for mobile playback)
        rel_path = bare_path.lstrip('/')
        if rel_path and '..' not in rel_path:
            suffix = Path(rel_path).suffix.lower()
            if suffix in VIDEO_EXTENSIONS or suffix in IMAGE_EXTENSIONS:
                if media_exists(rel_path):
                    serve_media(self, rel_path)
                    return

        # Serve other static files (HTML, images) normally
        try:
            super().do_GET()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass


if __name__ == '__main__':
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
        print('   Gateway: streams Drive bytes on demand (no full-file cache)')
    else:
        print(f'   MEDIA_ROOT: {os.getcwd()}')
        print(f'   Videos: {os.path.abspath(VIDEO_DIR)}')
        print(f'   Photos: {[os.path.abspath(d) if not Path(d).is_absolute() else d for d in PHOTO_DIRS]}')
    if _ffmpeg_path:
        codec = 'webp' if _ffmpeg_has_webp else 'jpeg (no libwebp in this ffmpeg)'
        print(f'   Video thumbnails: ffmpeg -> {codec}')
    elif _qlmanage_path:
        print(f'   Video thumbnails: Quick Look ({_qlmanage_path})')
    else:
        print('   Video thumbnails: unavailable (install ffmpeg: brew install ffmpeg)')
    print(f'   Cache (local disk): {CACHE_ROOT}')
    print('   Loading video index...')
    t0 = time.time()
    video_count = len(get_videos_cached())
    print(f'   Ready: {video_count} videos ({(time.time() - t0) * 1000:.0f} ms)')
    if STORAGE_MODE != 'drive':
        threading.Thread(target=_faststart_worker, daemon=True).start()
        if PREWARM_ENABLED:
            prewarm_workers = 2
            for worker_id in range(prewarm_workers):
                threading.Thread(
                    target=_prewarm_worker, args=(worker_id, prewarm_workers), daemon=True
                ).start()
            print(f'   Thumbnail prewarm: on, {prewarm_workers} idle workers (MEDIA_PREWARM=0 to disable)')
    print(f'   Press Ctrl+C to stop')
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print('\n   Stopped.')
