#!/usr/bin/env python3
"""Photo grid thumbs must cache after the first decode."""

import io
import os
import random
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import video_server  # noqa: E402


def checkerboard_png(width=240, height=160):
    from PIL import Image, ImageDraw
    img = Image.new('RGB', (width, height), (0, 0, 0))
    draw = ImageDraw.Draw(img)
    rng = random.Random(3)
    step = 8
    for y in range(0, height, step):
        for x in range(0, width, step):
            shade = 255 if (x // step + y // step) % 2 else rng.randrange(256)
            draw.rectangle([x, y, x + step, y + step], fill=(shade, shade, shade))
    buf = io.BytesIO()
    img.save(buf, 'PNG')
    return buf.getvalue()


class PhotoThumbTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.prev_cache = video_server.THUMB_CACHE_DIR
        self.prev_legacy = video_server.LEGACY_THUMB_CACHE_DIR
        video_server.THUMB_CACHE_DIR = root / 'thumbs'
        video_server.LEGACY_THUMB_CACHE_DIR = root / 'legacy'
        video_server.THUMB_CACHE_DIR.mkdir()
        video_server._thumb_memory.clear()
        self.png = root / 'shot.png'
        self.png.write_bytes(checkerboard_png())

    def tearDown(self):
        video_server.THUMB_CACHE_DIR = self.prev_cache
        video_server.LEGACY_THUMB_CACHE_DIR = self.prev_legacy
        video_server._thumb_memory.clear()
        video_server._photo_ondemand = 0
        video_server._thumb_inflight.clear()
        self.tmp.cleanup()

    def test_generate_writes_a_cache_file(self):
        first = video_server.generate_photo_thumbnail(self.png)
        self.assertIsNotNone(first)
        data, mime = first
        self.assertTrue(video_server._thumb_magic_ok(data))
        self.assertIn(mime, ('image/webp', 'image/jpeg'))
        cached = list(video_server.THUMB_CACHE_DIR.glob('*'))
        self.assertTrue(any(p.suffix in ('.webp', '.jpg') for p in cached))

    def test_second_call_does_not_reopen_the_original(self):
        video_server.generate_photo_thumbnail(self.png)
        video_server._thumb_memory.clear()
        real_open = video_server.Image.open if hasattr(video_server, 'Image') else None
        from PIL import Image as PILImage
        opens = []
        original = PILImage.open

        def counting(fp, *a, **k):
            opens.append(str(fp))
            return original(fp, *a, **k)

        import PIL.Image
        PIL.Image.open = counting
        try:
            second = video_server.generate_photo_thumbnail(self.png)
        finally:
            PIL.Image.open = original
        self.assertIsNotNone(second)
        self.assertEqual(opens, [])

    def test_cache_key_does_not_stat_via_resolve(self):
        with mock.patch.object(Path, 'resolve', side_effect=AssertionError('Drive resolve')):
            key = video_server._photo_cache_key(self.png)
        self.assertEqual(key, video_server._cache_key_from_rel(str(self.png), ':400webp'))

    def test_parallel_generate_opens_the_original_once(self):
        from PIL import Image as PILImage
        opens = []
        original = PILImage.open

        def counting(fp, *a, **k):
            if str(fp) == str(self.png):
                opens.append(str(fp))
                time.sleep(0.05)
            return original(fp, *a, **k)

        import PIL.Image
        PIL.Image.open = counting
        results = []

        def run():
            results.append(video_server.generate_photo_thumbnail(self.png))

        try:
            threads = [threading.Thread(target=run) for _ in range(4)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
        finally:
            PIL.Image.open = original
        self.assertEqual(len(opens), 1)
        self.assertTrue(all(results))

    def test_background_skips_while_ondemand_is_waiting(self):
        from PIL import Image as PILImage
        opens = []
        original = PILImage.open

        def counting(fp, *a, **k):
            opens.append(1)
            return original(fp, *a, **k)

        import PIL.Image
        PIL.Image.open = counting
        video_server._photo_ondemand_begin()
        try:
            self.assertIsNone(video_server.generate_photo_thumbnail(self.png, background=True))
            self.assertEqual(opens, [])
        finally:
            video_server._photo_ondemand_end()
            PIL.Image.open = original
        self.assertIsNotNone(video_server.generate_photo_thumbnail(self.png, background=True))


class PhotoFrontendTests(unittest.TestCase):
    def test_photos_page_warms_visible_paths(self):
        src = Path(ROOT, 'photos.html').read_text()
        self.assertIn("fetch(`${serverBaseUrl}/api/warm?", src)
        self.assertIn('requestThumbWarm(added.length ? added : merged, !append)', src)
        self.assertIn("img.dataset.src && img.getAttribute('src')) return", src)
        self.assertIn('function removePhotoFromState(photo)', src)
        self.assertIn("fetch(`${serverBaseUrl}/api/thumbs`", src)


class PhotoDeleteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        out = self.root / 'ComfyUI' / 'output'
        out.mkdir(parents=True)
        self.keep = out / 'keep.png'
        self.gone = out / 'gone.png'
        self.keep.write_bytes(checkerboard_png())
        self.gone.write_bytes(checkerboard_png())
        self.prev = {
            'cwd': os.getcwd(),
            'mode': video_server.STORAGE_MODE,
            'photo_index': video_server.PHOTO_INDEX_PATH,
            'legacy_photo': video_server.LEGACY_PHOTO_INDEX_PATH,
            'thumbs': video_server.THUMB_CACHE_DIR,
            'legacy_thumbs': video_server.LEGACY_THUMB_CACHE_DIR,
        }
        os.chdir(self.root)
        video_server.STORAGE_MODE = 'local'
        video_server.THUMB_CACHE_DIR = self.root / 'thumbs'
        video_server.LEGACY_THUMB_CACHE_DIR = self.root / 'legacy'
        video_server.THUMB_CACHE_DIR.mkdir()
        video_server.PHOTO_INDEX_PATH = video_server.THUMB_CACHE_DIR / 'photos_index_v1.json'
        video_server.LEGACY_PHOTO_INDEX_PATH = video_server.LEGACY_THUMB_CACHE_DIR / 'missing.json'
        video_server._thumb_memory.clear()
        video_server.invalidate_photo_cache()
        now = time.time()
        video_server._commit_scan('photos', [
            {'name': 'keep.png', 'path': 'ComfyUI/output/keep.png', 'size': 1, 'modified': now},
            {'name': 'gone.png', 'path': 'ComfyUI/output/gone.png', 'size': 1, 'modified': now - 1},
        ])

    def tearDown(self):
        os.chdir(self.prev['cwd'])
        video_server.STORAGE_MODE = self.prev['mode']
        video_server.PHOTO_INDEX_PATH = self.prev['photo_index']
        video_server.LEGACY_PHOTO_INDEX_PATH = self.prev['legacy_photo']
        video_server.THUMB_CACHE_DIR = self.prev['thumbs']
        video_server.LEGACY_THUMB_CACHE_DIR = self.prev['legacy_thumbs']
        video_server._thumb_memory.clear()
        video_server.invalidate_photo_cache()
        self.tmp.cleanup()

    def test_delete_removes_file_index_and_thumb(self):
        rel = Path('ComfyUI/output/gone.png')
        self.assertTrue(rel.is_file(), msg=os.getcwd())
        self.assertIsNotNone(video_server.generate_photo_thumbnail(rel))
        self.assertTrue(video_server._photo_thumb_on_disk('ComfyUI/output/gone.png'))
        self.assertTrue(video_server.delete_local_media('ComfyUI/output/gone.png'))
        self.assertFalse(self.gone.exists())
        self.assertTrue(self.keep.exists())
        names = [p['name'] for p in video_server.get_photos_cached()]
        self.assertEqual(names, ['keep.png'])
        self.assertFalse(video_server._photo_thumb_on_disk('ComfyUI/output/gone.png'))
        video_server.invalidate_photo_cache()
        names = [p['name'] for p in video_server.get_photos_cached()]
        self.assertEqual(names, ['keep.png'])


if __name__ == '__main__':
    unittest.main()
