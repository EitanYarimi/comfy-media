"""Google Drive gateway: list files, then stream bytes on demand (no full downloads)."""

import hashlib
import json
import os
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

VIDEO_EXTENSIONS = {'.mp4', '.webm', '.ogg', '.mov', '.mkv', '.avi', '.m4v', '.3gp', '.flv', '.wmv'}
IMAGE_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp', '.svg', '.avif', '.heic'}

DRIVE_SCOPES = ['https://www.googleapis.com/auth/drive.readonly']
MEDIA_URL = 'https://www.googleapis.com/drive/v3/files/{file_id}?alt=media&supportsAllDrives=true'
FOLDER_MIME = 'application/vnd.google-apps.folder'
VIDEO_PAGE_SIZE = 40
REFRESH_LOOKBACK_DAYS = 30
VIDEO_LIST_FIELDS = (
    'nextPageToken, files(id, name, mimeType, size, modifiedTime, thumbnailLink, hasThumbnail)'
)
_VIDEO_EXT_QUERY = ' or '.join(
    f"fileExtension='{ext.lstrip('.')}'" for ext in sorted(VIDEO_EXTENSIONS)
)
VIDEO_FILE_QUERY = f"trashed=false and (mimeType contains 'video/' or {_VIDEO_EXT_QUERY})"
_IMAGE_EXT_QUERY = ' or '.join(
    f"fileExtension='{ext.lstrip('.')}'" for ext in sorted(IMAGE_EXTENSIONS)
)
IMAGE_FILE_QUERY = f"trashed=false and (mimeType contains 'image/' or {_IMAGE_EXT_QUERY})"
INDEX_ITEM_KEYS = ('path', 'name', 'id', 'size', 'modified', 'mime', 'thumbnailLink', 'hasThumbnail')


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


def month_utc_bounds(month):
    """Return UTC [start, end) datetimes for a YYYY-MM key."""
    year_s, month_s = month.split('-')
    year, mon = int(year_s), int(month_s)
    start = datetime(year, mon, 1, tzinfo=timezone.utc)
    if mon == 12:
        end = datetime(year + 1, 1, 1, tzinfo=timezone.utc)
    else:
        end = datetime(year, mon + 1, 1, tzinfo=timezone.utc)
    return start, end


def month_key_from_ts(modified_ts):
    dt = datetime.fromtimestamp(float(modified_ts), tz=timezone.utc)
    return f'{dt.year}-{dt.month:02d}'


def months_between_keys(oldest_key, newest_key):
    """Inclusive YYYY-MM range, newest first."""
    if not oldest_key or not newest_key:
        return []
    oy, om = map(int, oldest_key.split('-'))
    ny, nm = map(int, newest_key.split('-'))
    if (oy, om) > (ny, nm):
        oy, om, ny, nm = ny, nm, oy, om
    keys = []
    y, m = oy, om
    while (y, m) <= (ny, nm):
        keys.append(f'{y}-{m:02d}')
        m += 1
        if m > 12:
            m = 1
            y += 1
    keys.reverse()
    return keys


