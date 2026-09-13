"""HTTP fetching with the guard rails a feed reader needs.

urllib only. Every fetch is bounded (timeout, byte cap, redirect count), only
http(s) is ever followed, gzip is handled whether or not the server said so,
and conditional requests (ETag / Last-Modified) turn an unchanged feed into a
cheap 304. Blocking by design: callers run it in a worker thread.
"""

import gzip
import io
import os
import socket
import ssl
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import zlib

from . import VERSION, log

LOG = log.get("http")

USER_AGENT = "Omarchy-Podcast/%s (+https://github.com/hominluo/Omarchy-Podcast)" % VERSION
DEFAULT_TIMEOUT = 30
MAX_REDIRECTS = 5
FEED_CAP = 50 * 1024 * 1024
SMALL_CAP = 5 * 1024 * 1024
ARTWORK_CAP = 10 * 1024 * 1024
CHUNK = 64 * 1024


class FetchError(Exception):
    """A fetch that failed in a way the user can be told about."""

    def __init__(self, kind, message, status=None):
        super().__init__(message)
        self.kind = kind          # network | http | too-large | bad-url | tls
        self.message = message
        self.status = status


class NotModified(Exception):
    pass


class Response:
    def __init__(self, url, status, headers, body):
        self.url = url
        self.status = status
        self.headers = headers
        self.body = body

    @property
    def etag(self):
        return self.headers.get("ETag")

    @property
    def last_modified(self):
        return self.headers.get("Last-Modified")

    @property
    def content_type(self):
        return (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()

    def text(self, fallback="utf-8"):
        charset = _charset(self.headers.get("Content-Type") or "") or fallback
        try:
            return self.body.decode(charset, "replace")
        except LookupError:
            return self.body.decode(fallback, "replace")


class _Redirects(urllib.request.HTTPRedirectHandler):
    """Follow a few redirects, and only to http(s)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        scheme = urllib.parse.urlsplit(newurl).scheme.lower()
        if scheme not in ("http", "https"):
            raise FetchError("bad-url", "redirect to unsupported scheme %r" % scheme, status=code)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def check_url(url):
    parts = urllib.parse.urlsplit(str(url or "").strip())
    if parts.scheme.lower() not in ("http", "https") or not parts.netloc:
        raise FetchError("bad-url", "only http and https links can be fetched")
    return urllib.parse.urlunsplit(parts)


def _opener():
    handler = _Redirects()
    handler.max_redirections = MAX_REDIRECTS
    return urllib.request.build_opener(handler)


def fetch(url, cap=SMALL_CAP, timeout=DEFAULT_TIMEOUT, etag=None, last_modified=None,
          accept="*/*", extra_headers=None, method="GET", data=None):
    """Fetch `url` into memory (up to `cap` bytes). Raises NotModified on 304,
    FetchError on anything else that went wrong."""
    url = check_url(url)
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": accept,
        "Accept-Encoding": "gzip",
    }
    if etag:
        headers["If-None-Match"] = etag
    if last_modified:
        headers["If-Modified-Since"] = last_modified
    if extra_headers:
        headers.update(extra_headers)
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with _opener().open(request, timeout=timeout) as response:
            body = _read_capped(response, cap)
            return Response(response.geturl(), response.status, response.headers, body)
    except urllib.error.HTTPError as error:
        if error.code == 304:
            raise NotModified()
        raise FetchError("http", "server answered %d %s" % (error.code, error.reason or ""), status=error.code)
    except ssl.SSLError as error:
        raise FetchError("tls", "secure connection failed: %s" % _short(error))
    except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError, OSError) as error:
        reason = getattr(error, "reason", error)
        raise FetchError("network", "could not reach the server: %s" % _short(reason))
    except FetchError:
        raise


def _read_capped(response, cap):
    encoding = (response.headers.get("Content-Encoding") or "").lower()
    raw = io.BytesIO()
    total = 0
    while True:
        chunk = response.read(CHUNK)
        if not chunk:
            break
        total += len(chunk)
        if total > cap:
            raise FetchError("too-large", "the response is larger than %d MB" % (cap // (1024 * 1024)))
        raw.write(chunk)
    body = raw.getvalue()
    # Servers lie about gzip in both directions: sniff the magic bytes.
    if body[:2] == b"\x1f\x8b":
        try:
            body = gzip.decompress(body)
        except (OSError, EOFError, zlib.error) as error:
            raise FetchError("http", "could not decompress the response: %s" % _short(error))
    elif encoding == "deflate":
        try:
            body = zlib.decompress(body)
        except zlib.error:
            try:
                body = zlib.decompress(body, -zlib.MAX_WBITS)
            except zlib.error as error:
                raise FetchError("http", "could not decompress the response: %s" % _short(error))
    if len(body) > cap:
        raise FetchError("too-large", "the response is larger than %d MB" % (cap // (1024 * 1024)))
    return body


def open_stream(url, timeout=DEFAULT_TIMEOUT, headers=None, method="GET"):
    """Open a streaming response for large bodies (audio, models). The caller
    reads and closes it. Same error mapping as fetch()."""
    url = check_url(url)
    merged = {"User-Agent": USER_AGENT, "Accept": "*/*"}
    if headers:
        merged.update(headers)
    request = urllib.request.Request(url, headers=merged, method=method)
    try:
        return _opener().open(request, timeout=timeout)
    except urllib.error.HTTPError as error:
        if error.code == 416:
            raise FetchError("http", "range not satisfiable", status=416)
        raise FetchError("http", "server answered %d %s" % (error.code, error.reason or ""), status=error.code)
    except ssl.SSLError as error:
        raise FetchError("tls", "secure connection failed: %s" % _short(error))
    except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError, OSError) as error:
        reason = getattr(error, "reason", error)
        raise FetchError("network", "could not reach the server: %s" % _short(reason))


def download_to_file(url, path, cap, timeout=DEFAULT_TIMEOUT, headers=None):
    """Stream a bounded body straight to `path` (via a temp file next to it).
    Returns the Response with an empty body but the real headers."""
    response = open_stream(url, timeout=timeout, headers=headers)
    total = 0
    fd, tmp = tempfile.mkstemp(prefix=".dl.", dir=_dirname(path))
    try:
        with open(fd, "wb") as handle:
            while True:
                chunk = response.read(CHUNK)
                if not chunk:
                    break
                total += len(chunk)
                if total > cap:
                    raise FetchError("too-large", "the file is larger than %d MB" % (cap // (1024 * 1024)))
                handle.write(chunk)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    finally:
        response.close()
    return Response(response.geturl(), response.status, response.headers, b"")


def _charset(content_type):
    for part in content_type.split(";")[1:]:
        key, _, value = part.strip().partition("=")
        if key.lower() == "charset" and value:
            return value.strip().strip('"').strip("'")
    return None


def _dirname(path):
    return os.path.dirname(os.path.abspath(path)) or "."


def _short(error):
    text = str(error)
    return text if len(text) < 160 else text[:157] + "..."
