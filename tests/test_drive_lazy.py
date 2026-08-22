#!/usr/bin/env python3
"""Drive listing should fetch one page on load, not crawl the whole library."""

import os
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import drive_backend  # noqa: E402


def _blank_drive(tmp=None):
    storage = drive_backend.DriveStorage.__new__(drive_backend.DriveStorage)
    storage._files = {}
    storage._videos = []
    storage._photos = []
    storage._videos_token = None
    storage._videos_complete = False
    storage._videos_started = False
    storage._photos_complete = False
    storage._photos_started = False
    storage._month_state = {}
    storage._month_keys = None
    storage._list_lock = threading.Lock()
    storage._video_scan_lock = threading.Lock()
    storage._root_name = 'ComfyUI'
    storage.root_folder_id = 'root'
    storage.last_error = None
    storage.videos_error = None
    storage.photos_error = None
    storage.videos_indexing = False
    storage.photos_indexing = False
    if tmp is None:
        tmp = tempfile.mkdtemp(prefix='comfy-drive-index-')
    storage.index_dir = Path(tmp) / 'drive_index'
    storage.index_dir.mkdir(parents=True, exist_ok=True)
    storage._video_index_path = storage.index_dir / 'videos.json'
    storage._photo_index_path = storage.index_dir / 'photos.json'
    return storage


def _file(file_id, name, modified):
    return {
        'id': file_id,
        'name': name,
        'mimeType': 'video/mp4',
        'size': '10',
        'modifiedTime': modified,
        'thumbnailLink': '',
        'hasThumbnail': False,
    }


