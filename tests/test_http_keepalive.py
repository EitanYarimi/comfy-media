#!/usr/bin/env python3
"""HTTP/1.1 keep-alive responses must include Content-Length or clients hang."""

import http.client
import os
import sys
import threading
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ['SITE_PASSWORD'] = 'test-pass'
os.environ['STORAGE_MODE'] = 'local'
os.environ['MEDIA_PREWARM'] = '0'
sys.path.insert(0, ROOT)

import video_server  # noqa: E402

video_server.SITE_PASSWORD = 'test-pass'
video_server.STORAGE_MODE = 'local'


class KeepAliveTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.httpd = video_server.ThreadedHTTPServer(('127.0.0.1', 0), video_server.VideoHandler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()
        deadline = time.time() + 2
        while time.time() < deadline:
            try:
                conn = http.client.HTTPConnection('127.0.0.1', cls.port, timeout=0.2)
                conn.request('GET', '/healthz')
                conn.getresponse().read()
                conn.close()
                return
            except OSError:
                time.sleep(0.02)
        raise RuntimeError('test server did not start')

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def _conn(self):
        return http.client.HTTPConnection('127.0.0.1', self.port, timeout=3)

    def test_unauthorized_api_finishes_quickly(self):
        started = time.time()
        conn = self._conn()
        conn.request('GET', '/api/videos?summary=1')
        resp = conn.getresponse()
        body = resp.read()
        elapsed = time.time() - started
        self.assertEqual(resp.status, 401)
        self.assertEqual(resp.getheader('Content-Length'), str(len(body)))
        self.assertEqual(body, video_server.UNAUTHORIZED_JSON)
        self.assertLess(elapsed, 1.5)
        conn.close()

    def test_login_redirect_has_content_length(self):
        started = time.time()
        conn = self._conn()
        conn.request(
            'POST',
            '/login',
            body='password=test-pass',
            headers={'Content-Type': 'application/x-www-form-urlencoded'},
        )
        resp = conn.getresponse()
        body = resp.read()
        elapsed = time.time() - started
        self.assertEqual(resp.status, 303)
        self.assertEqual(resp.getheader('Content-Length'), '0')
        self.assertEqual(body, b'')
        self.assertEqual(resp.getheader('Location'), '/index.html')
        self.assertLess(elapsed, 1.5)
        cookie = resp.getheader('Set-Cookie')
        self.assertIn(video_server.AUTH_COOKIE + '=', cookie)
        conn.close()

        conn = self._conn()
        conn.request(
            'GET',
            '/api/videos?summary=1',
            headers={'Cookie': cookie.split(';', 1)[0]},
        )
        resp = conn.getresponse()
        body = resp.read()
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.getheader('Content-Length'), str(len(body)))
        conn.close()

    def test_healthz_has_content_length(self):
        conn = self._conn()
        conn.request('GET', '/healthz')
        resp = conn.getresponse()
        body = resp.read()
        self.assertEqual(resp.status, 200)
        self.assertEqual(body, video_server.HEALTHZ_BODY)
        self.assertEqual(resp.getheader('Content-Length'), str(len(body)))
        conn.close()


if __name__ == '__main__':
    unittest.main()
