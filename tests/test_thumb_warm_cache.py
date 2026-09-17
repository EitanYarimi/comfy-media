#!/usr/bin/env python3
"""Thumbnail negative cache, immutable headers, and priority warm queue."""

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


class ImmutableThumbHeaderTests(unittest.TestCase):
    def test_thumbs_are_cached_for_a_long_time(self):
        # Thumb URLs are versioned with ?v=&m=<mtime>, so revalidating every
        # 5 minutes just re-downloaded the whole grid on each filter change.
        self.assertIn('immutable', video_server.THUMB_CACHE_HEADER)
        self.assertNotIn('must-revalidate', video_server.THUMB_CACHE_HEADER)

    def test_no_stale_revalidating_thumb_headers_remain(self):
        source = Path(ROOT, 'video_server.py').read_text()
        self.assertNotIn("'public, max-age=300, must-revalidate'", source)


class ThumbFailureCacheTests(unittest.TestCase):
    def setUp(self):
        video_server._thumb_failures.clear()
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'broken.mp4'
        self.path.write_bytes(b'\x00' * 32)

    def tearDown(self):
        video_server._thumb_failures.clear()
        self.tmp.cleanup()

    def test_failure_is_remembered(self):
        key = video_server._video_cache_key(self.path)
        self.assertFalse(video_server._thumb_failed_recently(key, self.path))
        video_server._remember_thumb_failure(key, self.path)
        self.assertTrue(video_server._thumb_failed_recently(key, self.path))

    def test_failure_expires(self):
        key = video_server._video_cache_key(self.path)
        video_server._remember_thumb_failure(key, self.path)
        stale = time.time() - (video_server.THUMB_FAILURE_TTL + 1)
        video_server._thumb_failures[video_server._thumb_failure_key(key, self.path)] = stale
        self.assertFalse(video_server._thumb_failed_recently(key, self.path))

    def test_rewriting_the_file_clears_the_failure(self):
        key = video_server._video_cache_key(self.path)
        video_server._remember_thumb_failure(key, self.path)
        os.utime(self.path, (time.time() + 10, time.time() + 10))
        self.assertFalse(video_server._thumb_failed_recently(key, self.path))

    def test_generate_skips_ffmpeg_after_a_failure(self):
        with mock.patch.object(video_server, '_thumb_from_ffmpeg', return_value=None) as ffmpeg, \
                mock.patch.object(video_server, '_thumb_from_qlmanage', return_value=None) as ql:
            self.assertIsNone(video_server.generate_video_thumbnail(self.path))
            self.assertEqual(ffmpeg.call_count, 1)
            # Second render of the same grid must not re-run the seek search.
            self.assertIsNone(video_server.generate_video_thumbnail(self.path))
            self.assertEqual(ffmpeg.call_count, 1)
            self.assertEqual(ql.call_count, 1)


class WarmQueueTests(unittest.TestCase):
    def setUp(self):
        video_server._warm_queue.clear()
        video_server._warm_seen.clear()

    def tearDown(self):
        video_server._warm_queue.clear()
        video_server._warm_seen.clear()

    def test_queue_preserves_page_order(self):
        video_server.queue_warm_paths(['a.mp4', 'b.mp4', 'c.mp4'])
        drained = [video_server._next_warm_path() for _ in range(3)]
        self.assertEqual(drained, ['a.mp4', 'b.mp4', 'c.mp4'])
        self.assertIsNone(video_server._next_warm_path())

    def test_reset_drops_the_previous_view(self):
        video_server.queue_warm_paths(['old1.mp4', 'old2.mp4'])
        video_server.queue_warm_paths(['new1.mp4', 'new2.mp4'], reset=True)
        self.assertEqual(video_server._next_warm_path(), 'new1.mp4')
        self.assertEqual(video_server._next_warm_path(), 'new2.mp4')
        self.assertIsNone(video_server._next_warm_path())

    def test_later_pages_append_in_scroll_order(self):
        video_server.queue_warm_paths(['page1a.mp4', 'page1b.mp4'], reset=True)
        video_server.queue_warm_paths(['page2a.mp4'])
        drained = [video_server._next_warm_path() for _ in range(3)]
        self.assertEqual(drained, ['page1a.mp4', 'page1b.mp4', 'page2a.mp4'])

    def test_duplicates_are_ignored_while_queued(self):
        self.assertEqual(video_server.queue_warm_paths(['a.mp4', 'b.mp4']), 2)
        self.assertEqual(video_server.queue_warm_paths(['a.mp4']), 0)
        self.assertEqual(len(video_server._warm_queue), 2)

    def test_queue_is_bounded(self):
        video_server.queue_warm_paths([f'v{i}.mp4' for i in range(video_server.WARM_QUEUE_LIMIT + 50)])
        self.assertLessEqual(len(video_server._warm_queue), video_server.WARM_QUEUE_LIMIT)
        self.assertEqual(len(video_server._warm_seen), len(video_server._warm_queue))

    def test_drain_skips_missing_files(self):
        video_server.queue_warm_paths(['does/not/exist.mp4'])
        with mock.patch.object(video_server, 'generate_video_thumbnail') as gen:
            video_server._drain_warm_queue()
            gen.assert_not_called()
        self.assertEqual(len(video_server._warm_queue), 0)


if __name__ == '__main__':
    unittest.main()
