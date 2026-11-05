import gzip
import io
import threading
import unittest
import urllib.error
import urllib.request
import zlib

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from engine import http


class _Response:
    """Enough of an HTTPResponse for _read_capped: read1 hands back at most
    `step` bytes per call, the way a slow socket would."""

    def __init__(self, body, headers=None, step=64 * 1024):
        self._buffer = io.BytesIO(body)
        self.headers = headers or {}
        self._step = step

    def read1(self, size):
        return self._buffer.read(min(size, self._step))


class ClientIdentityTest(unittest.TestCase):
    def test_user_agent_carries_no_url(self):
        # feed.xyzfm.space and friends answer 403 to agents that name a URL.
        self.assertNotIn("http", http.USER_AGENT.lower())
        self.assertNotIn("github", http.USER_AGENT.lower())
        self.assertTrue(http.USER_AGENT.startswith("Omarchy-Podcast/"))

    def test_redirect_budget_covers_tracking_chains(self):
        self.assertGreaterEqual(http.MAX_REDIRECTS, 8)


class ReadCappedTest(unittest.TestCase):
    def test_plain_body(self):
        self.assertEqual(http._read_capped(_Response(b"hello"), 100), b"hello")
        self.assertEqual(http._read_capped(_Response(b"x", step=1), 100), b"x")

    def test_gzip_multi_member_all_chunk_sizes(self):
        a, b = b"A" * 6000, b"B" * 6000
        body = gzip.compress(a) + b"\x00\x00" + gzip.compress(b)
        for step in (1, 7, 100, 64 * 1024):
            for headers in ({}, {"Content-Encoding": "gzip"}):
                out = http._read_capped(_Response(body, headers, step=step), 1 << 20)
                self.assertEqual(out, a + b, "step=%d headers=%r" % (step, headers))

    def test_gzip_cap_spans_members(self):
        body = gzip.compress(b"A" * 6000) + gzip.compress(b"B" * 6000)
        with self.assertRaises(http.FetchError) as caught:
            http._read_capped(_Response(body), 7000)
        self.assertEqual(caught.exception.kind, "too-large")

    def test_gzip_trailing_garbage_and_truncation_are_errors(self):
        with self.assertRaises(http.FetchError):
            http._read_capped(_Response(gzip.compress(b"A" * 100) + b"garbage"), 1 << 20)
        with self.assertRaises(http.FetchError):
            http._read_capped(_Response(gzip.compress(b"A" * 6000)[:-10]), 1 << 20)

    def test_deflate(self):
        body = zlib.compress(b"D" * 5000)
        self.assertEqual(http._read_capped(_Response(body, {"Content-Encoding": "deflate"}, step=13), 1 << 20), b"D" * 5000)


