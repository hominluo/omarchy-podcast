"""Test doubles: an HTTP server for feeds/audio and an engine harness."""

import asyncio
import http.server
import json
import os
import tempfile
import threading
import urllib.parse

from engine.config import Paths
from engine.engine import Engine

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN_DIR = os.path.dirname(HERE)


class FakeHttpServer:
    """Serves in-memory bodies; supports ETag/304, Range and gzip flags.

    routes: {path: {"body": bytes, "type": str, "etag": str|None, "status": int}}
    Every request is appended to `requests` as (method, path, headers).
    """

    def __init__(self):
        self.routes = {}
        self.json_routes = {}      # path -> fn(method, params, body) -> dict
        self.requests = []
        self.fail_after_bytes = None
        server = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def _json(self, method):
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                path, _, query = self.path.partition("?")
                params = dict(urllib.parse.parse_qsl(query))
                body = json.loads(raw) if raw else None
                server.requests.append((method, self.path, dict(self.headers), body))
                handler = server.json_routes.get(path)
                if handler is None:
                    self.send_response(404)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                try:
                    result = handler(method, params, body)
                    status = 200
                except PermissionError:
                    result, status = {"error": "auth"}, 401
                payload = json.dumps(result).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def do_POST(self):
                self._json("POST")

            def _serve(self, send_body):
                if self.path.split("?")[0] in server.json_routes:
                    self._json(self.command)
                    return
                server.requests.append((self.command, self.path, dict(self.headers)))
                route = server.routes.get(self.path.split("?")[0])
                if route is None:
                    self.send_response(404)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                status = route.get("status", 200)
                extra = route.get("extra_headers") or {}
                if status != 200:
                    self.send_response(status)
                    self.send_header("Content-Length", "0")
                    for name, value in extra.items():
                        self.send_header(name, value)
                    self.end_headers()
                    return
                body = route["body"]
                etag = route.get("etag")
                if etag and self.headers.get("If-None-Match") == etag:
                    self.send_response(304)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                start = 0
                rng = self.headers.get("Range")
                if rng and rng.startswith("bytes=") and route.get("ranges", True):
                    start = int(rng[6:].split("-")[0])
                    self.send_response(206)
                    # bad_range: claim the wrong starting offset (a broken CDN)
                    claimed = 0 if route.get("bad_range") else start
                    self.send_header("Content-Range", "bytes %d-%d/%d" % (claimed, len(body) - 1, len(body)))
                else:
                    self.send_response(200)
                chunk = body[start:]
                if not route.get("content_length", True):
                    self.close_connection = True      # length unknown: end with the connection
                elif server.fail_after_bytes is not None and len(chunk) > server.fail_after_bytes:
                    self.send_header("Content-Length", str(len(body) - start))
                else:
                    self.send_header("Content-Length", str(len(chunk)))
                if server.fail_after_bytes is not None and len(chunk) > server.fail_after_bytes:
                    chunk = chunk[:server.fail_after_bytes]
                self.send_header("Content-Type", route.get("type", "application/octet-stream"))
                if etag:
                    self.send_header("ETag", etag)
                self.send_header("Accept-Ranges", "bytes")
                for name, value in extra.items():
                    self.send_header(name, value)
                self.end_headers()
                if send_body:
                    try:
                        self.wfile.write(chunk)
                    except (BrokenPipeError, ConnectionResetError):
                        pass
                if server.fail_after_bytes is not None:
                    self.close_connection = True

            def do_GET(self):
                self._serve(True)

            def do_HEAD(self):
                self._serve(False)

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    @property
    def base(self):
        return "http://127.0.0.1:%d" % self.httpd.server_address[1]

    def url(self, path):
        return self.base + path

    def add(self, path, body, content_type="application/octet-stream", etag=None, status=200, ranges=True,
            extra_headers=None, content_length=True, bad_range=False):
        self.routes[path] = {"body": body, "type": content_type, "etag": etag, "status": status, "ranges": ranges,
                             "extra_headers": extra_headers, "content_length": content_length, "bad_range": bad_range}
        return self.url(path)

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class EngineHarness:
    """A real Engine on temporary paths. `attach` picks the subsystems."""

    def __init__(self, attach=None):
        self.tmp = tempfile.TemporaryDirectory()
        base = self.tmp.name
        self.paths = Paths(PLUGIN_DIR)
        self.paths.runtime_dir = base
        self.paths.state_dir = base
        self.paths.cache_dir = os.path.join(base, "cache")
        self.paths.config_dir = os.path.join(base, "config")
        self.paths.socket_path = os.path.join(base, "d.sock")
        self.paths.lock_path = os.path.join(base, "d.lock")
        self.paths.info_path = os.path.join(base, "d.json")
        self.paths.mpv_socket_path = os.path.join(base, "mpv.sock")
        self.paths.mpv_pid_path = os.path.join(base, "mpv.pid")
        self.paths.db_path = os.path.join(base, "t.db")
        self.paths.log_path = os.path.join(base, "t.log")
        self.paths.credentials_path = os.path.join(base, "config", "credentials.json")
        for name in ("artwork_dir", "transcripts_dir", "chapters_dir", "audio_dir", "models_dir", "thumbs_dir", "pycache_dir"):
            setattr(self.paths, name, os.path.join(base, "cache", name))
        self.paths.ensure()
        self.engine = Engine(self.paths)
        if attach:
            attach(self.engine)
        self.task = None
        self.events = []
        original_emit = self.engine.emit

        def capture(event, data):
            self.events.append((event, data))
            original_emit(event, data)
        self.engine.emit = capture

    async def __aenter__(self):
        self.task = asyncio.ensure_future(self.engine.run())
        for _ in range(300):
            if os.path.exists(self.paths.socket_path) and self.engine.store is not None:
                break
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.05)
        return self

    async def __aexit__(self, *exc):
        self.engine.request_stop()
        await asyncio.wait_for(self.task, 5)
        self.tmp.cleanup()

    def events_named(self, name):
        return [data for event, data in self.events if event == name]
