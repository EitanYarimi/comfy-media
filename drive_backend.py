"""Google Drive gateway: list files, then stream bytes on demand (no full downloads)."""

import hashlib
import json
import mimetypes
import os
import re
import threading
import time
from datetime import datetime
from pathlib import Path

import requests
from google.auth.transport.requests import AuthorizedSession
from google.oauth2 import service_account
from googleapiclient.discovery import build

VIDEO_EXTENSIONS = {'.mp4', '.webm', '.ogg', '.mov', '.mkv', '.avi', '.m4v', '.3gp', '.flv', '.wmv'}
IMAGE_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp', '.svg', '.avif', '.heic'}

DRIVE_SCOPES = ['https://www.googleapis.com/auth/drive.readonly']
MEDIA_URL = 'https://www.googleapis.com/drive/v3/files/{file_id}?alt=media&supportsAllDrives=true'


def _parse_drive_time(value):
    if not value:
        return 0.0
    try:
        dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return dt.timestamp()
    except (TypeError, ValueError):
        return 0.0


class DriveStorage:
    """On-demand gateway over a shared Drive folder (virtual paths like ComfyUI/output/...)."""

    def __init__(self, cache_root: Path, video_dir: str, photo_dirs: list[str]):
        self.thumb_cache = cache_root / 'drive_thumbs'
        self.thumb_cache.mkdir(parents=True, exist_ok=True)
        self.index_path = cache_root / 'drive_index_v1.json'
        self.video_prefix = video_dir.replace('\\', '/').strip('/')
        self.photo_prefixes = [p.replace('\\', '/').strip('/') for p in photo_dirs]
        self.root_folder_id = os.environ['DRIVE_ROOT_FOLDER_ID']
        self.creds, self.service, self.session = self._build_clients()
        self._files = {}
        self._index_mtime = 0.0
        self._index_lock = threading.Lock()
        self._load_index()

    def _build_clients(self):
        raw = os.environ.get('GOOGLE_SERVICE_ACCOUNT_JSON', '')
        if not raw:
            raise RuntimeError('GOOGLE_SERVICE_ACCOUNT_JSON is required for STORAGE_MODE=drive')
        info = json.loads(raw)
        creds = service_account.Credentials.from_service_account_info(info, scopes=DRIVE_SCOPES)
        service = build('drive', 'v3', credentials=creds, cache_discovery=False)
        session = AuthorizedSession(creds)
        return creds, service, session

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

    def _list_children(self, folder_id):
        items = []
        page_token = None
        while True:
            resp = self.service.files().list(
                q=f"'{folder_id}' in parents and trashed=false",
                fields='nextPageToken, files(id, name, mimeType, size, modifiedTime, thumbnailLink, hasThumbnail)',
                pageSize=1000,
                pageToken=page_token,
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
            ).execute()
            items.extend(resp.get('files', []))
            page_token = resp.get('nextPageToken')
            if not page_token:
                break
        return items

    def _find_child_folder(self, parent_id, name):
        safe = name.replace("'", "\\'")
        resp = self.service.files().list(
            q=(
                f"'{parent_id}' in parents and trashed=false and "
                f"name='{safe}' and mimeType='application/vnd.google-apps.folder'"
            ),
            fields='files(id, name)',
            pageSize=10,
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        ).execute()
        files = resp.get('files') or []
        return files[0]['id'] if files else None

    def _folder_id_for_path(self, rel_path):
        current = self.root_folder_id
        parts = [p for p in rel_path.replace('\\', '/').strip('/').split('/') if p]
        for part in parts:
            found = self._find_child_folder(current, part)
            if not found:
                return None
            current = found
        return current

    def _list_folder_tree(self, folder_id, prefix=''):
        items = []
        for item in self._list_children(folder_id):
            name = item.get('name', '')
            if item.get('mimeType') == 'application/vnd.google-apps.folder':
                sub = f'{prefix}/{name}' if prefix else name
                items.extend(self._list_folder_tree(item['id'], sub))
                continue
            rel = f'{prefix}/{name}' if prefix else name
            size = int(item.get('size') or 0)
            items.append({
                'path': rel,
                'name': name,
                'id': item['id'],
                'size': size,
                'modified': _parse_drive_time(item.get('modifiedTime')),
                'mime': item.get('mimeType') or '',
                'thumbnailLink': item.get('thumbnailLink') or '',
                'hasThumbnail': bool(item.get('hasThumbnail')),
            })
        return items

    def refresh_index(self, force=False):
        ttl = int(os.environ.get('DRIVE_INDEX_TTL', '300'))
        now = time.time()
        if not force and self._files and (now - self._index_mtime) < ttl:
            return
        with self._index_lock:
            if not force and self._files and (now - self._index_mtime) < ttl:
                return
            collected = {}
            roots = []
            if self.video_prefix:
                roots.append(self.video_prefix)
            roots.extend(self.photo_prefixes)
            seen = set()
            for rel in roots:
                if not rel or rel in seen:
                    continue
                seen.add(rel)
                folder_id = self._folder_id_for_path(rel)
                if not folder_id:
                    continue
                for item in self._list_folder_tree(folder_id, rel):
                    collected[item['path']] = item
            self._files = collected
            self._save_index()

    def get_meta(self, virtual_path):
        virtual_path = virtual_path.replace('\\', '/').lstrip('/')
        if virtual_path not in self._files:
            self.refresh_index()
        return self._files.get(virtual_path)

    def exists(self, virtual_path):
        return self.get_meta(virtual_path) is not None

    def open_media(self, file_id, range_header=None, timeout=120):
        """Open a Drive media stream. Pass Range so only requested bytes are fetched."""
        headers = {}
        if range_header:
            headers['Range'] = range_header
        url = MEDIA_URL.format(file_id=file_id)
        return self.session.get(url, headers=headers, stream=True, timeout=timeout)

    def fetch_thumbnail(self, virtual_path, size=400):
        """Fetch Drive's own thumbnail on demand — never downloads the source video."""
        meta = self.get_meta(virtual_path)
        if not meta:
            return None
        cache_key = hashlib.md5(f"{meta['id']}:s{size}".encode()).hexdigest()
        cache_path = self.thumb_cache / f'{cache_key}.jpg'
        try:
            if cache_path.is_file() and cache_path.stat().st_size > 0:
                return cache_path.read_bytes(), 'image/jpeg'
        except OSError:
            pass

        urls = []
        link = meta.get('thumbnailLink') or ''
        if link:
            resized = re.sub(r'=s\d+', f'=s{size}', link)
            urls.append(resized)
            if resized != link:
                urls.append(link)
        urls.append(f'https://lh3.googleusercontent.com/d/{meta["id"]}=s{size}')
        urls.append(f'https://drive.google.com/thumbnail?id={meta["id"]}&sz=w{size}')

        seen = set()
        for url in urls:
            if not url or url in seen:
                continue
            seen.add(url)
            try:
                resp = self.session.get(url, timeout=30)
            except requests.RequestException:
                continue
            ctype = (resp.headers.get('Content-Type') or '').split(';')[0].strip()
            if resp.status_code == 200 and resp.content and ctype.startswith('image/'):
                try:
                    cache_path.write_bytes(resp.content)
                except OSError:
                    pass
                return resp.content, ctype
        return None

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
