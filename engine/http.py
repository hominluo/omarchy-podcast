"""HTTP fetching with the guard rails a feed reader needs.

urllib only. Every fetch is bounded (timeout, byte cap, redirect count), only
http(s) is ever followed and an https link is never followed down to plain
http, gzip is handled whether or not the server said so, and conditional
requests (ETag / Last-Modified) turn an unchanged feed into a cheap 304.
Blocking by design: callers run it in a worker thread.
"""

import http.client
import io
import ipaddress
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


def _port(parts):
    try:
        return parts.port
    except ValueError:
        return None


def _host_addresses(host, port):
    """The literal or resolved addresses of `host`; [] when it does not
    resolve at all."""
    text = str(host or "").strip("[]")
    if not text:
        return []
    try:
        return [ipaddress.ip_address(text.split("%", 1)[0])]
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(text, port or 0, proto=socket.IPPROTO_TCP)
    except (OSError, UnicodeError):
        return []
    addresses = []
    for info in infos:
        try:
            addresses.append(ipaddress.ip_address(str(info[4][0]).split("%", 1)[0]))
        except ValueError:
            continue
    return addresses


def _private_host(host, port):
    """True when `host` is (or resolves to) an address outside the public
    internet. A name that does not resolve is treated as public: the
    connection will fail on its own."""
    addresses = _host_addresses(host, port)
    if not str(host or "").strip("[]"):
        return True
    return bool(addresses) and any(not address.is_global for address in addresses)


def url_is_local(url):
    """True when `url` points at loopback or another non-public address.

    Callers use this to keep a feed the user typed as the only source
    allowed to hand out local URLs; a remote feed pointing an artwork or
    enclosure link at the LAN is a different thing."""
    try:
        parts = urllib.parse.urlsplit(str(url or ""))
    except ValueError:
        return True
    return _private_host(parts.hostname, _port(parts))


def _destination_allowed(host, port, allow_local):
    """True when every address `host` resolves to is public, or loopback
    and the caller allows it. An unresolvable name is left to fail on its
    own; an empty one is refused."""
    text = str(host or "").strip("[]")
    if not text:
        return False
    addresses = _host_addresses(text, port)
    if not addresses:
        return True
    for address in addresses:
        if address.is_global:
            continue
        if allow_local and address.is_loopback:
            continue
        return False
    return True


def _private_redirect(origin_url, target_url, allow_local=True):
    """True when a redirect would take a public feed onto a private network.

    Direct subscriptions to a local feed are allowed (the user typed that
    URL); the attack is a remote feed bouncing the fetch to loopback, a LAN
    address or a cloud metadata endpoint. With allow_local=False the whole
    chain must stay public, including loopback."""
    try:
        origin = urllib.parse.urlsplit(origin_url)
        target = urllib.parse.urlsplit(target_url)
    except ValueError:
        return True
    if not allow_local:
        return not _destination_allowed(target.hostname, _port(target), allow_local=False)
    if _private_host(origin.hostname, _port(origin)):
        return False
    return _private_host(target.hostname, _port(target))


def _check_destination(host, port, allow_local):
    """Resolve `host` before connecting and refuse a private destination.

    First half of the connection-time check: a name must not resolve to
    loopback, the LAN or a metadata address. The peer address is checked
    again after connect, which closes the DNS-rebinding window between the
    two lookups."""
    if not _destination_allowed(host, port, allow_local):
        raise FetchError("bad-url", "the link points at a private address")


def _check_peer(sock, allow_local):
    """Second half of the connection-time check: the address the socket
    actually reached, after DNS has done whatever it is going to do."""
    try:
        peer = sock.getpeername()[0]
    except OSError:
        return
    try:
        address = ipaddress.ip_address(str(peer).split("%", 1)[0])
    except ValueError:
        return
    if address.is_global or (allow_local and address.is_loopback):
        return
    raise FetchError("bad-url", "the link connects to a private address")


class _CheckedHTTPConnection(http.client.HTTPConnection):
    """An HTTP connection that validates its real destination."""

    def __init__(self, *args, allow_local=True, **kwargs):
        super().__init__(*args, **kwargs)
        self.allow_local = allow_local

    def connect(self):
        _check_destination(self.host, self.port, self.allow_local)
        super().connect()
        try:
            _check_peer(self.sock, self.allow_local)
        except BaseException:
            self.close()
            raise


class _CheckedHTTPSConnection(http.client.HTTPSConnection):
    """The TLS half of _CheckedHTTPConnection; the peer is checked after
    the handshake, before any request is written."""

    def __init__(self, *args, allow_local=True, **kwargs):
        super().__init__(*args, **kwargs)
        self.allow_local = allow_local

    def connect(self):
        _check_destination(self.host, self.port, self.allow_local)
        super().connect()
        try:
            _check_peer(self.sock, self.allow_local)
        except BaseException:
            self.close()
            raise