class DriveStorage:
    """On-demand gateway over a shared Drive folder."""

    def __init__(self, cache_root: Path, video_dir: str, photo_dirs: list[str]):
        self.thumb_cache = cache_root / 'drive_thumbs'
        self.thumb_cache.mkdir(parents=True, exist_ok=True)
        self.index_dir = Path(cache_root) / 'drive_index'
        self.index_dir.mkdir(parents=True, exist_ok=True)
        self._video_index_path = self.index_dir / 'videos.json'
        self._photo_index_path = self.index_dir / 'photos.json'
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
        self.last_error = None
        self.videos_error = None
        self.photos_error = None
        self.videos_indexing = False
        self.photos_indexing = False
        self._videos_token = None
        self._videos_complete = False
        self._videos_started = False
        self._photos_complete = False
        self._month_state = {}
        self._month_keys = None
        self._photos_started = False
        self._list_lock = threading.Lock()
        self._video_scan_lock = threading.Lock()
        self._load_persisted_indexes()

    def _build_clients(self):
        from google.auth.transport.requests import AuthorizedSession
        from google.oauth2 import service_account
        from googleapiclient.discovery import build

        raw = (os.environ.get('GOOGLE_SERVICE_ACCOUNT_JSON') or '').strip()
        if not raw:
            raise RuntimeError('GOOGLE_SERVICE_ACCOUNT_JSON is required for STORAGE_MODE=drive')
        info = json.loads(raw)
        creds = service_account.Credentials.from_service_account_info(info, scopes=DRIVE_SCOPES)
        try:
            import httplib2
            from google_auth_httplib2 import AuthorizedHttp
            http = httplib2.Http(timeout=30)
            service = build(
                'drive', 'v3', http=AuthorizedHttp(creds, http=http), cache_discovery=False
            )
        except ImportError:
            service = build('drive', 'v3', credentials=creds, cache_discovery=False)
        session = AuthorizedSession(creds)
        return creds, service, session

    def _serialize_item(self, item):
        return {key: item.get(key) for key in INDEX_ITEM_KEYS}

    def _read_index_file(self, path):
        try:
            if not path.is_file():
                return None
            data = json.loads(path.read_text(encoding='utf-8'))
            if not isinstance(data, list):
                return None
            items = []
            for raw in data:
                if not isinstance(raw, dict) or not raw.get('id'):
                    continue
                item = {key: raw.get(key) for key in INDEX_ITEM_KEYS}
                item['size'] = int(item.get('size') or 0)
                item['modified'] = float(item.get('modified') or 0)
                item['mime'] = item.get('mime') or ''
                item['thumbnailLink'] = item.get('thumbnailLink') or ''
                item['hasThumbnail'] = bool(item.get('hasThumbnail'))
                item['path'] = item.get('path') or item.get('name') or item['id']
                item['name'] = item.get('name') or Path(item['path']).name
                items.append(item)
            return items
        except Exception as exc:
            print(f'   Failed to read Drive index {path}: {exc}')
            return None

    def _write_index_file(self, path, items):
        payload = [self._serialize_item(item) for item in items]
        tmp = path.with_suffix(path.suffix + '.tmp')
        tmp.write_text(json.dumps(payload), encoding='utf-8')
        tmp.replace(path)

    def _register_items(self, items, *, as_videos=False, as_photos=False):
        """Register items into _files / lists. Caller must hold _list_lock."""
        if as_videos:
            for old in self._videos:
                self._files.pop(old['path'], None)
            self._videos = sorted(items, key=lambda video: video['modified'], reverse=True)
            for item in self._videos:
                self._files[item['path']] = item
        if as_photos:
            for old in self._photos:
                self._files.pop(old['path'], None)
            self._photos = sorted(items, key=lambda photo: photo['modified'], reverse=True)
            for item in self._photos:
                self._files[item['path']] = item

    def _load_persisted_indexes(self):
        videos = self._read_index_file(self._video_index_path)
        if videos is not None:
            with self._list_lock:
                self._register_items(videos, as_videos=True)
                self._videos_complete = True
                self._videos_started = True
                self._videos_token = None
                self._rebuild_month_keys_locked()
            print(f'   Loaded {len(videos)} videos from disk index')
        photos = self._read_index_file(self._photo_index_path)
        if photos is not None:
            with self._list_lock:
                self._register_items(photos, as_photos=True)
                self._photos_complete = True
                self._photos_started = True
            print(f'   Loaded {len(photos)} photos from disk index')

    def _persist_videos(self):
        # Copy under the list lock, but never call _months_payload here — Lock is not reentrant.
        with self._list_lock:
            items = [self._serialize_item(video) for video in self._videos]
        try:
            self._write_index_file(self._video_index_path, items)
        except Exception as exc:
            print(f'   Failed to persist video index: {exc}')

    def _persist_photos(self):
        with self._list_lock:
            items = [self._serialize_item(photo) for photo in self._photos]
        try:
            self._write_index_file(self._photo_index_path, items)
        except Exception as exc:
            print(f'   Failed to persist photo index: {exc}')

    def _rebuild_month_keys_locked(self):
        """Rebuild month keys from in-memory videos. Caller must hold _list_lock."""
        if not self._videos:
            self._month_keys = []
            return
        oldest = min(video['modified'] for video in self._videos)
        newest = max(video['modified'] for video in self._videos)
        self._month_keys = months_between_keys(month_key_from_ts(oldest), month_key_from_ts(newest))

    def ensure_full_index(self):
        self.start_video_scan_if_needed()
        self.start_photo_scan_if_needed()

    def start_video_scan_if_needed(self):
        with self._list_lock:
            if self._videos_complete or self._videos_started or self.videos_indexing:
                return
            self._videos_started = True
            self.videos_indexing = True
        threading.Thread(target=self._full_scan_videos, daemon=True).start()

    def start_photo_scan_if_needed(self):
        with self._list_lock:
            if self._photos_complete or self._photos_started or self.photos_indexing:
                return
            self._photos_started = True
        threading.Thread(target=self._index_photos, daemon=True).start()

    def _recent_cutoff_iso(self):
        """ISO timestamp for now minus REFRESH_LOOKBACK_DAYS (UTC)."""
        cutoff = datetime.now(timezone.utc) - timedelta(days=REFRESH_LOOKBACK_DAYS)
        return cutoff.strftime('%Y-%m-%dT%H:%M:%SZ')

    def _merge_items_by_id(self, existing, incoming):
        by_id = {item['id']: dict(item) for item in existing}
        used_paths = {item['path'] for item in existing}
        for item in incoming:
            item = dict(item)
            if item['id'] in by_id:
                kept_path = by_id[item['id']]['path']
                item['path'] = kept_path
                by_id[item['id']] = item
                continue
            if item['path'] in used_paths:
                item['path'] = f"{item['id']}/{item['name']}"
            by_id[item['id']] = item
            used_paths.add(item['path'])
        return sorted(by_id.values(), key=lambda entry: entry['modified'], reverse=True)

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
            self.last_error = (
                f'Cannot open Drive folder {self.root_folder_id!r}. '
                f'Share that folder with {email} as Viewer. Original error: {exc}'
            )
            raise RuntimeError(self.last_error) from exc

    def _drive_list(self, q, fields, page_size=1000, page_token=None, order_by=None):
        kwargs = {
            'q': q,
            'fields': fields,
            'pageSize': page_size,
            'supportsAllDrives': True,
            'includeItemsFromAllDrives': True,
        }
        if page_token:
            kwargs['pageToken'] = page_token
        if order_by:
            kwargs['orderBy'] = order_by
        try:
            return self.service.files().list(corpora='allDrives', **kwargs).execute()
        except Exception:
            fallback = dict(kwargs)
            fallback.pop('orderBy', None)
            try:
                return self.service.files().list(corpora='allDrives', **fallback).execute()
            except Exception:
                return self.service.files().list(**fallback).execute()

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

    def _iter_query_pages(self, q, fields, page_size=1000):
        page_token = None
        while True:
            resp = self._drive_list(q=q, fields=fields, page_size=page_size, page_token=page_token)
            yield resp.get('files', [])
            page_token = resp.get('nextPageToken')
            if not page_token:
                break

    def _iter_children_pages(self, folder_id):
        folder_id = _clean_id(folder_id)
        yield from self._iter_query_pages(
            q=f"'{folder_id}' in parents and trashed=false",
            fields='nextPageToken, files(id, name, mimeType, size, modifiedTime, thumbnailLink, hasThumbnail)',
        )

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

    def _query_videos(self, extra_q='', page_size=VIDEO_PAGE_SIZE, page_token=None):
        q = VIDEO_FILE_QUERY
        if extra_q:
            q = f'{q} and ({extra_q})'
        return self._drive_list(
            q=q,
            fields=VIDEO_LIST_FIELDS,
            page_size=page_size,
            page_token=page_token,
            order_by='modifiedTime desc',
        )

    def _query_images(self, extra_q='', page_size=VIDEO_PAGE_SIZE, page_token=None):
        q = IMAGE_FILE_QUERY
        if extra_q:
            q = f'{q} and ({extra_q})'
        return self._drive_list(
            q=q,
            fields=VIDEO_LIST_FIELDS,
            page_size=page_size,
            page_token=page_token,
            order_by='modifiedTime desc',
        )

    def _entries_from_files(self, files, extensions=None):
        allowed = VIDEO_EXTENSIONS if extensions is None else extensions
        items = []
        for item in files or []:
            name = item.get('name', '')
            if Path(name).suffix.lower() not in allowed:
                continue
            items.append(self._file_entry(item, prefix=''))
        return items

    def _remember_videos(self, items):
        with self._list_lock:
            known = {video['id'] for video in self._videos}
            used_paths = {video['path'] for video in self._videos}
            for item in items:
                if item['id'] in known:
                    continue
                if item['path'] in used_paths:
                    item = dict(item)
                    item['path'] = f"{item['id']}/{item['name']}"
                self._videos.append(item)
                self._files[item['path']] = item
                known.add(item['id'])
                used_paths.add(item['path'])
            self._videos.sort(key=lambda video: video['modified'], reverse=True)

    def reset_video_cache(self):
        """Drop cached video pages so the next list hits Drive again."""
        with self._list_lock:
            # Keep _files for open streams; remove video entries that came from lists.
            video_paths = {v['path'] for v in self._videos}
            for path in video_paths:
                self._files.pop(path, None)
            self._videos = []
            self._videos_token = None
            self._videos_complete = False
            self._videos_started = False
            self._month_state = {}
            self._month_keys = None
            self.videos_error = None

    def reset_photo_cache(self):
        """Drop photo index and rescan on next request."""
        with self._list_lock:
            photo_paths = {p['path'] for p in self._photos}
            for path in photo_paths:
                self._files.pop(path, None)
            self._photos = []
            self._photos_started = False
            self._photos_complete = False
            self.photos_error = None

    def _discover_month_keys(self):
        """Build the full month list from oldest+newest video only (2 Drive calls)."""
        if self._month_keys is not None:
            return list(self._month_keys)
        self._ensure_root()
        try:
            newest_resp = self._query_videos(page_size=1)
            newest_files = newest_resp.get('files') or []
            if not newest_files:
                self._month_keys = []
                return []
            newest_ts = _parse_drive_time(newest_files[0].get('modifiedTime'))
            oldest_resp = self._drive_list(
                q=VIDEO_FILE_QUERY,
                fields='files(id, modifiedTime)',
                page_size=1,
                order_by='modifiedTime',
            )
            oldest_files = oldest_resp.get('files') or []
            oldest_ts = (
                _parse_drive_time(oldest_files[0].get('modifiedTime'))
                if oldest_files else newest_ts
            )
            keys = months_between_keys(month_key_from_ts(oldest_ts), month_key_from_ts(newest_ts))
            self._month_keys = keys
            return list(keys)
        except Exception as exc:
            print(f'   Month discovery failed: {exc}')
            return []

    def _months_payload(self, loaded):
        """Months from oldest→newest span; counts filled only for already-loaded items."""
        counts = {}
        for item in loaded or []:
            key = month_key_from_ts(item['modified'])
            counts[key] = counts.get(key, 0) + 1
        keys = self._discover_month_keys()
        if not keys and counts:
            keys = sorted(counts.keys(), reverse=True)
        return [{'month': key, 'count': counts.get(key, 0)} for key in keys]

    def _fill_recent(self, min_count):
        self._ensure_root()
        finished_complete = False
        with self._video_scan_lock:
            while True:
                with self._list_lock:
                    have = len(self._videos)
                    complete = self._videos_complete
                    token = self._videos_token
                if complete or have >= min_count:
                    return
                resp = self._query_videos(page_size=VIDEO_PAGE_SIZE, page_token=token)
                files = resp.get('files') or []
                self._remember_videos(self._entries_from_files(files))
                next_token = resp.get('nextPageToken')
                with self._list_lock:
                    self._videos_token = next_token
                    if not next_token or not files:
                        self._videos_complete = True
                        self._rebuild_month_keys_locked()
                        finished_complete = True
                        break
        if finished_complete:
            self._persist_videos()

    def _full_scan_videos(self):
        try:
            self.videos_error = None
            self._ensure_root()
            print('   Scanning Drive videos (one-time full index)...')
            with self._video_scan_lock:
                while True:
                    with self._list_lock:
                        if self._videos_complete:
                            break
                        token = self._videos_token
                    resp = self._query_videos(page_size=1000, page_token=token)
                    files = resp.get('files') or []
                    self._remember_videos(self._entries_from_files(files))
                    next_token = resp.get('nextPageToken')
                    with self._list_lock:
                        self._videos_token = next_token
                        count = len(self._videos)
                        if not next_token or not files:
                            self._videos_complete = True
                            self._rebuild_month_keys_locked()
                            break
                    print(f'   Videos indexed so far: {count}')
            with self._list_lock:
                total = len(self._videos)
            self._persist_videos()
            print(f'   Videos ready: {total} (saved to disk index)')
        except Exception as exc:
            self.videos_error = str(exc)
            print(f'   Video full scan failed: {exc}')
        finally:
            self.videos_indexing = False

    def _refresh_recent_videos(self):
        """Merge videos modified in the last REFRESH_LOOKBACK_DAYS into the saved library."""
        self._ensure_root()
        start_iso = self._recent_cutoff_iso()
        extra = f"modifiedTime >= '{start_iso}'"
        incoming = []
        token = None
        while True:
            resp = self._query_videos(extra_q=extra, page_size=1000, page_token=token)
            files = resp.get('files') or []
            incoming.extend(self._entries_from_files(files))
            token = resp.get('nextPageToken')
            if not token or not files:
                break
        with self._list_lock:
            merged = self._merge_items_by_id(self._videos, incoming)
            self._register_items(merged, as_videos=True)
            self._videos_complete = True
            self._videos_started = True
            self._videos_token = None
            self._month_state.clear()
            self._rebuild_month_keys_locked()
        self._persist_videos()
        print(f'   Refresh: merged {len(incoming)} videos from last {REFRESH_LOOKBACK_DAYS} days')

    def _refresh_recent_photos(self):
        """Merge images modified in the last REFRESH_LOOKBACK_DAYS into the saved library."""
        self._ensure_root()
        start_iso = self._recent_cutoff_iso()
        extra = f"modifiedTime >= '{start_iso}'"
        incoming = []
        token = None
        while True:
            resp = self._query_images(extra_q=extra, page_size=1000, page_token=token)
            files = resp.get('files') or []
            incoming.extend(self._entries_from_files(files, extensions=IMAGE_EXTENSIONS))
            token = resp.get('nextPageToken')
            if not token or not files:
                break
        with self._list_lock:
            merged = self._merge_items_by_id(self._photos, incoming)
            self._register_items(merged, as_photos=True)
            self._photos_complete = True
            self._photos_started = True
        self._persist_photos()
        print(f'   Refresh: merged {len(incoming)} photos from last {REFRESH_LOOKBACK_DAYS} days')

    def _videos_from_memory(self, month=None, q=None, offset=0, limit=VIDEO_PAGE_SIZE, summary=False):
        with self._list_lock:
            videos = list(self._videos)
            complete = self._videos_complete
            indexing = bool(self.videos_indexing or not complete)
        if q:
            needle = q.lower()
            videos = [video for video in videos if needle in (video.get('name') or '').lower()]
        if month:
            start, end = month_utc_bounds(month)
            start_ts, end_ts = start.timestamp(), end.timestamp()
            videos = [
                video for video in videos
                if start_ts <= float(video.get('modified') or 0) < end_ts
            ]
        if summary:
            page = videos[:VIDEO_PAGE_SIZE]
            # Counts use all loaded videos; month keys come from discover/rebuild.
            months = self._months_payload(videos)
            return {
                'videos': page,
                'loaded': page,
                'months': months,
                'total': len(videos),
                'hasMore': not complete,
                'indexing': indexing,
                'error': None,
            }
        page = videos[offset:offset + limit]
        return {
            'videos': page,
            'loaded': videos,
            'months': self._months_payload(videos) if self._month_keys is not None else None,
            'total': len(videos),
            'hasMore': (offset + limit) < len(videos) if complete else True,
            'indexing': indexing,
            'error': None,
        }

    def _paged_video_query(self, extra_q, offset, limit, state):
        needed = offset + limit
        while not state['complete'] and len(state['items']) < needed:
            resp = self._query_videos(
                extra_q=extra_q,
                page_size=VIDEO_PAGE_SIZE,
                page_token=state['token'],
            )
            files = resp.get('files') or []
            batch = self._entries_from_files(files)
            self._remember_videos(batch)
            state['items'].extend(batch)
            state['token'] = resp.get('nextPageToken')
            if not state['token'] or not files:
                state['complete'] = True
                break
        page = state['items'][offset:offset + limit]
        return page, len(state['items']), not state['complete']

    def _month_videos(self, month, offset, limit):
        start, end = month_utc_bounds(month)
        extra = (
            f"modifiedTime >= '{start.strftime('%Y-%m-%dT%H:%M:%SZ')}' and "
            f"modifiedTime < '{end.strftime('%Y-%m-%dT%H:%M:%SZ')}'"
        )
        state = self._month_state.setdefault(month, {'items': [], 'token': None, 'complete': False})
        return self._paged_video_query(extra, offset, limit, state)

    def _search_videos(self, q, offset, limit):
        safe = q.replace("'", "\\'")[:100]
        extra = f"name contains '{safe}'"
        state = {'items': [], 'token': None, 'complete': False}
        return self._paged_video_query(extra, offset, limit, state)

    def list_videos(self, month=None, q=None, offset=0, limit=VIDEO_PAGE_SIZE, summary=False, refresh=False):
        """Serve from persisted/full index; Refresh merges files from the last REFRESH_LOOKBACK_DAYS."""
        self.videos_error = None
        try:
            self._ensure_root()
            if refresh:
                self._refresh_recent_videos()
                return self._videos_from_memory(
                    month=month, q=q, offset=offset, limit=limit, summary=summary
                )

            if self._videos_complete or self.videos_indexing:
                return self._videos_from_memory(
                    month=month, q=q, offset=offset, limit=limit, summary=summary
                )

            # Cold start: first page quickly, then background full scan.
            if q:
                page, total, has_more = self._search_videos(q, offset, limit)
                self.start_video_scan_if_needed()
                return {
                    'videos': page,
                    'loaded': page,
                    'total': total,
                    'hasMore': has_more,
                    'indexing': True,
                    'error': None,
                }
            if month:
                page, total, has_more = self._month_videos(month, offset, limit)
                self.start_video_scan_if_needed()
                return {
                    'videos': page,
                    'loaded': page,
                    'total': total,
                    'hasMore': has_more,
                    'indexing': True,
                    'error': None,
                }

            # Cold start only sync-fills a small first page. Large limits (slideshow)
            # must not crawl Drive on the request thread — that times out on Render.
            if summary:
                wanted = VIDEO_PAGE_SIZE
            else:
                wanted = min(max(offset + limit, VIDEO_PAGE_SIZE), VIDEO_PAGE_SIZE * 2)
            self._fill_recent(wanted)
            self.start_video_scan_if_needed()
            return self._videos_from_memory(
                month=month, q=q, offset=offset, limit=limit, summary=summary
            )
        except Exception as exc:
            self.videos_error = str(exc)
            print(f'   Video list failed: {exc}')
            return {
                'videos': [],
                'loaded': [],
                'total': 0,
                'hasMore': False,
                'indexing': False,
                'error': self.videos_error,
            }

    def _index_photos(self):
        self.photos_indexing = True
        try:
            self.photos_error = None
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
            with self._list_lock:
                self._photos_complete = True
            self._persist_photos()
            print(f'   Photos ready: {len(collected)} (saved to disk index)')
        except Exception as exc:
            self.photos_error = str(exc)
            print(f'   Photo index failed: {exc}')
        finally:
            self.photos_indexing = False

    def scan_videos(self):
        result = self.list_videos(summary=True)
        return list(result.get('loaded') or [])

    def _photo_months_payload(self, photos):
        """Month chips from photos actually in memory (newest first)."""
        counts = {}
        for item in photos or []:
            key = month_key_from_ts(item['modified'])
            counts[key] = counts.get(key, 0) + 1
        keys = sorted(counts.keys(), reverse=True)
        return [{'month': key, 'count': counts[key]} for key in keys]

    def _photos_from_memory(self, month=None, q=None, offset=0, limit=VIDEO_PAGE_SIZE, summary=False):
        with self._list_lock:
            all_photos = list(self._photos)
            complete = self._photos_complete
            indexing = bool(self.photos_indexing or not complete)
        months = self._photo_months_payload(all_photos)
        photos = all_photos
        if q:
            needle = q.lower()
            photos = [photo for photo in photos if needle in (photo.get('name') or '').lower()]
        if month:
            start, end = month_utc_bounds(month)
            start_ts, end_ts = start.timestamp(), end.timestamp()
            photos = [
                photo for photo in photos
                if start_ts <= float(photo.get('modified') or 0) < end_ts
            ]
        if summary:
            page = photos[:VIDEO_PAGE_SIZE]
            return {
                'photos': page,
                'loaded': page,
                'months': months,
                'total': len(photos),
                'hasMore': not complete,
                'indexing': indexing,
                'error': None,
            }
        page = photos[offset:offset + limit]
        return {
            'photos': page,
            'loaded': photos,
            'months': months,
            'total': len(photos),
            'hasMore': (offset + limit) < len(photos) if complete else True,
            'indexing': indexing,
            'error': None,
        }

    def list_photos(self, month=None, q=None, offset=0, limit=VIDEO_PAGE_SIZE, summary=False, refresh=False):
        """Serve photos from memory like list_videos; Refresh merges last REFRESH_LOOKBACK_DAYS."""
        self.photos_error = None
        try:
            self._ensure_root()
            if refresh:
                self._refresh_recent_photos()
                return self._photos_from_memory(
                    month=month, q=q, offset=offset, limit=limit, summary=summary
                )

            if self._photos_complete or self.photos_indexing:
                return self._photos_from_memory(
                    month=month, q=q, offset=offset, limit=limit, summary=summary
                )

            # Cold start: progressive folder scan publishes as it walks; return what we have.
            self.start_photo_scan_if_needed()
            return self._photos_from_memory(
                month=month, q=q, offset=offset, limit=limit, summary=summary
            )
        except Exception as exc:
            self.photos_error = str(exc)
            print(f'   Photo list failed: {exc}')
            return {
                'photos': [],
                'loaded': [],
                'total': 0,
                'hasMore': False,
                'indexing': False,
                'error': self.photos_error,
            }

    def scan_photos(self, refresh=False):
        if refresh:
            self._refresh_recent_photos()
            with self._list_lock:
                return list(self._photos)
        result = self.list_photos(summary=True)
        return list(result.get('loaded') or [])

    def _lookup_by_name(self, name):
        if not name:
            return None
        safe = name.replace("'", "\\'")
        try:
            self._ensure_root()
            resp = self._drive_list(
                q=f"name='{safe}' and trashed=false",
                fields=VIDEO_LIST_FIELDS,
                page_size=5,
            )
        except Exception:
            return None
        entries = self._entries_from_files(resp.get('files') or [])
        if not entries:
            entries = [self._file_entry(item, prefix='') for item in (resp.get('files') or [])]
        if not entries:
            return None
        entry = entries[0]
        with self._list_lock:
            self._files[entry['path']] = entry
        return entry

    def get_meta(self, virtual_path):
        virtual_path = virtual_path.replace('\\', '/').lstrip('/')
        name = Path(virtual_path).name
        with self._list_lock:
            found = self._files.get(virtual_path)
            if found:
                return found
            for item in self._files.values():
                if item.get('path') == virtual_path or item.get('name') == name:
                    return item
        return self._lookup_by_name(name)

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
            except Exception:
                continue
            ctype = (resp.headers.get('Content-Type') or '').split(';')[0].strip()
            if resp.status_code == 200 and resp.content and ctype.startswith('image/'):
                try:
                    cache_path.write_bytes(resp.content)
                except OSError:
                    pass
                return resp.content, ctype
        return None
