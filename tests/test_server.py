import json
import tempfile
import threading
import unittest
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer

from app.server import Application, handler


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app = Application(self.tmp.name)
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), handler(self.app))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join()
        self.app.store.db.close(); self.tmp.cleanup()

    def request(self, method, path, body=None, headers=None):
        conn = HTTPConnection('127.0.0.1', self.server.server_port, timeout=5)
        payload = json.dumps(body).encode() if body is not None else None
        all_headers = dict(headers or {})
        if body is not None:
            all_headers['Content-Type'] = 'application/json'
            all_headers['Content-Length'] = str(len(payload))
        conn.request(method, path, payload, all_headers)
        response = conn.getresponse(); data = response.read(); conn.close()
        return response.status, response.getheaders(), data

    def test_health(self):
        status, _, data = self.request('GET', '/health')
        self.assertEqual(200, status)
        self.assertTrue(json.loads(data)['ok'])

    def test_settings_page_is_public(self):
        status, _, data = self.request('GET', '/')
        self.assertEqual(200, status)
        self.assertIn(b'Plex-Kinopois', data)

    def test_movie_manifest_json(self):
        status, _, data = self.request('GET', '/providers/movies', headers={'Accept':'application/json'})
        body = json.loads(data)['MediaProvider']
        self.assertEqual(200, status)
        self.assertEqual([1], [x['type'] for x in body['Types']])
        self.assertEqual(
            [
                {'type': 'metadata', 'key': '/library/metadata'},
                {'type': 'match', 'key': '/library/metadata/matches'},
            ],
            body['Feature'],
        )

    def test_show_manifest_xml(self):
        status, headers, data = self.request('GET', '/providers/shows', headers={'Accept':'application/xml'})
        self.assertEqual(200, status)
        self.assertIn(('Content-Type', 'application/xml; charset=utf-8'), headers)
        self.assertIn(b'<MediaProvider', data)
        self.assertIn(b'tv.plex.agents.custom.plexkinopois.shows', data)

    def test_settings_require_login_and_hide_key(self):
        status, _, _ = self.request('GET', '/api/settings')
        self.assertEqual(401, status)
        status, headers, _ = self.request('POST', '/api/login', {'token':self.app.store.admin_token})
        self.assertEqual(200, status)
        cookie = next(v for k,v in headers if k == 'Set-Cookie').split(';',1)[0]
        status, _, data = self.request('POST', '/api/settings', {'kp_key':'very-secret'}, {'Cookie':cookie})
        self.assertEqual(200, status)
        self.assertNotIn(b'very-secret', data)
        self.assertTrue(json.loads(data)['kp_key_configured'])

    def test_rejects_cross_origin_admin_request(self):
        status, _, _ = self.request('POST', '/api/login', {'token':self.app.store.admin_token},
                                    {'Origin':'http://evil.example'})
        self.assertEqual(401, status)


if __name__ == '__main__': unittest.main()
