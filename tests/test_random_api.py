#!/usr/bin/env python3
"""Server-side random media pick."""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import video_server  # noqa: E402


class PickRandomItemTests(unittest.TestCase):
    def test_empty(self):
        self.assertIsNone(video_server.pick_random_item([]))

    def test_excludes_path_when_possible(self):
        items = [
            {'path': 'a.mp4', 'name': 'a.mp4'},
            {'path': 'b.mp4', 'name': 'b.mp4'},
        ]
        with mock.patch('video_server.random.choice', side_effect=lambda pool: pool[0]) as choice:
            picked = video_server.pick_random_item(items, exclude_path='a.mp4')
            self.assertEqual(picked['path'], 'b.mp4')
            choice.assert_called_once()
            self.assertEqual(choice.call_args[0][0], [{'path': 'b.mp4', 'name': 'b.mp4'}])

    def test_falls_back_when_only_excluded(self):
        items = [{'path': 'only.mp4', 'name': 'only.mp4'}]
        picked = video_server.pick_random_item(items, exclude_path='only.mp4')
        self.assertEqual(picked['path'], 'only.mp4')


class RandomVideoApiTests(unittest.TestCase):
    def test_get_random_video_local(self):
        with tempfile.TemporaryDirectory() as tmp:
            media_root = Path(tmp)
            video_dir = media_root / 'ComfyUI' / 'output' / 'video'
            video_dir.mkdir(parents=True)
            (video_dir / 'one.mp4').write_bytes(b'a')
            (video_dir / 'two.mp4').write_bytes(b'b')

            old_cwd = os.getcwd()
            old_mode = video_server.STORAGE_MODE
            old_video_dir = video_server.VIDEO_DIR
            old_cache = dict(video_server._video_cache)
            try:
                os.chdir(media_root)
                video_server.STORAGE_MODE = 'local'
                video_server.VIDEO_DIR = 'ComfyUI/output/video'
                video_server.invalidate_video_cache()
                result = video_server.get_random_video()
                self.assertIsNotNone(result['video'])
                self.assertEqual(result['total'], 2)
                self.assertIn(result['video']['name'], ('one.mp4', 'two.mp4'))

                other = video_server.get_random_video(exclude_path=result['video']['path'])
                self.assertIsNotNone(other['video'])
                if result['total'] > 1:
                    self.assertNotEqual(other['video']['path'], result['video']['path'])
            finally:
                os.chdir(old_cwd)
                video_server.STORAGE_MODE = old_mode
                video_server.VIDEO_DIR = old_video_dir
                video_server._video_cache.update(old_cache)


if __name__ == '__main__':
    unittest.main()
