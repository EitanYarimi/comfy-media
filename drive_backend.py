"""Google Drive gateway: list files, then stream bytes on demand (no full downloads)."""

import hashlib
import json
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
FOLDER_MIME = 'application/vnd.google-apps.folder'


def _clean_id(value):
    """Render env values often include a trailing newline that breaks Drive queries."""
    return (value or '').strip().strip('"').strip("'").replace('\r', '').replace('\n', '')


def _parse_drive_time(value):
    if not value:
        return 0.0
    try:
        dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return dt.timestamp()
    except (TypeError, ValueError):
        return 0.0


class DriveStorage:
    """On-demand gateway over a shared Drive folder."""

    def __init__(self, cache_root: Path, video_dir: str, photo_dirs: list[str]):
        self.thumb_cache = cache_root / 'drive_thumbs'
        self.thumb_cache.mkdir(parents=True, exist_ok=True)
        self.video_prefix = video_dir.replace('\\', '/').strip('/')
        self.photo_prefixes = [p.replace('\\', '/').strip('/') for p in photo_dirs]
        self.root_folder_id = _clean_id(os.environ.get('DRIVE_ROOT_FOLDER_ID', ''))
        if not self.root_folder_id:
            raise RuntimeError('DRIVE_ROOT_FOLDER_ID is required for STORAGE_MODE=drive')
        self.creds, self.service, self.session = self._build_clients()
        self._files = {}
        self._videos = []
        self._photos = []
        self._root_name = ''
        self.videos_indexing = False
        self.photos_indexing = False
        self._videos_started = False
        self._photos_started = False
        self._list_lock = threading.Lock()

    def _build_clients(self):
        raw = (os.environ.get('GOOGLE_SERVICE_ACCOUNT_JSON') or '').strip()
        if not raw:
            raise RuntimeError('GOOGLE_SERVICE_ACCOUNT_JSON is required for STORAGE_MODE=drive')
        info = json.loads(raw)
        creds = service_account.Credentials.from_service_account_info(info, scopes=DRIVE_SCOPES)
        service = build('drive', 'v3', credentials=creds, cache_discovery=False)
        session = AuthorizedSession(creds)
        return creds, service, session

    def _ensure_root(self):
        if self._root_name:
            return
        try:
            root = self.service.files().get(
                fileId=self.root_folder_id,
                fields='id, name',
                supportsAllDrives=True,
            ).execute()
            self._root_name = root.get('name') or ''
        except Exception as exc:
            email = getattr(self.creds, 'service_account_email', 'the service account')
            raise RuntimeError(
                f'Cannot open Drive folder {self.root_folder_id!r}. '
                f'Share that folder with {email} as Viewer. Original error: {exc}'
            ) from exc

    def _drive_list(self, q, fields, page_size=1000, page_token=None):
        kwargs = {
            'q': q,
            'fields': fields,
            'pageSize': page_size,
            'supportsAllDrives': True,
            'includeItemsFromAllDrives': True,
        }
        if page_token:
            kwargs['pageToken'] = page_token
        try:
            return self.service.files().list(corpora='allDrives', **kwargs).execute()
        except Exception:
            return self.service.files().list(**kwargs).execute()

    def _find_child_folder(self, parent_id, name):
        parent_id = _clean_id(parent_id)
        safe = name.replace("'", "\\'")
        resp = self._drive_list(
            q=(
                f"'{parent_id}' in parents and trashed=false and "
                f"name='{safe}' and mimeType='{FOLDER_MIME}'"
            ),
            fields='files(id, name)',
            page_size=10,
        )
        files = resp.get('files') or []
        return files[0]['id'] if files else None

    def _folder_id_for_path(self, rel_path):
        current = self.root_folder_id
        parts = [p for p in rel_path.replace('\\', '/').strip('/').split('/') if p]
        root_name = (self._root_name or '').lower()
        if parts and root_name and parts[0].lower() == root_name:
            parts = parts[1:]
        if not parts:
            return current
        for part in parts:
            found = self._find_child_folder(current, part)
            if not found:
                return None
            current = found
        return current

    def _iter_children_pages(self, folder_id):
        folder_id = _clean_id(folder_id)
        page_token = None
        while True:
            resp = self._drive_list(
                q=f"'{folder_id}' in parents and trashed=false",
                fields='nextPageToken, files(id, name, mimeType, size, modifiedTime, thumbnailLink, hasThumbnail)',
                page_token=page_token,
            )
            yield resp.get('files', [])
            page_token = resp.get('nextPageToken')
            if not page_token:
                break

    def _file_entry(self, item, prefix):
        name = item.get('name', '')
        rel = f'{prefix}/{name}' if prefix else name
        return {
            'path': rel,
            'name': name,
            'id': item['id'],
            'size': int(item.get('size') or 0),
            'modified': _parse_drive_time(item.get('modifiedTime')),
            'mime': item.get('mimeType') or '',
            'thumbnailLink': item.get('thumbnailLink') or '',
            'hasThumbnail': bool(item.get('hasThumbnail')),
        }

    def _index_folder_files(self, folder_id, prefix, extensions, skip_folders=None, on_batch=None):
        skip_folders = {n.lower() for n in (skip_folders or [])}
        found = []
        for page in self._iter_children_pages(folder_id):
            extra_folders = []
            batch = []
            for item in page:
                name = item.get('name', '')
                mime = item.get('mimeType') or ''
                if mime == FOLDER_MIME:
                    if name.lower() in skip_folders:
                        continue
                    extra_folders.append((item['id'], f'{prefix}/{name}' if prefix else name))
                    continue
                if Path(name).suffix.lower() not in extensions:
                    continue
                entry = self._file_entry(item, prefix)
                found.append(entry)
                batch.append(entry)
            if batch and on_batch:
                on_batch(found)
            for child_id, child_prefix in extra_folders:
                found.extend(self._index_folder_files(
                    child_id, child_prefix, extensions, skip_folders=skip_folders, on_batch=on_batch
                ))
        return found

    def _index_videos(self):
        self.videos_indexing = True
        try:
            self._ensure_root()
            folder_id = None
            prefix = self.video_prefix or 'output/video'
            for rel in (self.video_prefix, 'output/video', 'video'):
                if not rel:
                    continue
                folder_id = self._folder_id_for_path(rel)
                if folder_id:
                    prefix = rel
                    break
            if not folder_id:
                print('   Drive video folder not found (tried output/video and video)')
                return
            print(f'   Indexing Drive videos from {prefix}...')

            def publish(items):
                with self._list_lock:
                    self._videos = sorted(items, key=lambda v: v['modified'], reverse=True)
                    for item in items:
                        self._files[item['path']] = item
                print(f'   Videos indexed so far: {len(items)}')

            items = self._index_folder_files(folder_id, prefix, VIDEO_EXTENSIONS, on_batch=publish)
            publish(items)
            print(f'   Videos ready: {len(items)}')
        except Exception as exc:
            print(f'   Video index failed: {exc}')
        finally:
            self.videos_indexing = False

    def _index_photos(self):
        self.photos_indexing = True
        try:
            self._ensure_root()
            collected = []

            def publish(items):
                with self._list_lock:
                    self._photos = sorted(items, key=lambda v: v['modified'], reverse=True)
                    for item in items:
                        self._files[item['path']] = item
                print(f'   Photos indexed so far: {len(items)}')

            prefixes = self.photo_prefixes or ['output']
            for rel in prefixes:
                folder_id = self._folder_id_for_path(rel) if rel else self.root_folder_id
                if not folder_id:
                    print(f'   Drive photo folder not found: {rel}')
                    continue
                print(f'   Indexing Drive photos from {rel or "/"}...')
                folder_items = self._index_folder_files(
                    folder_id,
                    rel,
                    IMAGE_EXTENSIONS,
                    skip_folders=['video'],
                    on_batch=lambda found, prev=collected: publish(prev + found),
                )
                collected.extend(folder_items)
            publish(collected)
            print(f'   Photos ready: {len(collected)}')
        except Exception as exc:
            print(f'   Photo index failed: {exc}')
        finally:
            self.photos_indexing = False

    def scan_videos(self):
        if not self._videos_started:
            self._videos_started = True
            threading.Thread(target=self._index_videos, daemon=True).start()
        with self._list_lock:
            return list(self._videos)

    def scan_photos(self):
        if not self._photos_started:
            self._photos_started = True
            threading.Thread(target=self._index_photos, daemon=True).start()
        with self._list_lock:
            return list(self._photos)

    def get_meta(self, virtual_path):
        virtual_path = virtual_path.replace('\\', '/').lstrip('/')
        with self._list_lock:
            found = self._files.get(virtual_path)
        if found:
            return found
        self.scan_videos()
        self.scan_photos()
        with self._list_lock:
            return self._files.get(virtual_path)

    def exists(self, virtual_path):
        return self.get_meta(virtual_path) is not None

    def open_media(self, file_id, range_header=None, timeout=120):
        headers = {}
        if range_header:
            headers['Range'] = range_header
        url = MEDIA_URL.format(file_id=file_id)
        return self.session.get(url, headers=headers, stream=True, timeout=timeout)

    def fetch_thumbnail(self, virtual_path, size=400):
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
