#!/usr/bin/env python3
"""Drive listing should fetch one page on load, not crawl the whole library."""

import os
import sys
import threading
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import drive_backend  # noqa: E402


def _blank_drive():
    storage = drive_backend.DriveStorage.__new__(drive_backend.DriveStorage)
    storage._files = {}
    storage._videos = []
    storage._videos_token = None
    storage._videos_complete = False
    storage._month_state = {}
    storage._month_keys = None
    storage._list_lock = threading.Lock()
    storage._root_name = 'ComfyUI'
    storage.root_folder_id = 'root'
    storage.last_error = None
    storage.videos_error = None
    storage.videos_indexing = False
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
        first = [_file(f'id{i}', f'clip{i}.mp4', '2026-08-18T12:00:00Z') for i in range(5)]
        calls = {'n': 0}

        def fake_query(extra_q='', page_size=40, page_token=None):
            calls['n'] += 1
            return {'files': first[:page_size], 'nextPageToken': None}

        def fake_list(q, fields, page_size=1000, page_token=None, order_by=None):
            # oldest video call
            return {'files': [_file('old', 'old.mp4', '2026-06-02T12:00:00Z')]}

        with patch.object(storage, '_ensure_root'), \
             patch.object(storage, '_query_videos', side_effect=fake_query), \
             patch.object(storage, '_drive_list', side_effect=fake_list):
            result = storage.list_videos(summary=True, limit=40)

        months = [m['month'] for m in result['months']]
        self.assertEqual(months, ['2026-08', '2026-07', '2026-06'])
        self.assertEqual(result['months'][0]['count'], 5)

    def test_summary_refresh_picks_up_new_files(self):
        storage = _blank_drive()
        pages = {
            1: [_file('a', 'a.mp4', '2026-08-18T12:00:00Z')],
            2: [
                _file('b', 'new.mp4', '2026-08-19T12:00:00Z'),
                _file('a', 'a.mp4', '2026-08-18T12:00:00Z'),
            ],
        }
        round_id = {'n': 1}

        def fake_query(extra_q='', page_size=40, page_token=None):
            return {'files': pages[round_id['n']], 'nextPageToken': None}

        with patch.object(storage, '_ensure_root'), \
             patch.object(storage, '_query_videos', side_effect=fake_query), \
             patch.object(storage, '_discover_month_keys', return_value=['2026-08']):
            first = storage.list_videos(summary=True)
            self.assertEqual([v['name'] for v in first['videos']], ['a.mp4'])
            round_id['n'] = 2
            # Cached summary must not re-query Drive; new files wait for Refresh
            soft = storage.list_videos(summary=True)
            self.assertEqual([v['name'] for v in soft['videos']], ['a.mp4'])
            # Explicit refresh still picks up new files
            third = storage.list_videos(summary=True, refresh=True)
            self.assertEqual([v['name'] for v in third['videos']], ['new.mp4', 'a.mp4'])


if __name__ == '__main__':
    unittest.main()
