#!/usr/bin/env python3
"""The grid's thumbnail queue must not leak concurrency slots.

Navigating between months replaces the grid, aborting in-flight <img> loads
without firing load or error. A plain counter never came back down, so after a
few navigations no thumbnail was ever requested again.
"""

import os
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, str(ROOT))

PAGES = ('index.html', 'photos.html')


class ThumbQueueTests(unittest.TestCase):
    def pages(self):
        for name in PAGES:
            yield name, (ROOT / name).read_text()

    def test_no_bare_counter_remains(self):
        for name, src in self.pages():
            self.assertNotIn('thumbLoadActive', src, f'{name} still counts in-flight thumbs')

    def test_slots_are_tracked_per_element(self):
        for name, src in self.pages():
            self.assertIn('thumbLoadInFlight = new Set()', src, name)
            self.assertIn('thumbLoadInFlight.size < THUMB_CONCURRENCY', src, name)

    def test_detached_images_release_their_slot(self):
        for name, src in self.pages():
            self.assertIn('function pruneThumbSlots()', src, name)
            self.assertIn('if (!img.isConnected) releaseThumbSlot(img)', src, name)

    def test_queue_processing_prunes_before_filling(self):
        for name, src in self.pages():
            body = re.search(
                r'function processThumb(?:Load)?Queue\(\) \{(.*?)\n\}', src, re.S
            )
            self.assertIsNotNone(body, f'{name}: queue processor not found')
            head = body.group(1)
            prune = head.index('pruneThumbSlots()')
            fill = head.index('while (thumbLoadInFlight.size')
            self.assertLess(prune, fill, f'{name}: must reclaim slots before filling')

    def test_render_clears_and_reclaims(self):
        for name, src in self.pages():
            body = re.search(r'function clearThumbLoadQueue\(\) \{(.*?)\n\}', src, re.S)
            self.assertIsNotNone(body, name)
            self.assertIn('pruneThumbSlots()', body.group(1), name)

    def test_scrolling_away_reclaims(self):
        for name, src in self.pages():
            body = re.search(r'function unloadFarThumb\(img\) \{(.*?)\n\}', src, re.S)
            self.assertIsNotNone(body, name)
            self.assertIn('releaseThumbSlot(img)', body.group(1), name)

    def test_stalled_request_cannot_hold_a_slot(self):
        for name, src in self.pages():
            self.assertIn('THUMB_LOAD_TIMEOUT', src, name)
            self.assertIn('thumbLoadTimers.set(img, setTimeout(', src, name)

    def test_release_is_idempotent(self):
        # Both a timeout and a late load event can fire for the same image.
        for name, src in self.pages():
            body = re.search(r'function releaseThumbSlot\(img\) \{(.*?)\n\}', src, re.S)
            self.assertIsNotNone(body, name)
            self.assertIn('if (!thumbLoadInFlight.has(img)) return false;', body.group(1), name)


class WarmRequestTests(unittest.TestCase):
    def test_first_page_resets_the_warm_queue(self):
        src = (ROOT / 'index.html').read_text()
        self.assertIn("requestThumbWarm(added.length ? added : merged, !append)", src)
        self.assertIn("reset ? 'reset=1&' : ''", src)


if __name__ == '__main__':
    unittest.main()
