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

        with patch.object(storage, '_query_videos', side_effect=fake_query):
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

        with patch.object(storage, '_query_videos', side_effect=fake_query):
            storage.list_videos(summary=True, limit=40)
            later = storage.list_videos(offset=40, limit=40, summary=False)

        self.assertEqual(len(later['videos']), 10)
        self.assertFalse(later['hasMore'])


if __name__ == '__main__':
    unittest.main()
