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

    def test_resolve_media_root_auto_my_drive(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / 'comfy-media'
            repo.mkdir()
            (repo / 'video_server.py').write_text('# app')
            videos = root / 'ComfyUI' / 'output' / 'video'
            videos.mkdir(parents=True)
            (videos / 'clip.mp4').write_bytes(b'x')

            old_env = os.environ.pop('MEDIA_ROOT', None)
            old_file = video_server.__file__
            try:
                video_server.__file__ = str(repo / 'video_server.py')
                resolved = video_server.resolve_media_root()
                self.assertEqual(resolved, root.resolve())
            finally:
                if old_env is not None:
                    os.environ['MEDIA_ROOT'] = old_env
                video_server.__file__ = old_file

    def test_delete_many_removes_only_chosen_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            out = root / 'ComfyUI' / 'output' / 'video'
            out.mkdir(parents=True)
            (out / 'a.mp4').write_bytes(b'a')
            (out / 'b.mp4').write_bytes(b'b')
            (out / 'c.mp4').write_bytes(b'c')
            thumbs = root / 'thumbs'
            thumbs.mkdir()
            old = (
                os.getcwd(),
                video_server.STORAGE_MODE,
                video_server.THUMB_CACHE_DIR,
                video_server.LEGACY_THUMB_CACHE_DIR,
                video_server.VIDEO_INDEX_PATH,
                video_server.PHOTO_INDEX_PATH,
                video_server.LEGACY_VIDEO_INDEX_PATH,
                video_server.LEGACY_PHOTO_INDEX_PATH,
                video_server._video_cache.get('data'),
            )
            try:
                os.chdir(root)
                video_server.STORAGE_MODE = 'local'
                video_server.THUMB_CACHE_DIR = thumbs
                video_server.LEGACY_THUMB_CACHE_DIR = thumbs
                video_server.VIDEO_INDEX_PATH = thumbs / 'videos_index_v1.json'
                video_server.PHOTO_INDEX_PATH = thumbs / 'photos_index_v1.json'
                video_server.LEGACY_VIDEO_INDEX_PATH = thumbs / 'missing-v.json'
                video_server.LEGACY_PHOTO_INDEX_PATH = thumbs / 'missing-p.json'
                now = 1_700_000_000
                video_server._video_cache['data'] = [
                    {'name': 'a.mp4', 'path': 'ComfyUI/output/video/a.mp4', 'modified': now},
                    {'name': 'b.mp4', 'path': 'ComfyUI/output/video/b.mp4', 'modified': now - 1},
                    {'name': 'c.mp4', 'path': 'ComfyUI/output/video/c.mp4', 'modified': now - 2},
                ]
                deleted, failed = video_server.delete_local_media_many([
                    'ComfyUI/output/video/a.mp4',
                    'ComfyUI/output/video/b.mp4',
                    'ComfyUI/output/video/missing.mp4',
                    '../nope.mp4',
                ])
                self.assertEqual(deleted, [
                    'ComfyUI/output/video/a.mp4',
                    'ComfyUI/output/video/b.mp4',
                ])
                self.assertFalse((out / 'a.mp4').exists())
                self.assertFalse((out / 'b.mp4').exists())
                self.assertTrue((out / 'c.mp4').exists())
                self.assertTrue(any(item['error'] == 'Bad path' for item in failed))
                names = [item['name'] for item in video_server._video_cache['data']]
                self.assertEqual(names, ['c.mp4'])
            finally:
                os.chdir(old[0])
                video_server.STORAGE_MODE = old[1]
                video_server.THUMB_CACHE_DIR = old[2]
                video_server.LEGACY_THUMB_CACHE_DIR = old[3]
                video_server.VIDEO_INDEX_PATH = old[4]
                video_server.PHOTO_INDEX_PATH = old[5]
                video_server.LEGACY_VIDEO_INDEX_PATH = old[6]
                video_server.LEGACY_PHOTO_INDEX_PATH = old[7]
                video_server._video_cache['data'] = old[8]


if __name__ == '__main__':
    unittest.main()