class _CheckedHTTPHandler(urllib.request.HTTPHandler):
    def http_open(self, req):
        return self.do_open(_CheckedHTTPConnection, req,
                            allow_local=getattr(req, "allow_local", True))


class _CheckedHTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, req):
        return self.do_open(_CheckedHTTPSConnection, req,
                            context=self._context,
                            allow_local=getattr(req, "allow_local", True))


class _Redirects(urllib.request.HTTPRedirectHandler):
    """Follow a few redirects, only to http(s), never from https down to
    plain http, never from a public feed to a private address, and never
    carry credentials to another host."""

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
        allow_local = getattr(req, "allow_local", True)
        if _private_redirect(req.full_url, newurl, allow_local):
            raise FetchError("bad-url", "the server redirected to a private address; refusing", status=code)
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is None:
            return None
        # The next hop reuses the same policy: a feed-controlled URL cannot
        # bounce onto loopback halfway through its redirect chain.
        new.allow_local = allow_local
        if target.netloc.lower() != origin.netloc.lower():
            for name in list(new.headers.keys()):
                if name.lower() in SENSITIVE_HEADERS:
                    del new.headers[name]
            for name in list(new.unredirected_hdrs.keys()):
                if name.lower() in SENSITIVE_HEADERS:
                    del new.unredirected_hdrs[name]
        return new


def _literal_address(host):
    try:
        return ipaddress.ip_address(str(host or "").strip("[]").split("%", 1)[0])
    except ValueError:
        return None


def check_url(url, allow_local=True):
    """Validate and normalise a URL for urllib: http(s) only, no control
    characters, and the path/query percent-encoded (feeds ship enclosure
    URLs with literal spaces, which http.client rejects outright).

    Literal private addresses are refused, except loopback when
    `allow_local` is true: a local feed (or the test harness) is a
    supported thing, a feed pointing downloads at a LAN address or the
    cloud metadata endpoint is not. Feed-controlled links pass
    allow_local=False, and names are resolved at connection time."""
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
    address = _literal_address(parts.hostname)
    if address is not None and not address.is_global:
        if not (allow_local and address.is_loopback):
            raise FetchError("bad-url", "the link points at a private address")
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
    return urllib.request.build_opener(_CheckedHTTPHandler(), _CheckedHTTPSHandler(), handler)


def fetch(url, cap=SMALL_CAP, timeout=DEFAULT_TIMEOUT, etag=None, last_modified=None,
          accept="*/*", extra_headers=None, method="GET", data=None, allow_local=True):
    """Fetch `url` into memory (up to `cap` bytes). Raises NotModified on 304,
    FetchError on anything else that went wrong.

    `allow_local` is the trust boundary for feed-supplied URLs: the feed the
    user typed may live on loopback, but artwork, enclosures and chapters a
    remote feed names must stay on the public internet."""
    url = check_url(url, allow_local=allow_local)
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
    request.allow_local = allow_local
    try:
        with _opener().open(request, timeout=timeout) as response:
            body = _read_capped(response, cap)
            return Response(response.geturl(), response.status, response.headers, body)
    except FetchError:
        raise
    except urllib.error.HTTPError as error:
        message = _http_message(error)
        code = error.code
        error.close()
        if code == 304:
            raise NotModified()
        raise FetchError("http", message, status=code)
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


def open_stream(url, timeout=DEFAULT_TIMEOUT, headers=None, method="GET", allow_local=True):
    """Open a streaming response for large bodies (audio, models). The caller
    reads and closes it. Same error mapping as fetch()."""
    url = check_url(url, allow_local=allow_local)
    merged = {"User-Agent": USER_AGENT, "Accept": "*/*"}
    if headers:
        merged.update(headers)
    request = urllib.request.Request(url, headers=merged, method=method)
    request.allow_local = allow_local
    try:
        return _opener().open(request, timeout=timeout)
    except FetchError:
        raise
    except urllib.error.HTTPError as error:
        if error.code == 416:
            error.close()
            raise FetchError("http", "range not satisfiable", status=416)
        message = _http_message(error)
        code = error.code
        error.close()
        raise FetchError("http", message, status=code)
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


def probe_headers(url, timeout=20, allow_local=True):
    """HEAD `url` and return the first hop's headers without following a
    redirect. Hugging Face, for one, puts the pinned object's size and
    digest on the 302 that sends a client to its CDN; the final response
    no longer carries them."""
    url = check_url(url, allow_local=allow_local)
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT}, method="HEAD")
    request.allow_local = allow_local
    try:
        opener = urllib.request.build_opener(_CheckedHTTPHandler(), _CheckedHTTPSHandler(), _NoRedirect())
        with opener.open(request, timeout=timeout) as response:
            return response.headers
    except urllib.error.HTTPError as error:
        if 300 <= error.code < 400:
            headers = error.headers
            error.close()
            return headers
        message = _http_message(error)
        code = error.code
        error.close()
        raise FetchError("http", message, status=code)
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
