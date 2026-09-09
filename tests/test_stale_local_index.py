#!/usr/bin/env python3
"""Stale/wrong-root local indexes must not stick on an empty library."""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import video_server  # noqa: E402


class StaleLocalIndexTests(unittest.TestCase):
    def test_rescan_when_filtered_index_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            media_root = Path(tmp)
            video_dir = media_root / 'ComfyUI' / 'output' / 'video'
            video_dir.mkdir(parents=True)
            (video_dir / 'real.mp4').write_bytes(b'x')

            cache = Path(tmp) / 'cache' / 'thumbs'
            cache.mkdir(parents=True)
            index = cache / 'videos_index_v1.json'
            # Index claims files that do not exist under this MEDIA_ROOT.
            index.write_text(json.dumps({
                'items': [{
                    'name': 'gone.mp4',
                    'path': 'ComfyUI/output/video/gone.mp4',
                    'size': 1,
                    'modified': 1.0,
                }],
                'months': [],
                'saved': 1.0,
                'media_root': str(media_root.resolve()),
            }))

            old_cwd = os.getcwd()
            old_mode = video_server.STORAGE_MODE
            old_video_dir = video_server.VIDEO_DIR
            old_index = video_server.VIDEO_INDEX_PATH
            old_legacy = video_server.LEGACY_VIDEO_INDEX_PATH
            try:
                os.chdir(media_root)
                video_server.STORAGE_MODE = 'local'
                video_server.VIDEO_DIR = 'ComfyUI/output/video'
                video_server.VIDEO_INDEX_PATH = index
                video_server.LEGACY_VIDEO_INDEX_PATH = cache / 'missing_legacy.json'
                video_server.invalidate_video_cache()

                videos = video_server.get_videos_cached()
                self.assertEqual(len(videos), 1)
                self.assertEqual(videos[0]['name'], 'real.mp4')
            finally:
                os.chdir(old_cwd)
                video_server.STORAGE_MODE = old_mode
                video_server.VIDEO_DIR = old_video_dir
                video_server.VIDEO_INDEX_PATH = old_index
                video_server.LEGACY_VIDEO_INDEX_PATH = old_legacy
                video_server.invalidate_video_cache()

    def test_ignore_index_from_other_media_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            media_root = Path(tmp) / 'mine'
            other_root = Path(tmp) / 'other'
            video_dir = media_root / 'ComfyUI' / 'output' / 'video'
            video_dir.mkdir(parents=True)
            (video_dir / 'mine.mp4').write_bytes(b'x')
            other_root.mkdir()

            cache = Path(tmp) / 'cache' / 'thumbs'
            cache.mkdir(parents=True)
            index = cache / 'videos_index_v1.json'
            index.write_text(json.dumps({
                'items': [{
                    'name': 'other.mp4',
                    'path': 'ComfyUI/output/video/other.mp4',
                    'size': 1,
                    'modified': 1.0,
                }],
                'months': [],
                'saved': 1.0,
                'media_root': str(other_root.resolve()),
            }))

            old_cwd = os.getcwd()
            old_mode = video_server.STORAGE_MODE
            old_video_dir = video_server.VIDEO_DIR
            old_index = video_server.VIDEO_INDEX_PATH
            old_legacy = video_server.LEGACY_VIDEO_INDEX_PATH
            try:
                os.chdir(media_root)
                video_server.STORAGE_MODE = 'local'
                video_server.VIDEO_DIR = 'ComfyUI/output/video'
                video_server.VIDEO_INDEX_PATH = index
                video_server.LEGACY_VIDEO_INDEX_PATH = cache / 'missing_legacy.json'
                video_server.invalidate_video_cache()

                with mock.patch('builtins.print'):
                    videos = video_server.get_videos_cached()
                self.assertEqual(len(videos), 1)
                self.assertEqual(videos[0]['name'], 'mine.mp4')
            finally:
                os.chdir(old_cwd)
                video_server.STORAGE_MODE = old_mode
                video_server.VIDEO_DIR = old_video_dir
                video_server.VIDEO_INDEX_PATH = old_index
                video_server.LEGACY_VIDEO_INDEX_PATH = old_legacy
                video_server.invalidate_video_cache()


if __name__ == '__main__':
    unittest.main()
