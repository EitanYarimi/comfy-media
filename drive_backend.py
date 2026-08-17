"""Google Drive storage backend for cloud deployment (Render, Railway, etc.)."""

import hashlib
import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

VIDEO_EXTENSIONS = {'.mp4', '.webm', '.ogg', '.mov', '.mkv', '.avi', '.m4v', '.3gp', '.flv', '.wmv'}
IMAGE_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp', '.svg', '.avif', '.heic'}

DRIVE_SCOPES = ['https://www.googleapis.com/auth/drive.readonly']


def _parse_drive_time(value):
    if not value:
        return 0.0
    try:
        dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return dt.timestamp()
    except (TypeError, ValueError):
        return 0.0


class DriveStorage:
    """List and cache Google Drive files using virtual paths (ComfyUI/output/...)."""

    def __init__(self, cache_root: Path, video_dir: str, photo_dirs: list[str]):
        self.cache_root = cache_root / 'drive_files'
        self.cache_root.mkdir(parents=True, exist_ok=True)
        self.index_path = cache_root / 'drive_index_v1.json'
        self.video_prefix = video_dir.replace('\\', '/').strip('/')
        self.photo_prefixes = [p.replace('\\', '/').strip('/') for p in photo_dirs]
        self.root_folder_id = os.environ['DRIVE_ROOT_FOLDER_ID']
        self.service = self._build_service()
        self._files = {}  # virtual_path -> metadata
        self._index_mtime = 0.0
        self._index_lock = threading.Lock()
        self._download_locks = {}
        self._load_index()

    def _build_service(self):
        raw = os.environ.get('GOOGLE_SERVICE_ACCOUNT_JSON', '')
        if not raw:
            raise RuntimeError('GOOGLE_SERVICE_ACCOUNT_JSON is required for STORAGE_MODE=drive')
        info = json.loads(raw)
        creds = service_account.Credentials.from_service_account_info(info, scopes=DRIVE_SCOPES)
        return build('drive', 'v3', credentials=creds, cache_discovery=False)

    def _load_index(self):
        try:
            data = json.loads(self.index_path.read_text())
            files = data.get('files', {})
            if isinstance(files, dict):
                self._files = files
                self._index_mtime = float(data.get('saved', 0))
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            self._files = {}
            self._index_mtime = 0.0

    def _save_index(self):
        try:
            self.index_path.parent.mkdir(parents=True, exist_ok=True)
            self.index_path.write_text(json.dumps({'files': self._files, 'saved': time.time()}))
            self._index_mtime = time.time()
        except OSError:
            pass

    def _list_folder(self, folder_id, prefix=''):
        items = []
        page_token = None
        while True:
            resp = self.service.files().list(
                q=f"'{folder_id}' in parents and trashed=false",
                fields='nextPageToken, files(id, name, mimeType, size, modifiedTime)',
                pageSize=1000,
                pageToken=page_token,
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
            ).execute()
            for item in resp.get('files', []):
                name = item.get('name', '')
                if item.get('mimeType') == 'application/vnd.google-apps.folder':
                    sub = f'{prefix}/{name}' if prefix else name
                    items.extend(self._list_folder(item['id'], sub))
                    continue
                rel = f'{prefix}/{name}' if prefix else name
                size = int(item.get('size') or 0)
                items.append({
                    'path': rel,
                    'name': name,
                    'id': item['id'],
                    'size': size,
                    'modified': _parse_drive_time(item.get('modifiedTime')),
                })
            page_token = resp.get('nextPageToken')
            if not page_token:
                break
        return items

    def refresh_index(self, force=False):
        ttl = int(os.environ.get('DRIVE_INDEX_TTL', '300'))
        now = time.time()
        if not force and self._files and (now - self._index_mtime) < ttl:
            return
        with self._index_lock:
            if not force and self._files and (now - self._index_mtime) < ttl:
                return
            all_files = self._list_folder(self.root_folder_id)
            self._files = {f['path']: f for f in all_files}
            self._save_index()

    def _entry_for_path(self, virtual_path):
        virtual_path = virtual_path.replace('\\', '/').lstrip('/')
        return self._files.get(virtual_path)

    def _cache_path(self, meta):
        ext = Path(meta['name']).suffix.lower()
        key = hashlib.md5(meta['id'].encode()).hexdigest()
        return self.cache_root / f'{key}{ext}'

    def ensure_local(self, virtual_path):
        meta = self._entry_for_path(virtual_path)
        if not meta:
            return None
        cache_path = self._cache_path(meta)
        try:
            if cache_path.is_file():
                size = meta.get('size') or 0
                if size <= 0 or cache_path.stat().st_size >= max(1, int(size * 0.95)):
                    return cache_path
        except OSError:
            pass

        lock_key = meta['id']
        with self._index_lock:
            lock = self._download_locks.get(lock_key)
            if lock is None:
                lock = threading.Lock()
                self._download_locks[lock_key] = lock

        with lock:
            try:
                if cache_path.is_file():
                    size = meta.get('size') or 0
                    if size <= 0 or cache_path.stat().st_size >= max(1, int(size * 0.95)):
                        return cache_path
            except OSError:
                pass
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = cache_path.with_suffix(cache_path.suffix + '.tmp')
            tmp.unlink(missing_ok=True)
            request = self.service.files().get_media(fileId=meta['id'])
            with open(tmp, 'wb') as fh:
                downloader = MediaIoBaseDownload(fh, request, chunksize=1024 * 1024)
                done = False
                while not done:
                    _, done = downloader.next_chunk()
            tmp.replace(cache_path)
            return cache_path

    def exists(self, virtual_path):
        return self._entry_for_path(virtual_path) is not None

    def scan_videos(self):
        self.refresh_index()
        videos = []
        prefix = self.video_prefix + '/'
        for path, meta in self._files.items():
            if not path.startswith(prefix) and path != self.video_prefix:
                continue
            if Path(meta['name']).suffix.lower() not in VIDEO_EXTENSIONS:
                continue
            videos.append({
                'name': meta['name'],
                'path': path,
                'size': meta.get('size', 0),
                'modified': meta.get('modified', 0),
            })
        videos.sort(key=lambda v: v['modified'], reverse=True)
        return videos

    def scan_photos(self):
        self.refresh_index()
        photos = []
        for path, meta in self._files.items():
            if Path(meta['name']).suffix.lower() not in IMAGE_EXTENSIONS:
                continue
            if not any(path.startswith(p + '/') or path == p for p in self.photo_prefixes):
                continue
            photos.append({
                'name': meta['name'],
                'path': path,
                'size': meta.get('size', 0),
                'modified': meta.get('modified', 0),
            })
        photos.sort(key=lambda v: v['modified'], reverse=True)
        return photos
