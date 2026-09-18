"""HTTP fetching with the guard rails a feed reader needs.

urllib only. Every fetch is bounded (timeout, byte cap, redirect count), only
http(s) is ever followed and an https link is never followed down to plain
http, gzip is handled whether or not the server said so, and conditional
requests (ETag / Last-Modified) turn an unchanged feed into a cheap 304.
Blocking by design: callers run it in a worker thread.
"""

import http.client
import io
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib

from . import VERSION, log

LOG = log.get("http")

# No URL in the agent string: some feed hosts (xiaoyuzhou's feed.xyzfm.space
# among them) answer 403 to any agent that carries one.
USER_AGENT = "Omarchy-Podcast/%s (Linux)" % VERSION
DEFAULT_TIMEOUT = 30
# Tracking chains on enclosures (podtrac -> pdst -> vpixl -> pscrb -> host)
# run to six or seven hops; feeds rarely need more than two.
MAX_REDIRECTS = 10
FEED_CAP = 50 * 1024 * 1024
SMALL_CAP = 5 * 1024 * 1024
ARTWORK_CAP = 10 * 1024 * 1024
CHUNK = 64 * 1024
# A whole in-memory fetch has this long (plus one socket timeout), whatever
# the per-read timeout says, so a server trickling one byte at a time cannot
# pin a pool thread. Reads use read1(): at most one recv per call, so the
# deadline is checked at least every socket timeout.
TOTAL_DEADLINE = 300
SENSITIVE_HEADERS = ("authorization", "x-auth-key", "x-auth-date", "cookie")


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
    """Follow a few redirects, only to http(s), never from https down to
    plain http, and never carry credentials to another host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = urllib.parse.urlsplit(newurl)
        scheme = target.scheme.lower()
        if scheme not in ("http", "https"):
            raise FetchError("bad-url", "redirect to unsupported scheme %r" % scheme, status=code)
        # req.full_url is this hop's URL, so a chain that goes https -> https
        # -> http is caught at the hop that downgrades.
        origin = urllib.parse.urlsplit(req.full_url)
        if origin.scheme.lower() == "https" and scheme == "http":
            raise FetchError("bad-url", "the server redirected from https to plain http; refusing", status=code)
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is None:
            return None
        if target.netloc.lower() != origin.netloc.lower():
            for name in list(new.headers.keys()):
                if name.lower() in SENSITIVE_HEADERS:
                    del new.headers[name]
            for name in list(new.unredirected_hdrs.keys()):
                if name.lower() in SENSITIVE_HEADERS:
                    del new.unredirected_hdrs[name]
        return new


def check_url(url):
    """Validate and normalise a URL for urllib: http(s) only, no control
    characters, and the path/query percent-encoded (feeds ship enclosure
    URLs with literal spaces, which http.client rejects outright)."""
    text = str(url or "").strip()
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in text):
        raise FetchError("bad-url", "the link contains control characters")
    try:
        parts = urllib.parse.urlsplit(text)
    except ValueError as error:
        raise FetchError("bad-url", "that is not a valid link: %s" % error)
    if parts.scheme.lower() not in ("http", "https") or not parts.netloc:
        raise FetchError("bad-url", "only http and https links can be fetched")
    if any(ch.isspace() for ch in parts.netloc) or not (parts.hostname or ""):
        raise FetchError("bad-url", "the link has no valid host")
    try:
        parts.port  # raises for a malformed port
    except ValueError:
        raise FetchError("bad-url", "the link has an invalid port")
    path = urllib.parse.quote(parts.path, safe="/%:@&=+$,;~!*'()")
    query = urllib.parse.quote(parts.query, safe="/%:@&=+$,;~!*'()?")
    return urllib.parse.urlunsplit((parts.scheme.lower(), parts.netloc, path, query, ""))


NETWORK_ERRORS = (urllib.error.URLError, http.client.HTTPException, socket.timeout, TimeoutError, ConnectionError, OSError)


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
    except FetchError:
        raise
    except urllib.error.HTTPError as error:
        if error.code == 304:
            raise NotModified()
        raise FetchError("http", _http_message(error), status=error.code)
    except ssl.SSLError as error:
        raise FetchError("tls", "secure connection failed: %s" % _short(error))
    except NETWORK_ERRORS as error:
        reason = getattr(error, "reason", error)
        if isinstance(reason, ssl.SSLError):
            raise FetchError("tls", "secure connection failed: %s" % _short(reason))
        raise FetchError("network", "could not reach the server: %s" % _short(reason))


def _read_capped(response, cap):
    """Read at most `cap` bytes *after* decompression. The body is inflated
    incrementally so a compressed bomb is rejected at the cap instead of
    being expanded in full first."""
    encoding = (response.headers.get("Content-Encoding") or "").lower()
    started = time.monotonic()
    out = io.BytesIO()
    total = 0
    inflater = None
    gzipped = False      # gzip bodies are a series of members (RFC 1952)
    between = False      # finished one member, expecting the next header
    sniffed = False
    head = b""
    while True:
        chunk = response.read1(CHUNK)
        if not chunk:
            break
        if time.monotonic() - started > TOTAL_DEADLINE:
            raise FetchError("network", "the server is too slow")
        if not sniffed:
            # read1 may hand back a single byte first; the magic needs two.
            head += chunk
            if len(head) < 2:
                continue
            chunk, head = head, b""
            sniffed = True
            # Servers lie about gzip in both directions: sniff the magic bytes.
            if chunk[:2] == b"\x1f\x8b":
                inflater = zlib.decompressobj(16 + zlib.MAX_WBITS)
                gzipped = True
            elif encoding == "deflate":
                inflater = zlib.decompressobj(zlib.MAX_WBITS if chunk[:1] == b"\x78" else -zlib.MAX_WBITS)
        if inflater is None:
            piece = chunk
        else:
            piece = b""
            data = chunk
            while True:
                if between:
                    # Between members: skip NUL padding, then expect a header.
                    data = data.lstrip(b"\x00")
                    if not data:
                        break
                    inflater = zlib.decompressobj(16 + zlib.MAX_WBITS)
                    between = False
                try:
                    piece += inflater.decompress(data, cap - total - len(piece) + 1)
                except zlib.error as error:
                    raise FetchError("http", "could not decompress the response: %s" % _short(error))
                if inflater.unconsumed_tail:
                    raise FetchError("too-large", "the response is larger than %d MB" % (cap // (1024 * 1024)))
                if not (gzipped and inflater.eof):
                    break
                # One decompressobj stops after the first member and parks the
                # rest in unused_data; carry it into the next member.
                data = inflater.unused_data
                between = True
        total += len(piece)
        if total > cap:
            raise FetchError("too-large", "the response is larger than %d MB" % (cap // (1024 * 1024)))
        out.write(piece)
    if head:
        # A one-byte body never reached the sniff.
        out.write(head)
        total += len(head)
    if inflater is not None:
        try:
            tail = inflater.flush()
        except zlib.error as error:
            raise FetchError("http", "could not decompress the response: %s" % _short(error))
        if not inflater.eof and not between:
            raise FetchError("http", "the compressed response ended before its end-of-stream marker")
        total += len(tail)
        if total > cap:
            raise FetchError("too-large", "the response is larger than %d MB" % (cap // (1024 * 1024)))
        out.write(tail)
    return out.getvalue()


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
    except FetchError:
        raise
    except urllib.error.HTTPError as error:
        if error.code == 416:
            raise FetchError("http", "range not satisfiable", status=416)
        raise FetchError("http", _http_message(error), status=error.code)
    except ssl.SSLError as error:
        raise FetchError("tls", "secure connection failed: %s" % _short(error))
    except NETWORK_ERRORS as error:
        reason = getattr(error, "reason", error)
        raise FetchError("network", "could not reach the server: %s" % _short(reason))


def read_chunk(response):
    """One read from a streaming response with the network errors mapped."""
    try:
        return response.read1(CHUNK)
    except NETWORK_ERRORS as error:
        raise FetchError("network", "the connection dropped: %s" % _short(error))


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse every redirect so the *first* hop's headers come back (as the
    HTTPError urllib raises for an unhandled 3xx)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def probe_headers(url, timeout=20):
    """HEAD `url` and return the first hop's headers without following a
    redirect. Hugging Face, for one, puts the pinned object's size and
    digest on the 302 that sends a client to its CDN; the final response
    no longer carries them."""
    url = check_url(url)
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT}, method="HEAD")
    try:
        with urllib.request.build_opener(_NoRedirect()).open(request, timeout=timeout) as response:
            return response.headers
    except urllib.error.HTTPError as error:
        if 300 <= error.code < 400:
            return error.headers
        raise FetchError("http", _http_message(error), status=error.code)
    except ssl.SSLError as error:
        raise FetchError("tls", "secure connection failed: %s" % _short(error))
    except NETWORK_ERRORS as error:
        reason = getattr(error, "reason", error)
        raise FetchError("network", "could not reach the server: %s" % _short(reason))


def redact_url(url):
    """A URL fit for a log line: scheme, host and path only. Private feeds
    carry their token in the query string or as userinfo."""
    try:
        parts = urllib.parse.urlsplit(str(url or ""))
    except ValueError:
        return "<unparseable url>"
    host = parts.hostname or ""
    if parts.port:
        host = "%s:%d" % (host, parts.port)
    text = urllib.parse.urlunsplit((parts.scheme, host, parts.path, "", ""))
    return text + "?\u2026" if parts.query else text


def _charset(content_type):
    for part in content_type.split(";")[1:]:
        key, _, value = part.strip().partition("=")
        if key.lower() == "charset" and value:
            return value.strip().strip('"').strip("'")
    return None


def _dirname(path):
    return os.path.dirname(os.path.abspath(path)) or "."


def _http_message(error):
    """urllib reports a redirect chain over the limit as a 3xx 'infinite
    loop'; say what happened in one line."""
    reason = str(error.reason or "")
    if 300 <= error.code < 400:
        return "too many redirects (more than %d)" % MAX_REDIRECTS
    if error.code == 403:
        return "server answered 403 Forbidden (the host refuses this client)"
    return "server answered %d %s" % (error.code, reason.splitlines()[0] if reason else "")


def _short(error):
    text = str(error)
    return text if len(text) < 160 else text[:157] + "..."