class LazyDriveListTests(unittest.TestCase):
    def test_month_utc_bounds(self):
        start, end = drive_backend.month_utc_bounds('2026-08')
        self.assertEqual(start, datetime(2026, 8, 1, tzinfo=timezone.utc))
        self.assertEqual(end, datetime(2026, 9, 1, tzinfo=timezone.utc))
        start, end = drive_backend.month_utc_bounds('2026-12')
        self.assertEqual(end, datetime(2027, 1, 1, tzinfo=timezone.utc))

    def test_summary_fetches_one_page(self):
        storage = _blank_drive()
        first = [_file(f'id{i}', f'clip{i}.mp4', '2026-08-18T12:00:00Z') for i in range(40)]
        extra = [_file('later', 'other.mp4', '2026-07-01T12:00:00Z')]
        calls = {'n': 0}

        def fake_query(extra_q='', page_size=40, page_token=None):
            calls['n'] += 1
            if page_token:
                return {'files': extra, 'nextPageToken': None}
            return {'files': first, 'nextPageToken': 'page-2'}

        with patch.object(storage, '_ensure_root'), \
             patch.object(storage, '_query_videos', side_effect=fake_query), \
             patch.object(storage, 'start_video_scan_if_needed'), \
             patch.object(storage, '_discover_month_keys', return_value=['2026-08']):
            result = storage.list_videos(summary=True, limit=40)

        self.assertEqual(calls['n'], 1)
        self.assertEqual(result['total'], 40)
        self.assertTrue(result['hasMore'])
        self.assertIsNone(result['error'])
        self.assertEqual(len(result['loaded']), 40)

    def test_older_page_only_when_requested(self):
        storage = _blank_drive()
        first = [_file(f'id{i}', f'clip{i}.mp4', '2026-08-18T12:00:00Z') for i in range(40)]
        second = [_file(f'old{i}', f'old{i}.mp4', '2026-07-01T12:00:00Z') for i in range(10)]

        def fake_query(extra_q='', page_size=40, page_token=None):
            if page_token == 'page-2':
                return {'files': second, 'nextPageToken': None}
            return {'files': first, 'nextPageToken': 'page-2'}

        with patch.object(storage, '_ensure_root'), \
             patch.object(storage, '_query_videos', side_effect=fake_query), \
             patch.object(storage, 'start_video_scan_if_needed'), \
             patch.object(storage, '_discover_month_keys', return_value=['2026-08', '2026-07']):
            storage.list_videos(summary=True, limit=40)
            later = storage.list_videos(offset=40, limit=40, summary=False)

        self.assertEqual(len(later['videos']), 10)
        self.assertFalse(later['hasMore'])

    def test_months_between_keys(self):
        keys = drive_backend.months_between_keys('2026-06', '2026-08')
        self.assertEqual(keys, ['2026-08', '2026-07', '2026-06'])

    def test_summary_months_from_oldest_newest(self):
        storage = _blank_drive()
        # Enough items to satisfy the first-page fill while staying incomplete
        # (nextPageToken set) so month discovery still queries Drive.
        first = [_file(f'id{i}', f'clip{i}.mp4', '2026-08-18T12:00:00Z') for i in range(40)]

        def fake_query(extra_q='', page_size=40, page_token=None):
            return {'files': first[:page_size], 'nextPageToken': 'more'}

        def fake_list(q, fields, page_size=1000, page_token=None, order_by=None):
            # oldest video call
            return {'files': [_file('old', 'old.mp4', '2026-06-02T12:00:00Z')]}

        with patch.object(storage, '_ensure_root'), \
             patch.object(storage, '_query_videos', side_effect=fake_query), \
             patch.object(storage, 'start_video_scan_if_needed'), \
             patch.object(storage, '_drive_list', side_effect=fake_list):
            result = storage.list_videos(summary=True, limit=40)

        months = [m['month'] for m in result['months']]
        self.assertEqual(months, ['2026-08', '2026-07', '2026-06'])
        self.assertEqual(result['months'][0]['count'], 40)
        self.assertTrue(result['hasMore'])

    def test_summary_refresh_merges_recent_window(self):
        storage = _blank_drive()
        existing = storage._entries_from_files([
            _file('a', 'a.mp4', '2026-08-18T12:00:00Z'),
        ])
        with storage._list_lock:
            storage._register_items(existing, as_videos=True)
            storage._videos_complete = True
            storage._videos_started = True
            storage._rebuild_month_keys_locked()

        refreshed = [
            _file('b', 'new.mp4', '2026-08-19T12:00:00Z'),
            _file('a', 'a.mp4', '2026-08-18T12:00:00Z'),
        ]
        query_calls = []

        def fake_query(extra_q='', page_size=40, page_token=None):
            query_calls.append(extra_q)
            return {'files': refreshed, 'nextPageToken': None}

        with patch.object(storage, '_ensure_root'), \
             patch.object(storage, '_query_videos', side_effect=fake_query), \
             patch.object(
                 storage,
                 '_recent_cutoff_iso',
                 return_value='2026-07-23T12:00:00Z',
             ), \
             patch.object(storage, '_discover_month_keys', return_value=['2026-08']):
            soft = storage.list_videos(summary=True)
            self.assertEqual([v['name'] for v in soft['videos']], ['a.mp4'])
            self.assertEqual(query_calls, [])

            third = storage.list_videos(summary=True, refresh=True)
            self.assertEqual([v['name'] for v in third['videos']], ['new.mp4', 'a.mp4'])
            self.assertTrue(any("modifiedTime >= '2026-07-23T12:00:00Z'" in (q or '') for q in query_calls))
            self.assertTrue(storage._video_index_path.is_file())

    def test_list_photos_has_more_from_memory(self):
        storage = _blank_drive()
        files = [
            {
                'id': f'p{i}',
                'name': f'pic{i}.png',
                'mimeType': 'image/png',
                'size': '10',
                'modifiedTime': f'2026-08-{(i % 28) + 1:02d}T12:00:00Z',
                'thumbnailLink': '',
                'hasThumbnail': False,
            }
            for i in range(50)
        ]
        items = storage._entries_from_files(files, extensions=drive_backend.IMAGE_EXTENSIONS)
        with storage._list_lock:
            storage._register_items(items, as_photos=True)
            storage._photos_complete = True
            storage._photos_started = True

        with patch.object(storage, '_ensure_root'):
            page1 = storage.list_photos(offset=0, limit=40, summary=False)
            page2 = storage.list_photos(offset=40, limit=40, summary=False)

        self.assertEqual(len(page1['photos']), 40)
        self.assertTrue(page1['hasMore'])
        self.assertEqual(page1['total'], 50)
        self.assertEqual(len(page2['photos']), 10)
        self.assertFalse(page2['hasMore'])
        self.assertFalse(page1.get('indexing'))

    def test_persisted_index_serves_without_drive(self):
        with tempfile.TemporaryDirectory() as tmp:
            storage = _blank_drive(tmp=tmp)
            items = storage._entries_from_files([
                _file('a', 'a.mp4', '2026-08-18T12:00:00Z'),
                _file('b', 'b.mp4', '2026-07-01T12:00:00Z'),
            ])
            with storage._list_lock:
                storage._register_items(items, as_videos=True)
                storage._videos_complete = True
                storage._videos_started = True
                storage._rebuild_month_keys_locked()
            storage._persist_videos()

            loaded = _blank_drive(tmp=tmp)
            loaded._load_persisted_indexes()
            self.assertTrue(loaded._videos_complete)
            self.assertEqual(len(loaded._videos), 2)

            with patch.object(loaded, '_ensure_root'), \
                 patch.object(loaded, '_query_videos') as query, \
                 patch.object(loaded, 'start_video_scan_if_needed') as start_scan:
                result = loaded.list_videos(summary=True)
                query.assert_not_called()
                start_scan.assert_not_called()

            self.assertEqual(result['total'], 2)
            self.assertEqual({v['name'] for v in result['videos']}, {'a.mp4', 'b.mp4'})
            self.assertFalse(result.get('indexing'))


if __name__ == '__main__':
    unittest.main()
