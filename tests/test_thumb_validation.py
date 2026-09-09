#!/usr/bin/env python3
"""Reject solid-color / tiny video thumbnail garbage."""

import io
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import video_server  # noqa: E402


def _solid_jpeg(color, size=(64, 64)):
    from PIL import Image
    img = Image.new('RGB', size, color)
    buf = io.BytesIO()
    img.save(buf, format='JPEG', quality=85)
    return buf.getvalue()


def _varied_jpeg(size=(64, 64)):
    from PIL import Image
    img = Image.new('RGB', size, (20, 20, 20))
    px = img.load()
    for y in range(size[1]):
        for x in range(size[0]):
            px[x, y] = ((x * 7) % 255, (y * 11) % 255, (x * y) % 255)
    buf = io.BytesIO()
    img.save(buf, format='JPEG', quality=85)
    return buf.getvalue()


class ThumbValidationTests(unittest.TestCase):
    def test_reject_empty_and_json(self):
        self.assertFalse(video_server._thumb_is_usable(b''))
        self.assertFalse(video_server._thumb_is_usable(b'{"error":true}'))

    def test_reject_solid_red(self):
        data = _solid_jpeg((254, 0, 0))
        self.assertTrue(video_server._thumb_magic_ok(data))
        self.assertFalse(video_server._thumb_has_visual_content(data))
        self.assertFalse(video_server._thumb_is_usable(data))

    def test_accept_varied_frame(self):
        data = _varied_jpeg()
        self.assertTrue(video_server._thumb_is_usable(data))


if __name__ == '__main__':
    unittest.main()
