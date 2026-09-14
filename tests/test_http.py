import gzip
import io
import unittest
import zlib

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


if __name__ == "__main__":
    unittest.main()
