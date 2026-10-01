#!/usr/bin/env python3
"""Local library listing and thumbs must not block on Google Drive."""

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import video_server  # noqa: E402


class StaleWhileRevalidateTests(unittest.TestCase):
    def setUp(self):
        self.prev_mode = video_server.STORAGE_MODE
        video_server.STORAGE_MODE = 'local'
        video_server.invalidate_video_cache()
        video_server.invalidate_photo_cache()

    def tearDown(self):
        video_server.STORAGE_MODE = self.prev_mode
        video_server.invalidate_video_cache()
        video_server.invalidate_photo_cache()

    def test_expired_memory_cache_is_returned_without_scanning(self):
        items = [{'name': 'a.mp4', 'path': 'a.mp4', 'modified': 1.0, 'size': 1}]
        video_server._video_cache['data'] = items
        video_server._video_cache['time'] = time.time() - video_server.VIDEO_CACHE_TTL - 10
        with mock.patch.object(video_server, 'scan_videos', side_effect=AssertionError('scan blocked')):
            with mock.patch.object(video_server, '_schedule_media_refresh') as sched:
                got = video_server.get_videos_cached()
        self.assertIs(got, items)
        sched.assert_not_called()

    def test_refresh_flag_does_not_block_either(self):
        items = [{'name': 'a.mp4', 'path': 'a.mp4', 'modified': 1.0, 'size': 1}]
        video_server._video_cache['data'] = items
        video_server._video_cache['time'] = time.time()
        with mock.patch.object(video_server, 'scan_videos', side_effect=AssertionError('scan blocked')):
            with mock.patch.object(video_server, '_schedule_media_refresh') as sched:
                got = video_server.get_videos_cached(force=True)
        self.assertIs(got, items)
        sched.assert_called_once_with('videos')

    def test_partial_scan_does_not_replace_a_larger_index(self):
        big = [{'name': f'{i}.mp4', 'path': f'{i}.mp4', 'modified': 1.0, 'size': 1} for i in range(100)]
        video_server._video_cache['data'] = big
        video_server._video_cache['time'] = time.time()
        kept = video_server._commit_scan('videos', big[:10])
        self.assertEqual(len(kept), 100)


class BasenameMapTests(unittest.TestCase):
    def test_two_missing_files_do_not_rglob_twice(self):
        with tempfile.TemporaryDirectory() as tmp:
            media_root = Path(tmp)
            video_dir = media_root / 'ComfyUI' / 'output' / 'video'
            video_dir.mkdir(parents=True)
            (video_dir / 'keep.mp4').write_bytes(b'x')
            old_cwd = os.getcwd()
            old_dir = video_server.VIDEO_DIR
            try:
                os.chdir(media_root)
                video_server.VIDEO_DIR = 'ComfyUI/output/video'
                video_server._invalidate_basename_map()
                first = video_server._video_basename_map()
                second = video_server._video_basename_map()
                self.assertIs(first, second)
                self.assertIn('keep.mp4', first)
                self.assertIsNone(video_server.resolve_media_path('nope/missing-a.mp4'))
                self.assertIsNone(video_server.resolve_media_path('nope/missing-b.mp4'))
            finally:
                os.chdir(old_cwd)
                video_server.VIDEO_DIR = old_dir
                video_server._invalidate_basename_map()


class DiskThumbTests(unittest.TestCase):
    def test_serves_cache_without_the_original_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / 'thumbs'
            cache.mkdir()
            prev = video_server.THUMB_CACHE_DIR
            prev_legacy = video_server.LEGACY_THUMB_CACHE_DIR
            video_server.THUMB_CACHE_DIR = cache
            video_server.LEGACY_THUMB_CACHE_DIR = Path(tmp) / 'legacy'
            video_server._thumb_memory.clear()
            try:
                key = 'abc123'
                payload = b'RIFF' + b'\x00' * 8 + b'WEBP' + b'\x00' * 80
                # Real webp magic: RIFF....WEBP at 0 and 8
                payload = b'RIFF' + (100).to_bytes(4, 'little') + b'WEBP' + b'\x00' * 90
                (cache / (key + '.webp')).write_bytes(payload)
                found = video_server.get_disk_thumb(key)
                self.assertIsNotNone(found)
                self.assertEqual(found[1], 'image/webp')
            finally:
                video_server.THUMB_CACHE_DIR = prev
                video_server.LEGACY_THUMB_CACHE_DIR = prev_legacy
                video_server._thumb_memory.clear()


class FrontendPerfTests(unittest.TestCase):
    def test_video_grid_does_not_unload_server_thumbs(self):
        src = Path(ROOT, 'index.html').read_text()
        self.assertIn('if (useServerVthumb()) return;', src)
        self.assertIn('THUMB_CONCURRENCY', src)
        self.assertIn(': 12)', src)
        self.assertIn('queueThumbLoad(img)', src)

    def test_photos_do_not_observe_unload(self):
        src = Path(ROOT, 'photos.html').read_text()
        self.assertNotIn('thumbUnloadObserver.observe(img)', src)
        self.assertIn(': 12)', src)


if __name__ == '__main__':
    unittest.main()
