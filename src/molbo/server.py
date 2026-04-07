"""Lightweight HTTP server that serves the Mol* viewer and the structure file."""

from __future__ import annotations

import mimetypes
import socket
import time
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

_VIEWER_TEMPLATE = (Path(__file__).parent / "viewer.html").read_text(encoding="utf-8")

# ── MIME helpers ─────────────────────────────────────────────────────────────

_EXTRA_MIMES: dict[str, str] = {
    ".pdb": "chemical/x-pdb",
    ".cif": "chemical/x-cif",
    ".mmcif": "chemical/x-mmcif",
    ".bcif": "application/octet-stream",
}


def _guess_mime(path: Path) -> str:
    ext = path.suffix.lower()
    if ext in _EXTRA_MIMES:
        return _EXTRA_MIMES[ext]
    mime, _ = mimetypes.guess_type(path.name)
    return mime or "application/octet-stream"


# ── Request handler ──────────────────────────────────────────────────────────


class _Handler(BaseHTTPRequestHandler):
    """Serves exactly two endpoints: ``/`` (viewer) and ``/structure`` (file)."""

    structure_path: Path
    format_key: str
    html_cache: str | None = None
    shutdown_event: threading.Event | None = None
    last_heartbeat: float = 0.0

    # Silence default request logging
    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        pass

    # ── routing ──────────────────────────────────────────────────────────

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/":
            self._serve_viewer()
        elif self.path == "/structure":
            self._serve_structure()
        elif self.path == "/heartbeat":
            self._serve_heartbeat()
        elif self.path == "/bye":
            self._serve_bye()
        else:
            self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:  # noqa: N802
        if self.path == "/bye":
            self._serve_bye()
        else:
            self.send_error(HTTPStatus.NOT_FOUND)

    # ── viewer page ──────────────────────────────────────────────────────

    def _serve_viewer(self) -> None:
        if self.html_cache is None:
            port = self.server.server_address[1]
            type(self).html_cache = _VIEWER_TEMPLATE.replace(
                "{{ title }}", self.structure_path.name
            ).replace(
                "{{ filename }}", self.structure_path.name
            ).replace(
                "{{ format }}", self.format_key
            ).replace(
                "{{ file_url }}", f"http://localhost:{port}/structure"
            )
        body = self.html_cache.encode()  # type: ignore[union-attr]
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._cors_headers()
        self.end_headers()
        self.wfile.write(body)

    # ── structure file ───────────────────────────────────────────────────

    def _serve_structure(self) -> None:
        try:
            data = self.structure_path.read_bytes()
        except OSError:
            self.send_error(HTTPStatus.INTERNAL_SERVER_ERROR, "Cannot read structure file")
            return
        mime = _guess_mime(self.structure_path)
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(data)))
        self._cors_headers()
        self.end_headers()
        self.wfile.write(data)

    # ── helpers ──────────────────────────────────────────────────────────

    # ── heartbeat ────────────────────────────────────────────────────────

    def _serve_heartbeat(self) -> None:
        type(self).last_heartbeat = time.monotonic()
        self.send_response(HTTPStatus.NO_CONTENT)
        self._cors_headers()
        self.end_headers()

    def _serve_bye(self) -> None:
        """Immediate shutdown triggered by browser tab close."""
        self.send_response(HTTPStatus.NO_CONTENT)
        self._cors_headers()
        self.end_headers()
        if self.shutdown_event is not None:
            self.shutdown_event.set()

    def _cors_headers(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")


# ── Public API ───────────────────────────────────────────────────────────────


def find_free_port() -> int:
    """Return an available TCP port on localhost."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def make_server(
    structure_path: Path,
    format_key: str,
    port: int,
    shutdown_event: threading.Event | None = None,
) -> HTTPServer:
    """Create (but don't start) an HTTPServer for *structure_path*."""
    handler = type("Handler", (_Handler,), {
        "structure_path": structure_path,
        "format_key": format_key,
        "html_cache": None,
        "shutdown_event": shutdown_event,
        "last_heartbeat": time.monotonic(),
    })
    server = HTTPServer(("127.0.0.1", port), handler)
    return server


def start_heartbeat_watchdog(
    handler_cls: type,
    server: HTTPServer,
    shutdown_event: threading.Event,
    timeout: float = 1.5,
) -> threading.Thread:
    """Monitor heartbeats; trigger shutdown when the browser tab closes."""
    def _watch() -> None:
        while not shutdown_event.is_set():
            elapsed = time.monotonic() - handler_cls.last_heartbeat
            if elapsed > timeout:
                shutdown_event.set()
                return
            shutdown_event.wait(timeout=0.5)

    t = threading.Thread(target=_watch, daemon=True)
    t.start()
    return t


def serve_blocking(server: HTTPServer) -> None:
    """Run the server until interrupted."""
    server.serve_forever()


def serve_background(server: HTTPServer) -> threading.Thread:
    """Start the server in a daemon thread and return the thread."""
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return t