class PrivateRedirectTest(unittest.TestCase):
    def test_literal_private_targets_are_refused(self):
        origin = "https://feeds.example.com/show.xml"
        for target in ("http://127.0.0.1:8080/feed", "http://localhost/feed",
                       "http://169.254.169.254/latest/meta-data/",
                       "http://10.0.0.5/feed", "http://192.168.1.5/feed",
                       "http://[::1]/feed", "http://0.0.0.0/feed"):
            self.assertTrue(http._private_redirect(origin, target), target)

    def test_public_targets_still_follow(self):
        self.assertFalse(http._private_redirect("https://feeds.example.com/x", "https://93.184.216.34/y"))
        # A local feed may redirect to another local address (an SSH tunnel,
        # a media server, the test harness).
        self.assertFalse(http._private_redirect("http://127.0.0.1:8080/x", "http://127.0.0.1:9090/y"))

    def test_feed_controlled_redirects_may_not_touch_loopback(self):
        # The user typed the original feed URL; the feed controls the target.
        for target in ("http://127.0.0.1:9090/y", "http://localhost/y", "http://192.168.1.5/y"):
            self.assertTrue(http._private_redirect("http://93.184.216.34/x", target, allow_local=False), target)

    def test_redirect_handler_refuses_a_private_hop(self):
        req = urllib.request.Request("https://feeds.example.com/show.xml")
        with self.assertRaises(http.FetchError) as caught:
            http._Redirects().redirect_request(req, None, 302, "Found", {}, "http://127.0.0.1/feed")
        self.assertEqual(caught.exception.kind, "bad-url")

    def test_direct_local_subscription_is_still_accepted(self):
        self.assertEqual(http.check_url("http://127.0.0.1:8080/feed.xml"),
                         "http://127.0.0.1:8080/feed.xml")
        self.assertEqual(http.check_url("https://[::1]:8080/feed.xml"),
                         "https://[::1]:8080/feed.xml")

    def test_feed_controlled_loopback_is_refused(self):
        for url in ("http://127.0.0.1:8080/art.jpg", "http://[::1]:8080/art.jpg"):
            with self.assertRaises(http.FetchError, msg=url) as caught:
                http.check_url(url, allow_local=False)
            self.assertEqual(caught.exception.kind, "bad-url")

    def test_url_is_local_separates_feed_origins(self):
        self.assertTrue(http.url_is_local("http://127.0.0.1:8080/feed.xml"))
        self.assertTrue(http.url_is_local("http://192.168.1.5/feed.xml"))
        self.assertFalse(http.url_is_local("https://93.184.216.34/feed.xml"))

    def test_literal_private_links_are_refused(self):
        for url in ("http://169.254.169.254/latest/meta-data/",
                    "http://10.0.0.5/feed", "http://192.168.1.5/feed",
                    "http://[fd00::1]/feed", "http://0.0.0.0/feed"):
            with self.assertRaises(http.FetchError, msg=url) as caught:
                http.check_url(url)
            self.assertEqual(caught.exception.kind, "bad-url")


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = b"ok"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class FirstHopTest(unittest.TestCase):
    """The check the review asked for: feed-controlled first hops, not just
    redirects. A name is resolved and the peer is the address actually
    reached, so DNS rebinding cannot slip past a pre-flight lookup."""

    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def test_user_typed_loopback_feed_is_allowed(self):
        response = http.fetch("http://127.0.0.1:%d/feed" % self.port)
        self.assertEqual(response.body, b"ok")

    def test_feed_controlled_loopback_literal_is_refused(self):
        with self.assertRaises(http.FetchError) as caught:
            http.fetch("http://127.0.0.1:%d/art.jpg" % self.port, allow_local=False)
        self.assertEqual(caught.exception.kind, "bad-url")

    def test_named_loopback_first_hop_is_resolved_and_refused(self):
        # `localhost` is not a literal private address at check_url(), so
        # this exercises the connection-time resolution rather than the
        # string check.
        with self.assertRaises(http.FetchError) as caught:
            http.fetch("http://localhost:%d/art.jpg" % self.port, allow_local=False)
        self.assertEqual(caught.exception.kind, "bad-url")


class _RaisingOpener:
    def __init__(self, error):
        self.error = error

    def open(self, *args, **kwargs):
        raise self.error


class HttpErrorCloseTest(unittest.TestCase):
    def setUp(self):
        self.original = http._opener

    def tearDown(self):
        http._opener = self.original

    def test_fetch_closes_the_error_response(self):
        body = io.BytesIO(b"error body")
        error = urllib.error.HTTPError("http://x/feed", 500, "boom", {}, body)
        http._opener = lambda: _RaisingOpener(error)
        with self.assertRaises(http.FetchError):
            http.fetch("http://x/feed")
        self.assertTrue(body.closed)

    def test_not_modified_closes_the_error_response(self):
        body = io.BytesIO(b"")
        error = urllib.error.HTTPError("http://x/feed", 304, "Not Modified", {}, body)
        http._opener = lambda: _RaisingOpener(error)
        with self.assertRaises(http.NotModified):
            http.fetch("http://x/feed")
        self.assertTrue(body.closed)

    def test_probe_headers_closes_after_a_redirect(self):
        body = io.BytesIO(b"")
        error = urllib.error.HTTPError("http://x/model", 302, "Found", {"Location": "http://y/model"}, body)
        original = http.urllib.request.build_opener
        http.urllib.request.build_opener = lambda *args, **kwargs: _RaisingOpener(error)
        try:
            headers = http.probe_headers("http://x/model")
        finally:
            http.urllib.request.build_opener = original
        self.assertEqual(headers["Location"], "http://y/model")
        self.assertTrue(body.closed)


if __name__ == "__main__":
    unittest.main()
