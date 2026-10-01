#!/usr/bin/env python3
"""Random restricted to horizontal or vertical videos."""

import io
import json
import os
import random
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import drive_backend  # noqa: E402
import video_server  # noqa: E402


def webp_bytes(width, height):
    """High contrast on purpose: the thumbnail cache throws away flat images."""
    from PIL import Image, ImageDraw
    img = Image.new('RGB', (width, height), (0, 0, 0))
    draw = ImageDraw.Draw(img)
    rng = random.Random(11)
    step = 8
    for y in range(0, height, step):
        for x in range(0, width, step):
            shade = 255 if (x // step + y // step) % 2 else rng.randrange(256)
            draw.rectangle([x, y, x + step, y + step], fill=(shade, shade, shade))
    buf = io.BytesIO()
    img.save(buf, 'WEBP', quality=90)
    return buf.getvalue()


class OrientationFromSizeTests(unittest.TestCase):
    def test_wider_than_tall_is_horizontal(self):
        self.assertEqual(video_server.orientation_from_size(1920, 1080), 'horizontal')

    def test_taller_than_wide_is_vertical(self):
        self.assertEqual(video_server.orientation_from_size(1080, 1920), 'vertical')

    def test_square_counts_as_horizontal(self):
        self.assertEqual(video_server.orientation_from_size(720, 720), 'horizontal')

    def test_missing_or_bogus_size_is_unknown(self):
        for width, height in ((0, 100), (100, 0), (None, None), ('x', 'y')):
            self.assertIsNone(video_server.orientation_from_size(width, height))


class NormalizeOrientationTests(unittest.TestCase):
    def test_accepts_aliases(self):
        for value in ('horizontal', 'Landscape', 'WIDE', 'h'):
            self.assertEqual(video_server.normalize_orientation(value), 'horizontal')
        for value in ('vertical', 'portrait', 'Tall', 'v'):
            self.assertEqual(video_server.normalize_orientation(value), 'vertical')

    def test_rejects_anything_else(self):
        for value in (None, '', 'diagonal', 'both'):
            self.assertIsNone(video_server.normalize_orientation(value))


class OrientationCacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.prev_cache = video_server.THUMB_CACHE_DIR
        self.prev_legacy = video_server.LEGACY_THUMB_CACHE_DIR
        video_server.THUMB_CACHE_DIR = self.tmp / 'thumbs'
        video_server.LEGACY_THUMB_CACHE_DIR = self.tmp / 'legacy'
        video_server.THUMB_CACHE_DIR.mkdir(parents=True)
        video_server._orientation_cache.clear()

    def tearDown(self):
        video_server.THUMB_CACHE_DIR = self.prev_cache
        video_server.LEGACY_THUMB_CACHE_DIR = self.prev_legacy
        video_server._orientation_cache.clear()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_sidecar_update_keeps_duration(self):
        video_server._update_thumb_meta('abc', {'duration': 12.5})
        video_server._update_thumb_meta('abc', {'orientation': 'vertical'})
        meta = json.loads((video_server.THUMB_CACHE_DIR / 'abc.meta.json').read_text())
        self.assertEqual(meta['duration'], 12.5)
        self.assertEqual(meta['orientation'], 'vertical')

    def test_thumbnail_generation_records_orientation(self):
        video_server._record_thumb_orientation('key1', webp_bytes(160, 320))
        self.assertEqual(video_server._read_thumb_meta('key1').get('orientation'), 'vertical')

    def test_recording_does_not_overwrite_known_orientation(self):
        video_server._update_thumb_meta('key2', {'orientation': 'horizontal'})
        video_server._record_thumb_orientation('key2', webp_bytes(160, 320))
        self.assertEqual(video_server._read_thumb_meta('key2').get('orientation'), 'horizontal')


class VideoOrientationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.prev_cache = video_server.THUMB_CACHE_DIR
        self.prev_legacy = video_server.LEGACY_THUMB_CACHE_DIR
        self.prev_mode = video_server.STORAGE_MODE
        video_server.THUMB_CACHE_DIR = self.tmp / 'thumbs'
        video_server.LEGACY_THUMB_CACHE_DIR = self.tmp / 'legacy'
        video_server.THUMB_CACHE_DIR.mkdir(parents=True)
        video_server._orientation_cache.clear()
        video_server._thumb_memory.clear()
        # Media paths are resolved relative to the working directory.
        self.prev_cwd = os.getcwd()
        os.chdir(self.tmp)
        self.rel = 'clip.mp4'
        self.media = self.tmp / self.rel
        self.media.write_bytes(b'not really a video')

    def tearDown(self):
        os.chdir(self.prev_cwd)
        video_server.THUMB_CACHE_DIR = self.prev_cache
        video_server.LEGACY_THUMB_CACHE_DIR = self.prev_legacy
        video_server.STORAGE_MODE = self.prev_mode
        video_server._orientation_cache.clear()
        video_server._thumb_memory.clear()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_item_dimensions_win_without_touching_disk(self):
        item = {'path': 'nowhere/missing.mp4', 'width': 1080, 'height': 1920}
        self.assertEqual(video_server.video_orientation(item), 'vertical')

    def test_cached_thumbnail_answers_without_ffprobe(self):
        cache_key = video_server._video_cache_key(self.media)
        (video_server.THUMB_CACHE_DIR / (cache_key + '.webp')).write_bytes(webp_bytes(400, 224))
        item = {'path': self.rel}
        self.assertEqual(video_server.video_orientation(item, allow_probe=False), 'horizontal')
        # And it is written back so the next pick skips the decode entirely.
        self.assertEqual(
            video_server._read_thumb_meta(cache_key).get('orientation'), 'horizontal'
        )

    def test_sidecar_answers_before_the_thumbnail(self):
        cache_key = video_server._video_cache_key(self.media)
        video_server._update_thumb_meta(cache_key, {'orientation': 'vertical'})
        self.assertEqual(video_server.video_orientation({'path': self.rel}), 'vertical')

    def test_unknown_without_probe_permission(self):
        self.assertIsNone(video_server.video_orientation({'path': self.rel}, allow_probe=False))

    def test_drive_mode_never_probes_local_disk(self):
        video_server.STORAGE_MODE = 'drive'
        self.assertIsNone(video_server.video_orientation({'path': self.rel}, allow_probe=True))


class PickOrientedTests(unittest.TestCase):
    def setUp(self):
        self.items = [
            {'path': 'a.mp4', 'width': 1920, 'height': 1080},
            {'path': 'b.mp4', 'width': 1080, 'height': 1920},
            {'path': 'c.mp4', 'width': 1280, 'height': 720},
        ]

    def test_picks_only_matching_orientation(self):
        for _ in range(20):
            picked, matched = video_server.pick_random_oriented_video(self.items, 'vertical')
            self.assertTrue(matched)
            self.assertEqual(picked['path'], 'b.mp4')

    def test_honours_exclude_when_alternatives_exist(self):
        picked, _ = video_server.pick_random_oriented_video(
            self.items, 'horizontal', exclude_path='a.mp4'
        )
        self.assertEqual(picked['path'], 'c.mp4')

    def test_returns_nothing_when_no_clip_matches(self):
        only_wide = [self.items[0], self.items[2]]
        picked, matched = video_server.pick_random_oriented_video(only_wide, 'vertical')
        self.assertIsNone(picked)
        self.assertFalse(matched)

    def test_no_orientation_falls_back_to_plain_random(self):
        picked, matched = video_server.pick_random_oriented_video(self.items, None)
        self.assertIn(picked['path'], {'a.mp4', 'b.mp4', 'c.mp4'})
        self.assertFalse(matched)

    def test_probing_is_bounded(self):
        unknown = [{'path': f'u{i}.mp4'} for i in range(200)]
        calls = []
        real = video_server.video_orientation

        def counting(item, allow_probe=False):
            if allow_probe:
                calls.append(item['path'])
            return real(item, allow_probe=allow_probe)

        video_server.video_orientation = counting
        try:
            picked, matched = video_server.pick_random_oriented_video(unknown, 'vertical')
        finally:
            video_server.video_orientation = real
        self.assertIsNone(picked)
        self.assertFalse(matched)
        self.assertLessEqual(len(calls), video_server.ORIENTATION_PROBE_BUDGET)


class RandomEndpointTests(unittest.TestCase):
    def setUp(self):
        self.prev_mode = video_server.STORAGE_MODE
        video_server.STORAGE_MODE = 'local'
        self.items = [
            {'path': 'a.mp4', 'name': 'a.mp4', 'modified': 1.0, 'size': 1,
             'width': 1920, 'height': 1080},
            {'path': 'b.mp4', 'name': 'b.mp4', 'modified': 2.0, 'size': 1,
             'width': 1080, 'height': 1920},
        ]
        self.prev_get = video_server.get_videos_cached
        video_server.get_videos_cached = lambda *a, **k: self.items

    def tearDown(self):
        video_server.STORAGE_MODE = self.prev_mode
        video_server.get_videos_cached = self.prev_get

    def test_horizontal_request_returns_horizontal_clip(self):
        result = video_server.get_random_video(orientation='horizontal')
        self.assertEqual(result['video']['path'], 'a.mp4')
        self.assertEqual(result['orientation'], 'horizontal')
        self.assertTrue(result['orientationMatched'])

    def test_vertical_request_returns_vertical_clip(self):
        result = video_server.get_random_video(orientation='vertical')
        self.assertEqual(result['video']['path'], 'b.mp4')

    def test_empty_orientation_behaves_like_before(self):
        result = video_server.get_random_video()
        self.assertIsNotNone(result['video'])
        self.assertIsNone(result['orientation'])

    def test_explains_itself_when_the_view_has_no_match(self):
        self.items = [self.items[0]]
        result = video_server.get_random_video(orientation='vertical')
        self.assertIsNone(result['video'])
        self.assertIn('vertical', result['error'])


class DriveDimensionTests(unittest.TestCase):
    def test_video_metadata_is_requested_and_kept(self):
        self.assertIn('videoMediaMetadata', drive_backend.VIDEO_LIST_FIELDS)
        self.assertIn('imageMediaMetadata', drive_backend.VIDEO_LIST_FIELDS)
        self.assertIn('width', drive_backend.INDEX_ITEM_KEYS)
        self.assertIn('height', drive_backend.INDEX_ITEM_KEYS)

    def test_dimensions_read_from_either_metadata_block(self):
        self.assertEqual(
            drive_backend._media_dimensions({'videoMediaMetadata': {'width': 1080, 'height': 1920}}),
            (1080, 1920),
        )
        self.assertEqual(
            drive_backend._media_dimensions({'imageMediaMetadata': {'width': 4, 'height': 3}}),
            (4, 3),
        )
        self.assertEqual(drive_backend._media_dimensions({}), (None, None))


class FrontendTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = Path(ROOT, 'index.html').read_text()

    def test_both_orientation_buttons_exist(self):
        self.assertIn("startSlideshow(true, 'horizontal')", self.html)
        self.assertIn("startSlideshow(true, 'vertical')", self.html)

    def test_orientation_is_sent_to_the_server(self):
        self.assertIn("params.set('orientation', slideshowOrientation)", self.html)

    def test_orientation_random_always_uses_the_server(self):
        self.assertIn(
            'random && (slideshowOrientation || (!searchMode && !activeMonth))', self.html
        )

    def test_stopping_clears_the_orientation(self):
        self.assertIn('slideshowOrientation = null;', self.html)


if __name__ == '__main__':
    unittest.main()
