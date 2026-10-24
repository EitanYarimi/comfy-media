#!/usr/bin/env python3
"""Favorites are a browser-side list: a heart while watching, and a Favorites tab."""

import os
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, str(ROOT))


class FavoritesMarkupTests(unittest.TestCase):
    def test_nav_tab_on_every_page(self):
        for name in ('index.html', 'photos.html', 'run.html'):
            src = (ROOT / name).read_text()
            self.assertIn('href="index.html?favorites=1"', src, name)

    def test_player_heart_and_local_store(self):
        src = (ROOT / 'index.html').read_text()
        self.assertIn('id="modalFavBtn"', src)
        self.assertIn('id="favoritesNav"', src)
        self.assertIn("const FAVORITES_KEY = 'comfyVideoFavorites'", src)
        self.assertIn('function toggleFavoriteFromModal()', src)
        self.assertIn('data-action="favorite"', src)
        # Random on the Favorites tab stays inside the saved list.
        self.assertIn(
            'const useServerRandom = !favoritesMode && random &&',
            src,
        )
        self.assertIn('if (favoritesMode) return favoriteItems();', src)


class FavoritesBehaviorTests(unittest.TestCase):
    def test_heart_round_trip_in_the_page_script(self):
        script = ROOT / 'tests' / 'check_favorites_ui.mjs'
        proc = subprocess.run(
            ['node', str(script)],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            self.fail(proc.stdout + '\n' + proc.stderr)


if __name__ == '__main__':
    unittest.main()
