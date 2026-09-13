"""Test doubles: an HTTP server for feeds/audio and an engine harness."""

import asyncio
import http.server
import os
import tempfile
import threading

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
        self.requests = []
        self.fail_after_bytes = None
        server = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def _serve(self, send_body):
                server.requests.append((self.command, self.path, dict(self.headers)))
                route = server.routes.get(self.path.split("?")[0])
                if route is None:
                    self.send_response(404)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                status = route.get("status", 200)
                if status != 200:
                    self.send_response(status)
                    self.send_header("Content-Length", "0")
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
                    self.send_header("Content-Range", "bytes %d-%d/%d" % (start, len(body) - 1, len(body)))
                else:
                    self.send_response(200)
                chunk = body[start:]
                if server.fail_after_bytes is not None and len(chunk) > server.fail_after_bytes:
                    chunk = chunk[:server.fail_after_bytes]
                    self.send_header("Content-Length", str(len(body) - start))
                else:
                    self.send_header("Content-Length", str(len(chunk)))
                self.send_header("Content-Type", route.get("type", "application/octet-stream"))
                if etag:
                    self.send_header("ETag", etag)
                self.send_header("Accept-Ranges", "bytes")
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

    def add(self, path, body, content_type="application/octet-stream", etag=None, status=200, ranges=True):
        self.routes[path] = {"body": body, "type": content_type, "etag": etag, "status": status, "ranges": ranges}
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
        for name in ("artwork_dir", "transcripts_dir", "chapters_dir", "audio_dir", "models_dir", "pycache_dir"):
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
