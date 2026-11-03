import io
import json
import os
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from unittest import mock

import app

OCTOCAT_GIST = {
    "id": "6cad326836d38bd3a7ae",
    "description": "Hello world!",
    "html_url": "https://gist.github.com/octocat/6cad326836d38bd3a7ae",
    "files": {"hello_world.rb": {}},
    "created_at": "2014-10-01T16:19:34Z",
    "updated_at": "2023-05-16T19:58:06Z",
}


class ServerTestCase(unittest.TestCase):
    """Runs the real HTTP server on a random free port for each test class."""

    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def get(self, path):
        try:
            with urllib.request.urlopen(self.base_url + path, timeout=30) as resp:
                return resp.status, json.load(resp)
        except urllib.error.HTTPError as e:
            return e.code, json.load(e)


class TestWithMockedGitHub(ServerTestCase):
    def setUp(self):
        # app and the test client share urllib.request, so only patch calls to GitHub.
        real_urlopen = urllib.request.urlopen
        self.calls = []

        def router(request, timeout=None):
            url = request if isinstance(request, str) else request.full_url
            if url.startswith(self.base_url):
                return real_urlopen(request, timeout=timeout)
            self.calls.append(url)
            return self.github(request)

        patcher = mock.patch("urllib.request.urlopen", side_effect=router)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.github = lambda request: io.BytesIO(json.dumps([OCTOCAT_GIST]).encode())

    def github_error(self, status, headers=None):
        def raise_error(request):
            raise urllib.error.HTTPError(request.full_url, status, "error", headers or {}, io.BytesIO(b"{}"))
        self.github = raise_error

    def test_lists_gists(self):
        status, body = self.get("/octocat")
        self.assertEqual(status, 200)
        self.assertEqual(body, [{
            "id": "6cad326836d38bd3a7ae",
            "description": "Hello world!",
            "url": "https://gist.github.com/octocat/6cad326836d38bd3a7ae",
            "files": ["hello_world.rb"],
            "created_at": "2014-10-01T16:19:34Z",
            "updated_at": "2023-05-16T19:58:06Z",
        }])
        self.assertIn("/users/octocat/gists?page=1&per_page=30", self.calls[0])

    def test_pagination_passed_through(self):
        self.get("/octocat?page=2&per_page=5")
        self.assertIn("page=2&per_page=5", self.calls[0])

    def test_per_page_capped_at_100(self):
        self.get("/octocat?per_page=500")
        self.assertIn("per_page=100", self.calls[0])

    def test_invalid_pagination(self):
        status, body = self.get("/octocat?page=abc")
        self.assertEqual(status, 400)
        self.assertIn("page", body["error"])
        self.assertEqual(self.calls, [])

    def test_unknown_user(self):
        self.github_error(404)
        status, body = self.get("/no-such-user")
        self.assertEqual(status, 404)
        self.assertIn("not found", body["error"])

    def test_rate_limited(self):
        self.github_error(403, {"x-ratelimit-remaining": "0", "x-ratelimit-reset": "1700000000"})
        with self.assertLogs("gists", "WARNING") as logs:
            status, _ = self.get("/octocat")
        self.assertEqual(status, 503)
        self.assertIn("GitHub returned 403", logs.output[0])
        self.assertIn("remaining=0, reset=1700000000", logs.output[0])

    def test_upstream_failure(self):
        self.github_error(500)
        with self.assertLogs("gists", "WARNING") as logs:
            status, _ = self.get("/octocat")
        self.assertEqual(status, 502)
        self.assertIn("GitHub returned 500", logs.output[0])

    def test_upstream_unreachable(self):
        def unreachable(request):
            raise urllib.error.URLError("connection refused")
        self.github = unreachable
        with self.assertLogs("gists", "ERROR") as logs:
            status, _ = self.get("/octocat")
        self.assertEqual(status, 502)
        self.assertIn("GitHub unreachable", logs.output[0])

    def test_invalid_username_not_forwarded(self):
        for path in ("/", "/bad_name", "/-leading", "/a/b"):
            status, _ = self.get(path)
            self.assertEqual(status, 404, path)
        self.assertEqual(self.calls, [])

    def test_healthz(self):
        self.assertEqual(self.get("/healthz"), (200, {"status": "ok"}))


class TestConfig(unittest.TestCase):
    def test_github_api_must_be_https(self):
        app.check_github_api("https://api.github.com")
        for bad in ("http://api.github.com", "file:///etc/passwd"):
            with self.subTest(url=bad), self.assertRaises(SystemExit):
                app.check_github_api(bad)


@unittest.skipIf(os.environ.get("SKIP_INTEGRATION"), "SKIP_INTEGRATION is set")
class TestAgainstRealGitHub(ServerTestCase):
    def test_octocat_has_public_gists(self):
        status, body = self.get("/octocat")
        self.assertEqual(status, 200)
        self.assertIsInstance(body, list)
        self.assertGreater(len(body), 0)
        self.assertTrue(all(g["url"].startswith("https://gist.github.com/") for g in body))


if __name__ == "__main__":
    unittest.main()
