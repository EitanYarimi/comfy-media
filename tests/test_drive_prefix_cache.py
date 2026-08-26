#!/usr/bin/env python3
"""Drive prefix cache should serve the start of a video without another Drive fetch."""

import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import video_server  # noqa: E402


class DrivePrefixCacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='comfy-prefix-')
        self.addCleanup(self.tmp.cleanup)
        self.cache_dir = Path(self.tmp.name) / 'drive_prefix'
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._old = {
            'dir': video_server.DRIVE_PREFIX_CACHE_DIR,
            'bytes': video_server.DRIVE_PREFIX_BYTES,
            'max': video_server.DRIVE_PREFIX_CACHE_MAX_BYTES,
            'warming': set(video_server._drive_prefix_warming),
        }
        video_server.DRIVE_PREFIX_CACHE_DIR = self.cache_dir
        video_server.DRIVE_PREFIX_BYTES = 1024
        video_server.DRIVE_PREFIX_CACHE_MAX_BYTES = 4096
        video_server._drive_prefix_warming = set()

    def tearDown(self):
        video_server.DRIVE_PREFIX_CACHE_DIR = self._old['dir']
        video_server.DRIVE_PREFIX_BYTES = self._old['bytes']
        video_server.DRIVE_PREFIX_CACHE_MAX_BYTES = self._old['max']
        video_server._drive_prefix_warming = self._old['warming']

    def test_save_and_get_prefix(self):
        data = b'a' * 800
        video_server.save_drive_prefix_cache('file1', 5000, data)
        found = video_server.get_drive_prefix_cache('file1', 5000)
        self.assertIsNotNone(found)
        path, length = found
        self.assertEqual(length, 800)
        self.assertEqual(path.read_bytes(), data)

    def test_size_mismatch_invalidates(self):
        video_server.save_drive_prefix_cache('file1', 5000, b'hello')
        self.assertIsNone(video_server.get_drive_prefix_cache('file1', 9999))

    def test_prune_keeps_budget(self):
        video_server.save_drive_prefix_cache('a', 100, b'x' * 1500)
        video_server.save_drive_prefix_cache('b', 100, b'y' * 1500)
        video_server.save_drive_prefix_cache('c', 100, b'z' * 1500)
        total = sum(p.stat().st_size for p in self.cache_dir.glob('*.bin'))
        self.assertLessEqual(total, video_server.DRIVE_PREFIX_CACHE_MAX_BYTES)

    def test_warm_downloads_prefix_once(self):
        payload = b'0123456789' * 200  # 2000 bytes
        calls = {'n': 0}

        class FakeResp:
            status_code = 206

            def iter_content(self, size=256 * 1024):
                yield payload

            def close(self):
                pass

        class FakeDrive:
            def open_media(self, file_id, range_header=None, timeout=120):
                calls['n'] += 1
                self.last_range = range_header
                return FakeResp()

        fake = FakeDrive()
        with patch.object(video_server, 'get_drive_storage', return_value=fake):
            ok = video_server.warm_drive_prefix_cache('vid', len(payload) + 100)
            ok2 = video_server.warm_drive_prefix_cache('vid', len(payload) + 100)

        self.assertTrue(ok)
        self.assertTrue(ok2)
        self.assertEqual(calls['n'], 1)
        self.assertEqual(fake.last_range, 'bytes=0-1023')
        found = video_server.get_drive_prefix_cache('vid', len(payload) + 100)
        self.assertEqual(found[1], 1024)


class ParseRangeTests(unittest.TestCase):
    def test_parse_range(self):
        self.assertEqual(video_server.parse_range_header('bytes=0-99', 1000), (0, 99))
        self.assertEqual(video_server.parse_range_header('bytes=100-', 1000), (100, 999))
        self.assertEqual(video_server.parse_range_header('bytes=-50', 1000), (950, 999))
        self.assertEqual(video_server.parse_range_header('bytes=5000-6000', 1000), 'unsatisfiable')


if __name__ == '__main__':
    unittest.main()
