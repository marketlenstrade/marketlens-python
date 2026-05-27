from __future__ import annotations

import http.server
import socketserver
import sys
import webbrowser
from pathlib import Path

import orjson

_STATIC_DIR = Path(__file__).parent / "_static"

_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
}


class _DashboardHandler(http.server.BaseHTTPRequestHandler):
    _data_bytes: bytes = b"{}"

    def do_GET(self) -> None:
        if self.path == "/api/data":
            self._serve_json()
        elif self.path in ("/", "/index.html"):
            self._serve_static("index.html")
        else:
            self._serve_static(self.path.lstrip("/"))

    def _serve_json(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(self._data_bytes)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(self._data_bytes)

    def _serve_static(self, filename: str) -> None:
        filepath = (_STATIC_DIR / filename).resolve()
        if not filepath.is_file() or not filepath.is_relative_to(_STATIC_DIR.resolve()):
            self.send_error(404)
            return
        ext = filepath.suffix
        content_type = _CONTENT_TYPES.get(ext, "application/octet-stream")
        data = filepath.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format: str, *args: object) -> None:
        pass


def serve(data: dict, *, open_browser: bool = True) -> None:
    data_bytes = orjson.dumps(data)

    handler = type("_Handler", (_DashboardHandler,), {"_data_bytes": data_bytes})

    with socketserver.TCPServer(("127.0.0.1", 0), handler) as httpd:
        port = httpd.server_address[1]
        url = f"http://127.0.0.1:{port}"
        sys.stderr.write(f"Dashboard: {url}\n")
        sys.stderr.write("Press Ctrl+C to stop.\n")
        sys.stderr.flush()

        if open_browser:
            webbrowser.open(url)

        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            sys.stderr.write("\nDashboard stopped.\n")
