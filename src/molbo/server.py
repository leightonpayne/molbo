"""Lightweight HTTP server that serves the Mol* viewer and the structure file."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import html
import json
import mimetypes
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, HTTPServer
from importlib.resources import files
from pathlib import Path
from socketserver import ThreadingMixIn
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlparse
from urllib.request import Request, urlopen

from molbo import __version__

_VIEWER_TEMPLATE = files("molbo").joinpath("viewer.html").read_text(encoding="utf-8")
_REMOTE_FETCH_ATTEMPTS = 2
_REMOTE_FETCH_RETRY_DELAY = 0.25
_STREAM_CHUNK_SIZE = 64 * 1024


@dataclass(frozen=True)
class VendoredAsset:
    package_name: str
    content_type: str


_VENDORED_ASSETS: dict[str, VendoredAsset] = {
    "/assets/molstar-4.5.0.css": VendoredAsset("molstar-4.5.0.css", "text/css; charset=utf-8"),
    "/assets/molstar-4.5.0.js": VendoredAsset("molstar-4.5.0.js", "application/javascript; charset=utf-8"),
    "/assets/qrcode-generator-2.0.4.min.js": VendoredAsset(
        "qrcode-generator-2.0.4.min.js",
        "application/javascript; charset=utf-8",
    ),
}

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
    fetch_timeout: float = 30.0

    @property
    def is_remote(self) -> bool:
        return self.remote_url is not None


@lru_cache(maxsize=None)
def _load_vendored_asset(package_name: str) -> bytes:
    return files("molbo").joinpath("vendor", package_name).read_bytes()


def _guess_mime(name: str) -> str:
    suffixes = [suffix.lower() for suffix in Path(name).suffixes]
    ext = suffixes[-2] if suffixes[-1:] == [".gz"] and len(suffixes) >= 2 else suffixes[-1]
    if ext in _EXTRA_MIMES:
        return _EXTRA_MIMES[ext]
    mime, _ = mimetypes.guess_type(name.removesuffix(".gz"))
    return mime or "application/octet-stream"


def _guess_content_encoding(name: str) -> str | None:
    return "gzip" if name.lower().endswith(".gz") else None


def format_host_for_url(host: str) -> str:
    """Return *host* in a form suitable for an http:// URL."""
    if ":" in host and not host.startswith("["):
        return f"[{host}]"
    return host


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
    share_url: str | None = None,
) -> str:
    """Render the viewer template with escaped HTML and JSON-safe script values."""
    replacements = {
        "{{ title_html }}": html.escape(structure_name, quote=True),
        "{{ filename_html }}": html.escape(structure_name, quote=True),
        "{{ format_label_html }}": html.escape(format_key, quote=True),
        "{{ format_json }}": json.dumps(format_key),
        "{{ file_url_json }}": json.dumps(file_url),
        "{{ auto_close_json }}": json.dumps(auto_close),
        "{{ share_url_json }}": json.dumps(share_url),
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
    share_url: str | None = None
    last_heartbeat: float = 0.0

    # Silence default request logging
    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        pass

    # ── routing ──────────────────────────────────────────────────────────

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path == "/":
            self._serve_viewer()
        elif path == "/structure":
            self._serve_structure()
        elif path in _VENDORED_ASSETS:
            self._serve_asset(path)
        elif path == "/heartbeat":
            self._serve_heartbeat()
        elif path == "/bye":
            self._serve_bye()
        else:
            self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:  # noqa: N802
        if urlparse(self.path).path == "/bye":
            self._serve_bye()
        else:
            self.send_error(HTTPStatus.NOT_FOUND)

    # ── viewer page ──────────────────────────────────────────────────────

    def _serve_viewer(self) -> None:
        if self.html_cache is None:
            type(self).html_cache = render_viewer_html(
                structure_name=self.structure_source.display_name,
                format_key=self.structure_source.format_key,
                file_url="/structure",
                auto_close=self.auto_close,
                share_url=self.share_url,
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

    def _serve_asset(self, path: str) -> None:
        asset = _VENDORED_ASSETS[path]
        body = _load_vendored_asset(asset.package_name)
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", asset.content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "public, max-age=31536000, immutable")
        self.end_headers()
        self.wfile.write(body)

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
                while chunk := handle.read(_STREAM_CHUNK_SIZE):
                    self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError):
            return
        except OSError:
            self.close_connection = True

    def _serve_remote_structure(self) -> None:
        assert self.structure_source.remote_url is not None
        try:
            with self._open_remote_structure() as response:
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

                while chunk := response.read(_STREAM_CHUNK_SIZE):
                    self.wfile.write(chunk)
        except HTTPError as exc:
            detail = str(exc.reason) if exc.reason else "request failed"
            self.send_error(exc.code, f"Upstream server returned {exc.code}: {detail}")
        except TimeoutError:
            self.send_error(HTTPStatus.GATEWAY_TIMEOUT, "Remote structure request timed out")
        except URLError as exc:
            detail = str(exc.reason) if exc.reason else "connection failed"
            self.send_error(HTTPStatus.BAD_GATEWAY, f"Failed to fetch remote structure: {detail}")
        except (BrokenPipeError, ConnectionResetError):
            return

    def _open_remote_structure(self):
        assert self.structure_source.remote_url is not None
        last_error: URLError | TimeoutError | None = None
        for attempt in range(_REMOTE_FETCH_ATTEMPTS):
            request = Request(
                self.structure_source.remote_url,
                headers={
                    "User-Agent": f"molbo/{__version__}",
                    "Accept": "*/*",
                },
            )
            try:
                return urlopen(request, timeout=self.structure_source.fetch_timeout)
            except HTTPError:
                raise
            except (TimeoutError, URLError) as exc:
                last_error = exc
                if attempt + 1 == _REMOTE_FETCH_ATTEMPTS:
                    raise
                time.sleep(_REMOTE_FETCH_RETRY_DELAY * (attempt + 1))

        raise RuntimeError(f"unreachable remote fetch state: {last_error}")

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
    host: str = "127.0.0.1",
    shutdown_event: threading.Event | None = None,
    auto_close: bool = False,
    share_url: str | None = None,
) -> HTTPServer:
    """Create (but don't start) an HTTPServer for *structure_path*."""
    handler = type("Handler", (_Handler,), {
        "structure_source": structure_source,
        "html_cache": None,
        "shutdown_event": shutdown_event,
        "auto_close": auto_close,
        "share_url": share_url,
        "last_heartbeat": time.monotonic(),
    })
    server = ThreadingHTTPServer((host, port), handler)
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


def serve_background(server: HTTPServer) -> threading.Thread:
    """Start the server in a daemon thread and return the thread."""
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return t
