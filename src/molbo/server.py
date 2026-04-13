"""Lightweight HTTP server that serves the Mol* viewer and the structure file."""

from __future__ import annotations

from dataclasses import dataclass
import html
import json
import mimetypes
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from socketserver import ThreadingMixIn
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlparse
from urllib.request import Request, urlopen

_VIEWER_TEMPLATE = (Path(__file__).parent / "viewer.html").read_text(encoding="utf-8")

# ── MIME helpers ─────────────────────────────────────────────────────────────

_EXTRA_MIMES: dict[str, str] = {
    ".pdb": "chemical/x-pdb",
    ".cif": "chemical/x-cif",
    ".mmcif": "chemical/x-mmcif",
    ".bcif": "application/octet-stream",
}


@dataclass(frozen=True)
class StructureSource:
    """Description of the structure input served by the local HTTP server."""

    display_name: str
    format_key: str
    local_path: Path | None = None
    remote_url: str | None = None

    @property
    def is_remote(self) -> bool:
        return self.remote_url is not None


def _guess_mime(name: str) -> str:
    suffixes = [suffix.lower() for suffix in Path(name).suffixes]
    ext = suffixes[-2] if suffixes[-1:] == [".gz"] and len(suffixes) >= 2 else suffixes[-1]
    if ext in _EXTRA_MIMES:
        return _EXTRA_MIMES[ext]
    mime, _ = mimetypes.guess_type(name.removesuffix(".gz"))
    return mime or "application/octet-stream"


def _guess_content_encoding(name: str) -> str | None:
    return "gzip" if name.lower().endswith(".gz") else None


def display_name_from_url(url: str) -> str:
    """Return a readable filename derived from a remote structure URL."""
    parsed = urlparse(url)
    path_name = Path(unquote(parsed.path)).name
    return path_name or parsed.netloc or "remote-structure"


def render_viewer_html(
    structure_name: str,
    format_key: str,
    file_url: str,
    auto_close: bool,
    viewer_style: str,
) -> str:
    """Render the viewer template with escaped HTML and JSON-safe script values."""
    replacements = {
        "{{ title_html }}": html.escape(structure_name, quote=True),
        "{{ filename_html }}": html.escape(structure_name, quote=True),
        "{{ format_label_html }}": html.escape(format_key, quote=True),
        "{{ format_json }}": json.dumps(format_key),
        "{{ file_url_json }}": json.dumps(file_url),
        "{{ auto_close_json }}": json.dumps(auto_close),
        "{{ style_json }}": json.dumps(viewer_style),
    }
    rendered = _VIEWER_TEMPLATE
    for placeholder, value in replacements.items():
        rendered = rendered.replace(placeholder, value)
    return rendered


# ── Request handler ──────────────────────────────────────────────────────────


class _Handler(BaseHTTPRequestHandler):
    """Serves exactly two endpoints: ``/`` (viewer) and ``/structure`` (file)."""

    structure_source: StructureSource
    html_cache: str | None = None
    shutdown_event: threading.Event | None = None
    auto_close: bool = False
    viewer_style: str = "publication"
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
            type(self).html_cache = render_viewer_html(
                structure_name=self.structure_source.display_name,
                format_key=self.structure_source.format_key,
                file_url=f"http://localhost:{port}/structure",
                auto_close=self.auto_close,
                viewer_style=self.viewer_style,
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
        if self.structure_source.is_remote:
            self._serve_remote_structure()
            return
        self._serve_local_structure()

    def _serve_local_structure(self) -> None:
        assert self.structure_source.local_path is not None
        try:
            size = self.structure_source.local_path.stat().st_size
        except OSError:
            self.send_error(HTTPStatus.INTERNAL_SERVER_ERROR, "Cannot read structure file")
            return
        mime = _guess_mime(self.structure_source.display_name)
        content_encoding = _guess_content_encoding(self.structure_source.display_name)
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", mime)
        if content_encoding is not None:
            self.send_header("Content-Encoding", content_encoding)
        self.send_header("Content-Length", str(size))
        self._cors_headers()
        self.end_headers()
        try:
            with self.structure_source.local_path.open("rb") as handle:
                while chunk := handle.read(64 * 1024):
                    self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError):
            return
        except OSError:
            self.close_connection = True

    def _serve_remote_structure(self) -> None:
        assert self.structure_source.remote_url is not None
        request = Request(
            self.structure_source.remote_url,
            headers={"User-Agent": "molbo/0.1.0"},
        )
        try:
            with urlopen(request, timeout=30) as response:
                self.send_response(getattr(response, "status", HTTPStatus.OK))
                content_type = response.headers.get("Content-Type") or _guess_mime(
                    self.structure_source.display_name
                )
                self.send_header("Content-Type", content_type)

                content_encoding = response.headers.get("Content-Encoding") or _guess_content_encoding(
                    self.structure_source.display_name
                )
                if content_encoding is not None:
                    self.send_header("Content-Encoding", content_encoding)

                content_length = response.headers.get("Content-Length")
                if content_length is not None:
                    self.send_header("Content-Length", content_length)

                self._cors_headers()
                self.end_headers()

                while chunk := response.read(64 * 1024):
                    self.wfile.write(chunk)
        except HTTPError as exc:
            self.send_error(exc.code, f"Upstream server returned {exc.code}")
        except URLError:
            self.send_error(HTTPStatus.BAD_GATEWAY, "Failed to fetch remote structure")
        except (BrokenPipeError, ConnectionResetError):
            return

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
        if self.auto_close and self.shutdown_event is not None:
            self.shutdown_event.set()

    def _cors_headers(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")


class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    """Small threaded HTTP server suitable for local one-user traffic."""

    daemon_threads = True


def make_server(
    structure_source: StructureSource,
    port: int,
    shutdown_event: threading.Event | None = None,
    auto_close: bool = False,
    viewer_style: str = "publication",
) -> HTTPServer:
    """Create (but don't start) an HTTPServer for *structure_path*."""
    handler = type("Handler", (_Handler,), {
        "structure_source": structure_source,
        "html_cache": None,
        "shutdown_event": shutdown_event,
        "auto_close": auto_close,
        "viewer_style": viewer_style,
        "last_heartbeat": time.monotonic(),
    })
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    return server


def start_heartbeat_watchdog(
    handler_cls: type,
    shutdown_event: threading.Event,
    timeout: float = 1.5,
) -> threading.Thread:
    """Monitor heartbeats and trigger shutdown after the idle timeout expires."""
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
