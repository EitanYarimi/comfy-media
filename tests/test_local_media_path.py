#!/usr/bin/env python3
"""Local path resolution should tolerate stale index paths."""

import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import video_server  # noqa: E402


class LocalMediaPathTests(unittest.TestCase):
    def test_resolve_by_basename_when_index_path_stale(self):
        with tempfile.TemporaryDirectory() as tmp:
            media_root = Path(tmp)
            video_dir = media_root / 'ComfyUI' / 'output' / 'video'
            video_dir.mkdir(parents=True)
            clip = video_dir / 'LTX_test.mp4'
            clip.write_bytes(b'fake')

            old_cwd = os.getcwd()
            old_video_dir = video_server.VIDEO_DIR
            try:
                os.chdir(media_root)
                video_server.VIDEO_DIR = 'ComfyUI/output/video'
                # Stale/wrong prefix in index — file still found by basename.
                found = video_server.resolve_media_path('wrong/prefix/LTX_test.mp4')
                self.assertIsNotNone(found)
                self.assertEqual(found.name, 'LTX_test.mp4')
            finally:
                os.chdir(old_cwd)
                video_server.VIDEO_DIR = old_video_dir

    def test_filter_local_items_drops_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            media_root = Path(tmp)
            video_dir = media_root / 'ComfyUI' / 'output' / 'video'
            video_dir.mkdir(parents=True)
            (video_dir / 'exists.mp4').write_bytes(b'x')

            old_cwd = os.getcwd()
            old_mode = video_server.STORAGE_MODE
            old_video_dir = video_server.VIDEO_DIR
            try:
                os.chdir(media_root)
                video_server.STORAGE_MODE = 'local'
                video_server.VIDEO_DIR = 'ComfyUI/output/video'
                items = [
                    {'path': 'ComfyUI/output/video/exists.mp4', 'name': 'exists.mp4'},
                    {'path': 'ComfyUI/output/video/gone.mp4', 'name': 'gone.mp4'},
                ]
                kept = video_server._filter_local_items(items)
                self.assertEqual(len(kept), 1)
                self.assertEqual(kept[0]['name'], 'exists.mp4')
            finally:
                os.chdir(old_cwd)
                video_server.STORAGE_MODE = old_mode
                video_server.VIDEO_DIR = old_video_dir


if __name__ == '__main__':
    unittest.main()
