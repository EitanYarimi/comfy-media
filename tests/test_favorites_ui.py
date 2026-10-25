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
        self.assertIn('/api/workflow?path=', src)
        self.assertIn('class="info-prompt"', src)
        self.assertIn('function promptFieldsFromWorkflow', src)
        self.assertNotIn("setTimeout(() => { panel.style.display = 'none'; }, 5000)", src)
        self.assertIn('id="recipeCard"', src)
        self.assertIn('src="recipe.js"', src)
        self.assertIn('function buildRecipeFromFavorites()', src)
        run = (ROOT / 'run.html').read_text()
        self.assertIn('src="recipe.js"', run)
        self.assertIn('id="useRecipe"', run)
        self.assertIn('Same checkpoint and LoRAs', run)
        self.assertIn('function lockWorkflowModels()', run)

    def test_phone_menu_and_fullscreen(self):
        src = (ROOT / 'index.html').read_text()
        self.assertIn('id="menuToggle"', src)
        self.assertIn('id="pageControls"', src)
        self.assertIn('id="zoomResetBtn"', src)
        self.assertIn('webkitEnterFullscreen', src)
        self.assertIn("const APP_BUILD = '2026-10-25-colab-link'", src)
        self.assertIn('header.menu-open .controls { display: flex; }', src)
        self.assertNotIn('.modal-close--fs,\n            .modal-close--fit {\n                display: none;', src)
        photos = (ROOT / 'photos.html').read_text()
        self.assertIn('id="menuToggle"', photos)
        self.assertIn('id="lightboxFsBtn"', photos)
        self.assertIn('function toggleLightboxFullscreen()', photos)
        run = (ROOT / 'run.html').read_text()
        self.assertIn('min-height: 44px', run)

    def test_recipe_script_is_served_from_the_app_folder(self):
        import video_server

        class Body:
            def __init__(self):
                self.buf = bytearray()

            def write(self, data):
                self.buf.extend(data)

        class Handler:
            def __init__(self):
                self.command = 'GET'
                self.status = None
                self.headers = {}
                self.wfile = Body()

            def send_response(self, code):
                self.status = code

            def send_header(self, key, value):
                self.headers[key] = value

            def end_headers(self):
                pass

        handler = Handler()
        self.assertTrue(video_server.serve_app_html(handler, '/recipe.js'))
        self.assertEqual(handler.status, 200)
        self.assertIn('javascript', handler.headers['Content-Type'])
        self.assertIn(b'buildRecipe', bytes(handler.wfile.buf))
        self.assertNotIn('/recipe.js', video_server.APP_HTML_PAGES)


class FavoritesBehaviorTests(unittest.TestCase):
    def test_recipe_picks_the_shared_checkpoint_and_loras(self):
        script = ROOT / 'tests' / 'check_recipe.mjs'
        proc = subprocess.run(
            ['node', str(script)],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            self.fail(proc.stdout + '\n' + proc.stderr)

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
