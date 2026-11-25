"""Lightweight HTTP server that serves the Mol* viewer and the structure file."""

from __future__ import annotations

import gzip
import html
import http.client
import ipaddress
import json
import mimetypes
import os
import re
import socket
import ssl
import threading
import time
from dataclasses import dataclass
from email.message import Message
from functools import cache
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, HTTPServer
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _dist_version
from importlib.resources import files
from pathlib import Path
from socketserver import TCPServer, ThreadingMixIn
from typing import Any, Literal, get_args
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, unquote, urlparse
from urllib.request import Request, urlopen

try:
    _PACKAGE_VERSION = _dist_version("molbo")
except PackageNotFoundError:  # pragma: no cover
    _PACKAGE_VERSION = "0+unknown"

_VIEWER_TEMPLATE = files("molbo").joinpath("viewer.html").read_text(encoding="utf-8")
_TEMPLATE_PLACEHOLDER_RE = re.compile(r"\{\{\s*([a-z0-9_]+)\s*\}\}")
_REMOTE_FETCH_ATTEMPTS = 2
_REMOTE_FETCH_RETRY_DELAY = 0.25
_STREAM_CHUNK_SIZE = 64 * 1024
_MAX_REMOTE_CACHE_BYTES = 50 * 1024 * 1024
_DEFAULT_IDLE_TIMEOUT = 15.0
_DEFAULT_BYE_GRACE = 2.0

_FAVICON_SVG = (
    b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24">'
    b'<rect width="24" height="24" rx="5" fill="#fcfbf9"/>'
    b'<circle cx="12" cy="12" r="2.3" fill="#6e5237"/>'
    b'<ellipse cx="12" cy="12" rx="8.5" ry="3.6" fill="none" stroke="#6e5237" stroke-width="1.5"/>'
    b'<ellipse cx="12" cy="12" rx="8.5" ry="3.6" fill="none" stroke="#6e5237" stroke-width="1.5" '
    b'transform="rotate(60 12 12)"/>'
    b'<ellipse cx="12" cy="12" rx="8.5" ry="3.6" fill="none" stroke="#6e5237" stroke-width="1.5" '
    b'transform="rotate(120 12 12)"/>'
    b"</svg>"
)

_BASE_SUFFIXES: dict[str, str] = {
    ".pdb": "pdb",
    ".ent": "pdb",
    ".cif": "cif",
    ".mmcif": "mmcif",
    ".bcif": "bcif",
    ".pdbqt": "pdbqt",
    ".sdf": "sdf",
    ".sd": "sdf",
    ".mol": "mol",
    ".mol2": "mol2",
    ".xyz": "xyz",
    ".gro": "gro",
}
SUPPORTED_SUFFIXES: dict[tuple[str, ...], str] = {
    **{(ext,): fmt for ext, fmt in _BASE_SUFFIXES.items()},
    **{(ext, ".gz"): fmt for ext, fmt in _BASE_SUFFIXES.items()},
}
SUPPORTED_EXTENSIONS = ", ".join("".join(suffixes) for suffixes in SUPPORTED_SUFFIXES)
FormatChoice = Literal["pdb", "cif", "mmcif", "bcif", "pdbqt", "sdf", "mol", "mol2", "xyz", "gro"]
SUPPORTED_FORMAT_KEYS: tuple[str, ...] = get_args(FormatChoice)

_PDB_ID_RE = re.compile(r"^[0-9][A-Za-z0-9]{3}$")
_EXTENDED_PDB_ID_RE = re.compile(r"^pdb_[A-Za-z0-9]{8}$", re.IGNORECASE)
_ALPHAFOLD_PREFIX_RE = re.compile(r"^(?:af|alphafold):([A-Za-z0-9_-]+)$", re.IGNORECASE)
_ALPHAFOLD_ENTRY_RE = re.compile(r"^AF-([A-Za-z0-9]+)-F(\d+)(?:-model_v(\d+))?$", re.IGNORECASE)

_RCSB_DOWNLOAD_URLS: dict[str, tuple[str, str, str]] = {
    "cif": ("https://files.rcsb.org/download/{pdb_id}.cif", "cif", "{pdb_id}.cif"),
    "mmcif": ("https://files.rcsb.org/download/{pdb_id}.cif", "cif", "{pdb_id}.cif"),
    "pdb": ("https://files.rcsb.org/download/{pdb_id}.pdb", "pdb", "{pdb_id}.pdb"),
    "bcif": ("https://models.rcsb.org/{pdb_id}.bcif", "bcif", "{pdb_id}.bcif"),
}
_ALPHAFOLD_FORMATS: dict[str, str] = {
    "cif": "cif",
    "mmcif": "cif",
    "pdb": "pdb",
    "bcif": "bcif",
}
_ALPHAFOLD_API_URL = "https://alphafold.ebi.ac.uk/api/prediction/{uniprot}"
_ALPHAFOLD_FILE_URL = "https://alphafold.ebi.ac.uk/files/{model_id}.{ext}"

_ALPHAFOLD_FORMAT_FIELDS: dict[str, str] = {
    "cif": "cifUrl",
    "mmcif": "cifUrl",
    "pdb": "pdbUrl",
    "bcif": "bcifUrl",
}


@dataclass(frozen=True)
class VendoredAsset:
    package_name: str
    content_type: str


@dataclass(frozen=True)
class _CachedRemoteResponse:
    status: int
    content_type: str
    content_encoding: str | None
    body: bytes


_VENDORED_ASSETS: dict[str, VendoredAsset] = {
    "/assets/molstar-4.5.0.css": VendoredAsset("molstar-4.5.0.css", "text/css; charset=utf-8"),
    "/assets/molstar-4.5.0.js": VendoredAsset(
        "molstar-4.5.0.js", "application/javascript; charset=utf-8"
    ),
    "/assets/qrcode-generator-2.0.4.min.js": VendoredAsset(
        "qrcode-generator-2.0.4.min.js",
        "application/javascript; charset=utf-8",
    ),
}

_FORMAT_MIMES: dict[str, str] = {
    "pdb": "chemical/x-pdb",
    "pdbqt": "chemical/x-pdb",
    "cif": "chemical/x-cif",
    "mmcif": "chemical/x-mmcif",
    "bcif": "application/octet-stream",
    "sdf": "chemical/x-mdl-sdfile",
    "mol": "chemical/x-mdl-molfile",
    "mol2": "chemical/x-mol2",
    "xyz": "chemical/x-xyz",
    "gro": "text/plain; charset=utf-8",
}
_EXTRA_MIMES: dict[str, str] = {
    **{f".{fmt}": mime for fmt, mime in _FORMAT_MIMES.items()},
    ".ent": _FORMAT_MIMES["pdb"],
    ".sd": _FORMAT_MIMES["sdf"],
}


@dataclass(frozen=True)
class StructureSource:
    """Description of the structure input served by the local HTTP server."""

    display_name: str
    format_key: str
    local_path: Path | None = None
    remote_url: str | None = None
    inline_bytes: bytes | None = None
    fetch_timeout: float = 30.0
    pdb_id: str | None = None
    alphafold_id: str | None = None
    alphafold_api_url: str | None = None

    @property
    def is_remote(self) -> bool:
        return self.remote_url is not None or self.alphafold_api_url is not None

    @property
    def is_inline(self) -> bool:
        return self.inline_bytes is not None

    @property
    def source_kind(self) -> str:
        if self.inline_bytes is not None:
            return "stdin"
        if self.pdb_id is not None:
            return "pdb"
        if self.alphafold_id is not None:
            return "alphafold"
        if self.is_remote:
            return "remote"
        return "local"


def resolve_structure_format(file: Path) -> str | None:
    suffixes = tuple(suffix.lower() for suffix in file.suffixes)
    for width in (2, 1):
        key = suffixes[-width:]
        if key in SUPPORTED_SUFFIXES:
            return SUPPORTED_SUFFIXES[key]
    return None


def looks_like_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def looks_like_pdb_id(value: str) -> bool:
    return (
        _PDB_ID_RE.fullmatch(value) is not None or _EXTENDED_PDB_ID_RE.fullmatch(value) is not None
    )


def looks_like_alphafold_id(value: str) -> bool:
    return (
        _ALPHAFOLD_PREFIX_RE.fullmatch(value) is not None
        or _ALPHAFOLD_ENTRY_RE.fullmatch(value) is not None
    )


def pdb_id_to_structure_source(
    value: str,
    fetch_timeout: float = 30.0,
    format_override: str | None = None,
) -> StructureSource:
    requested_format = (format_override or "cif").lower()
    if requested_format not in _RCSB_DOWNLOAD_URLS:
        raise ValueError("Unsupported --format for PDB IDs. Choose from cif, mmcif, pdb, or bcif.")

    is_extended = _EXTENDED_PDB_ID_RE.fullmatch(value) is not None
    if is_extended:
        pdb_id = value.lower()
        if requested_format == "pdb":
            if pdb_id.startswith("pdb_0000"):
                short_id = pdb_id[8:].upper()
                return StructureSource(
                    display_name=f"{short_id}.pdb",
                    format_key="pdb",
                    remote_url=_RCSB_DOWNLOAD_URLS["pdb"][0].format(pdb_id=short_id),
                    fetch_timeout=fetch_timeout,
                    pdb_id=pdb_id,
                )
            raise ValueError(
                "Extended PDB IDs only support cif, mmcif, or bcif downloads from RCSB."
            )
    else:
        pdb_id = value.upper()

    url_template, format_key, name_template = _RCSB_DOWNLOAD_URLS[requested_format]
    return StructureSource(
        display_name=name_template.format(pdb_id=pdb_id),
        format_key=format_key,
        remote_url=url_template.format(pdb_id=pdb_id),
        fetch_timeout=fetch_timeout,
        pdb_id=pdb_id,
    )


def alphafold_id_to_structure_source(
    value: str,
    fetch_timeout: float = 30.0,
    format_override: str | None = None,
) -> StructureSource:
    requested_format = (format_override or "cif").lower()
    if requested_format not in _ALPHAFOLD_FORMATS:
        raise ValueError(
            "Unsupported --format for AlphaFold DB IDs. Choose from cif, mmcif, pdb, or bcif."
        )
    format_key = _ALPHAFOLD_FORMATS[requested_format]

    prefix_match = _ALPHAFOLD_PREFIX_RE.fullmatch(value)
    if prefix_match is not None:
        inner = prefix_match.group(1)
        entry_match = _ALPHAFOLD_ENTRY_RE.fullmatch(inner)
        if entry_match is None:
            uniprot = inner.upper()
            return StructureSource(
                display_name=f"AF-{uniprot}.{format_key}",
                format_key=format_key,
                fetch_timeout=fetch_timeout,
                alphafold_id=uniprot,
                alphafold_api_url=_ALPHAFOLD_API_URL.format(uniprot=uniprot),
            )
    else:
        entry_match = _ALPHAFOLD_ENTRY_RE.fullmatch(value)

    assert entry_match is not None
    uniprot = entry_match.group(1).upper()
    fragment = entry_match.group(2)
    version = entry_match.group(3)
    entry_id = f"AF-{uniprot}-F{fragment}"
    if version is not None:
        model_id = f"{entry_id}-model_v{version}"
        return StructureSource(
            display_name=f"{model_id}.{format_key}",
            format_key=format_key,
            remote_url=_ALPHAFOLD_FILE_URL.format(model_id=model_id, ext=format_key),
            fetch_timeout=fetch_timeout,
            alphafold_id=model_id,
        )
    return StructureSource(
        display_name=f"{entry_id}.{format_key}",
        format_key=format_key,
        fetch_timeout=fetch_timeout,
        alphafold_id=entry_id,
        alphafold_api_url=_ALPHAFOLD_API_URL.format(uniprot=uniprot),
    )


def resolve_source(
    value: str | Path,
    *,
    fetch_timeout: float = 30.0,
    format_override: str | None = None,
) -> StructureSource | None:
    """Resolve a path, URL, PDB ID, or AlphaFold DB ID into a StructureSource.

    Returns ``None`` when a local file or URL lacks a recognized structure suffix and
    *format_override* is not given. Raises ``IsADirectoryError``, ``FileNotFoundError``,
    ``OSError``, or ``ValueError`` for invalid inputs.
    """
    raw = str(value)
    fmt_override = format_override.lower() if format_override is not None else None
    if fmt_override is not None and fmt_override not in SUPPORTED_FORMAT_KEYS:
        raise ValueError(
            f"Unsupported format {format_override!r}. Choose from {', '.join(SUPPORTED_FORMAT_KEYS)}."
        )

    if looks_like_url(raw):
        display_name = display_name_from_url(raw)
        fmt = fmt_override or resolve_structure_format(Path(display_name))
        if fmt is None:
            return None
        return StructureSource(
            display_name=display_name,
            format_key=fmt,
            remote_url=raw,
            fetch_timeout=fetch_timeout,
        )

    path = Path(raw).expanduser().resolve()
    if path.exists():
        if path.is_dir():
            raise IsADirectoryError(raw)
        if not path.is_file():
            raise OSError(f"Path is not a regular file: {raw}")
        fmt = fmt_override or resolve_structure_format(path)
        if fmt is None:
            return None
        return StructureSource(display_name=path.name, format_key=fmt, local_path=path)

    if looks_like_pdb_id(raw):
        return pdb_id_to_structure_source(raw, fetch_timeout, format_override=fmt_override)

    if looks_like_alphafold_id(raw):
        return alphafold_id_to_structure_source(raw, fetch_timeout, format_override=fmt_override)

    raise FileNotFoundError(raw)


@cache
def _load_vendored_asset(package_name: str) -> bytes:
    return files("molbo").joinpath("vendor").joinpath(package_name).read_bytes()


@cache
def _load_vendored_asset_gzip(package_name: str) -> bytes:
    return gzip.compress(_load_vendored_asset(package_name), compresslevel=6)


def _accepts_gzip(header_value: str | None) -> bool:
    """Return True if the HTTP Accept-Encoding header permits gzip."""
    if not header_value:
        return False
    for part in header_value.split(","):
        tokens = [token.strip().lower() for token in part.split(";") if token.strip()]
        if not tokens or tokens[0] != "gzip":
            continue
        q_value = 1.0
        for param in tokens[1:]:
            if param.startswith("q="):
                try:
                    q_value = float(param[2:])
                except ValueError:
                    q_value = 0.0
        if q_value > 0.0:
            return True
    return False


def _guess_mime(name: str, format_key: str | None = None) -> str:
    suffixes = [suffix.lower() for suffix in Path(name).suffixes]
    if suffixes:
        ext = suffixes[-2] if suffixes[-1:] == [".gz"] and len(suffixes) >= 2 else suffixes[-1]
        if ext in _EXTRA_MIMES:
            return _EXTRA_MIMES[ext]
    mime, _ = mimetypes.guess_type(name.removesuffix(".gz"))
    return mime or _FORMAT_MIMES.get(format_key or "", "application/octet-stream")


def _guess_content_encoding(name: str) -> str | None:
    return "gzip" if name.lower().endswith(".gz") else None


def _is_gzip_bytes(data: bytes) -> bool:
    return data.startswith(b"\x1f\x8b")


def local_file_signature(path: Path) -> str | None:
    """Return a lightweight change signature for a local file, or ``None`` if unavailable."""
    try:
        st = path.stat()
    except OSError:
        return None
    return f"{st.st_mtime_ns}:{st.st_size}"


def _json_for_script(value: object) -> str:
    """Serialize *value* as JSON safe for embedding inside an HTML ``<script>`` tag."""
    return (
        json.dumps(value)
        .replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


def _is_timeout_error(exc: BaseException) -> bool:
    if isinstance(exc, TimeoutError):
        return True
    return isinstance(exc, URLError) and isinstance(exc.reason, TimeoutError)


def _is_non_retryable_url_error(exc: URLError) -> bool:
    return isinstance(exc.reason, ssl.SSLError)


def format_host_for_url(host: str) -> str:
    """Return *host* in a form suitable for an http:// URL."""
    stripped = host.strip("[]")
    if ":" in stripped:
        return f"[{stripped}]"
    return stripped


def local_access_host(host: str) -> str:
    """Return a locally routable host for opening a browser when binding wildcard addresses."""
    stripped = host.strip("[]")
    if stripped in {"", "0.0.0.0"}:
        return "127.0.0.1"
    if stripped == "::":
        return "::1"
    return stripped


def detect_lan_host(host: str) -> str | None:
    """Detect a non-loopback LAN address when binding a wildcard host."""
    stripped = host.strip("[]")
    if stripped in {"", "0.0.0.0"}:
        family = socket.AF_INET
        probe_target = ("192.0.2.1", 80)
    elif stripped == "::":
        family = socket.AF_INET6
        probe_target = ("2001:db8::1", 80)
    else:
        return None

    try:
        with socket.socket(family, socket.SOCK_DGRAM) as sock:
            sock.connect(probe_target)
            detected = sock.getsockname()[0]
    except OSError:
        return None

    try:
        parsed = ipaddress.ip_address(detected.split("%", 1)[0])
    except ValueError:
        return None
    if parsed.is_loopback or parsed.is_unspecified:
        return None
    return str(parsed)


def _resolve_address_family(host: str) -> socket.AddressFamily:
    stripped = host.strip("[]")
    if not stripped:
        return socket.AF_INET
    try:
        address = ipaddress.ip_address(stripped)
    except ValueError:
        try:
            infos = socket.getaddrinfo(stripped, None, type=socket.SOCK_STREAM)
        except OSError:
            return socket.AF_INET
        if infos and all(info[0] == socket.AF_INET6 for info in infos):
            return socket.AF_INET6
        return socket.AF_INET
    return socket.AF_INET6 if address.version == 6 else socket.AF_INET


def display_name_from_url(url: str) -> str:
    """Return a readable filename derived from a remote structure URL."""
    parsed = urlparse(url)
    path_name = Path(unquote(parsed.path)).name
    return path_name or parsed.netloc or "remote-structure"


def compute_heartbeat_interval_ms(idle_timeout: float) -> int:
    """Compute a client heartbeat interval shorter than *idle_timeout*."""
    return min(5000, max(250, int(idle_timeout * 1000 / 3)))


def render_viewer_html(
    structure_name: str,
    format_key: str,
    file_url: str = "./structure",
    auto_close: bool = False,
    share_url: str | None = None,
    molstar_css_url: str = "./assets/molstar-4.5.0.css",
    molstar_js_url: str = "./assets/molstar-4.5.0.js",
    qrcode_js_url: str = "./assets/qrcode-generator-2.0.4.min.js",
    heartbeat_url: str = "./heartbeat",
    bye_url: str = "./bye",
    heartbeat_interval_ms: int = 5000,
    watch: bool = False,
    version_url: str = "./version",
    favicon_url: str = "./favicon.ico",
    fallback_share_url: str | None = None,
) -> str:
    """Render the viewer template with escaped HTML and script-safe JSON values."""
    download_name = (
        structure_name[:-3]
        if structure_name.lower().endswith(".gz") and len(structure_name) > 3
        else structure_name
    )
    replacements = {
        "title_html": html.escape(structure_name, quote=True),
        "filename_html": html.escape(structure_name, quote=True),
        "download_filename_html": html.escape(download_name, quote=True),
        "file_url_html": html.escape(file_url, quote=True),
        "favicon_url_html": html.escape(favicon_url, quote=True),
        "molstar_css_url_html": html.escape(molstar_css_url, quote=True),
        "molstar_js_url_html": html.escape(molstar_js_url, quote=True),
        "qrcode_js_url_html": html.escape(qrcode_js_url, quote=True),
        "structure_name_json": _json_for_script(structure_name),
        "format_json": _json_for_script(format_key),
        "file_url_json": _json_for_script(file_url),
        "auto_close_json": _json_for_script(auto_close),
        "share_url_json": _json_for_script(share_url),
        "fallback_share_url_json": _json_for_script(fallback_share_url),
        "heartbeat_url_json": _json_for_script(heartbeat_url),
        "bye_url_json": _json_for_script(bye_url),
        "heartbeat_interval_ms_json": _json_for_script(heartbeat_interval_ms),
        "watch_json": _json_for_script(watch),
        "version_url_json": _json_for_script(version_url),
    }

    def _replace(match: re.Match[str]) -> str:
        key = match.group(1)
        return replacements.get(key, match.group(0))

    return _TEMPLATE_PLACEHOLDER_RE.sub(_replace, _VIEWER_TEMPLATE)


def normalize_base_path(path: str | None) -> str:
    """Return a canonical request path prefix without a trailing slash."""
    if path in {None, "", "/"}:
        return ""
    return f"/{path.strip('/')}"


def route_path(base_path: str, endpoint: str) -> str:
    """Build an absolute request path rooted at *base_path*."""
    normalized_base = normalize_base_path(base_path)
    if endpoint == "/":
        return "/" if not normalized_base else f"{normalized_base}/"
    return f"{normalized_base}{endpoint}" if normalized_base else endpoint


def strip_base_path(request_path: str, base_path: str) -> str | None:
    """Return *request_path* relative to *base_path*, or ``None`` if it does not match."""
    normalized_base = normalize_base_path(base_path)
    if not normalized_base:
        return request_path
    if request_path == normalized_base:
        return "/"
    prefix = f"{normalized_base}/"
    if not request_path.startswith(prefix):
        return None
    suffix = request_path[len(normalized_base) :]
    return suffix or "/"


class _Handler(BaseHTTPRequestHandler):
    """Serves the viewer page, structure file, vendored assets, and session lifecycle endpoints."""

    structure_source: StructureSource
    viewer_html_bytes: bytes = b""
    fallback_share_url: str | None = None
    shutdown_event: threading.Event | None = None
    auto_close: bool = False
    watch: bool = False
    base_path: str = ""
    idle_timeout: float = _DEFAULT_IDLE_TIMEOUT
    bye_grace: float = _DEFAULT_BYE_GRACE
    last_heartbeat: float = 0.0
    activity_seq: int = 0
    active_clients: dict[str, float]
    state_lock: threading.Lock
    remote_cache: _CachedRemoteResponse | None = None
    remote_cache_lock: threading.Lock

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        pass

    def do_GET(self) -> None:  # noqa: N802
        self._dispatch_get_or_head(send_body=True)

    def do_HEAD(self) -> None:  # noqa: N802
        self._dispatch_get_or_head(send_body=False)

    def _dispatch_get_or_head(self, *, send_body: bool) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/favicon.ico":
            self._serve_favicon(send_body=send_body)
            return

        if self.base_path and path == self.base_path:
            self.send_response(HTTPStatus.PERMANENT_REDIRECT)
            self.send_header("Location", route_path(self.base_path, "/"))
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        inner_path = strip_base_path(path, self.base_path)
        if inner_path == "/":
            self._record_activity()
            self._serve_viewer(send_body=send_body)
        elif inner_path == "/structure":
            self._record_activity()
            refresh = parse_qs(parsed.query).get("refresh", [""])[0] in {"1", "true"}
            self._serve_structure(send_body=send_body, force_refresh=refresh)
        elif inner_path == "/version":
            self._serve_version(send_body=send_body)
        elif inner_path in {"/info", "/health"}:
            self._serve_info(send_body=send_body)
        elif inner_path == "/favicon.ico":
            self._serve_favicon(send_body=send_body)
        elif inner_path in _VENDORED_ASSETS:
            self._record_activity()
            self._serve_asset(inner_path, send_body=send_body)
        elif inner_path == "/heartbeat":
            self._serve_heartbeat(parsed.query)
        elif inner_path == "/bye":
            self._serve_bye(parsed.query)
        else:
            self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if strip_base_path(parsed.path, self.base_path) == "/bye":
            self._serve_bye(parsed.query)
        else:
            self.send_error(HTTPStatus.NOT_FOUND)

    def _send_bytes(
        self,
        body: bytes,
        content_type: str,
        *,
        status: int = HTTPStatus.OK,
        send_body: bool = True,
        headers: dict[str, str] | None = None,
        cors: bool = False,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        if headers:
            for key, value in headers.items():
                self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        if cors:
            self._cors_headers()
        self.end_headers()
        if not send_body:
            return
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            return

    def _serve_viewer(self, *, send_body: bool = True) -> None:
        self._send_bytes(
            self.viewer_html_bytes,
            "text/html; charset=utf-8",
            send_body=send_body,
            cors=True,
        )

    def _serve_favicon(self, *, send_body: bool = True) -> None:
        self._send_bytes(
            _FAVICON_SVG,
            "image/svg+xml",
            send_body=send_body,
            headers={"Cache-Control": "public, max-age=86400"},
        )

    def _serve_version(self, *, send_body: bool = True) -> None:
        version_token: str | None = None
        if self.structure_source.local_path is not None:
            version_token = local_file_signature(self.structure_source.local_path)
        body = json.dumps({"version": version_token}).encode("utf-8")
        self._send_bytes(
            body,
            "application/json; charset=utf-8",
            send_body=send_body,
            cors=True,
        )

    def _serve_info(self, *, send_body: bool = True) -> None:
        payload = {
            "status": "ok",
            "version": _PACKAGE_VERSION,
            "name": self.structure_source.display_name,
            "format": self.structure_source.format_key,
            "source_kind": self.structure_source.source_kind,
            "watch": self.watch,
            "auto_close": self.auto_close,
        }
        body = json.dumps(payload).encode("utf-8")
        self._send_bytes(
            body,
            "application/json; charset=utf-8",
            send_body=send_body,
            cors=True,
        )

    def _serve_structure(self, *, send_body: bool = True, force_refresh: bool = False) -> None:
        if self.structure_source.is_inline:
            self._serve_inline_structure(send_body=send_body)
            return
        if self.structure_source.is_remote:
            self._serve_remote_structure(send_body=send_body, force_refresh=force_refresh)
            return
        self._serve_local_structure(send_body=send_body)

    def _serve_inline_structure(self, *, send_body: bool = True) -> None:
        assert self.structure_source.inline_bytes is not None
        data = self.structure_source.inline_bytes
        mime = _guess_mime(
            self.structure_source.display_name,
            format_key=self.structure_source.format_key,
        )
        headers = {"Content-Encoding": "gzip"} if _is_gzip_bytes(data) else None
        self._send_bytes(data, mime, send_body=send_body, headers=headers, cors=True)

    def _serve_asset(self, path: str, *, send_body: bool = True) -> None:
        asset = _VENDORED_ASSETS[path]
        use_gzip = _accepts_gzip(self.headers.get("Accept-Encoding"))
        body = (
            _load_vendored_asset_gzip(asset.package_name)
            if use_gzip
            else _load_vendored_asset(asset.package_name)
        )
        headers = {
            "Vary": "Accept-Encoding",
            "Cache-Control": "public, max-age=31536000, immutable",
        }
        if use_gzip:
            headers["Content-Encoding"] = "gzip"
        self._send_bytes(body, asset.content_type, send_body=send_body, headers=headers)

    def _serve_local_structure(self, *, send_body: bool = True) -> None:
        assert self.structure_source.local_path is not None
        try:
            handle = self.structure_source.local_path.open("rb")
        except OSError:
            self.send_error(HTTPStatus.INTERNAL_SERVER_ERROR, "Cannot read structure file")
            return

        with handle:
            try:
                size = os.fstat(handle.fileno()).st_size
            except OSError:
                self.send_error(HTTPStatus.INTERNAL_SERVER_ERROR, "Cannot read structure file")
                return

            mime = _guess_mime(
                self.structure_source.display_name,
                format_key=self.structure_source.format_key,
            )
            content_encoding = _guess_content_encoding(self.structure_source.display_name)
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", mime)
            if content_encoding is not None:
                self.send_header("Content-Encoding", content_encoding)
            self.send_header("Content-Length", str(size))
            self._cors_headers()
            self.end_headers()
            if not send_body:
                return
            try:
                while chunk := handle.read(_STREAM_CHUNK_SIZE):
                    self.wfile.write(chunk)
            except (BrokenPipeError, ConnectionResetError):
                return
            except OSError:
                self.close_connection = True

    def _serve_cached_remote(
        self,
        cached: _CachedRemoteResponse,
        *,
        send_body: bool,
    ) -> None:
        headers = {"Content-Encoding": cached.content_encoding} if cached.content_encoding else None
        self._send_bytes(
            cached.body,
            cached.content_type,
            status=cached.status,
            send_body=send_body,
            headers=headers,
            cors=True,
        )

    def _resolve_remote_url(self) -> str:
        if self.structure_source.remote_url is not None:
            return self.structure_source.remote_url

        api_url = self.structure_source.alphafold_api_url
        assert api_url is not None
        with self._open_url(api_url, accept="application/json") as response:
            payload = json.loads(response.read().decode("utf-8"))

        if not isinstance(payload, list) or not payload:
            raise HTTPError(
                api_url, HTTPStatus.NOT_FOUND, "AlphaFold DB entry not found", Message(), None
            )

        target_entry: dict[str, Any] | None = None
        af_id = (self.structure_source.alphafold_id or "").upper()
        for item in payload:
            if not isinstance(item, dict):
                continue
            entry_id = str(item.get("entryId", "")).upper()
            if af_id and entry_id == af_id:
                target_entry = item
                break
        if target_entry is None:
            first = payload[0]
            if isinstance(first, dict):
                target_entry = first

        field = _ALPHAFOLD_FORMAT_FIELDS.get(self.structure_source.format_key, "cifUrl")
        resolved_url = target_entry.get(field) if target_entry is not None else None
        if not isinstance(resolved_url, str) or not resolved_url:
            raise HTTPError(
                api_url,
                HTTPStatus.NOT_FOUND,
                f"AlphaFold DB does not provide a {self.structure_source.format_key} URL for this entry",
                Message(),
                None,
            )
        return resolved_url

    def _serve_remote_structure(
        self,
        *,
        send_body: bool = True,
        force_refresh: bool = False,
    ) -> None:
        cls = type(self)
        if not force_refresh:
            with cls.remote_cache_lock:
                cached = cls.remote_cache
            if cached is not None:
                self._serve_cached_remote(cached, send_body=send_body)
                return

        try:
            resolved_url = self._resolve_remote_url()
            response = self._open_url(resolved_url)
        except HTTPError as exc:
            detail = str(exc.reason) if exc.reason else "request failed"
            self.send_error(exc.code, f"Upstream server returned {exc.code}: {detail}")
            return
        except (TimeoutError, URLError) as exc:
            if _is_timeout_error(exc):
                self.send_error(HTTPStatus.GATEWAY_TIMEOUT, "Remote structure request timed out")
                return
            reason = exc.reason if isinstance(exc, URLError) else exc
            detail = str(reason) if reason else "connection failed"
            self.send_error(HTTPStatus.BAD_GATEWAY, f"Failed to fetch remote structure: {detail}")
            return
        except (ValueError, UnicodeDecodeError) as exc:
            self.send_error(HTTPStatus.BAD_GATEWAY, f"Invalid metadata from upstream: {exc}")
            return

        with response:
            status = getattr(response, "status", HTTPStatus.OK)
            content_type = response.headers.get("Content-Type") or _guess_mime(
                self.structure_source.display_name,
                format_key=self.structure_source.format_key,
            )
            content_encoding = response.headers.get("Content-Encoding") or _guess_content_encoding(
                self.structure_source.display_name
            )
            content_length = response.headers.get("Content-Length")

            self.send_response(status)
            self.send_header("Content-Type", content_type)
            if content_encoding is not None:
                self.send_header("Content-Encoding", content_encoding)
            if content_length is not None:
                self.send_header("Content-Length", content_length)
            self._cors_headers()
            self.end_headers()
            if not send_body:
                return

            chunks: list[bytes] | None = [] if status == HTTPStatus.OK else None
            cached_bytes = 0
            completed = False
            try:
                while chunk := response.read(_STREAM_CHUNK_SIZE):
                    self.wfile.write(chunk)
                    if chunks is not None:
                        cached_bytes += len(chunk)
                        if cached_bytes <= _MAX_REMOTE_CACHE_BYTES:
                            chunks.append(chunk)
                        else:
                            chunks = None
                completed = True
            except (BrokenPipeError, ConnectionResetError):
                return
            except (OSError, http.client.HTTPException):
                self.close_connection = True
                return

            if completed and chunks is not None:
                body = b"".join(chunks)
                expected_length: int | None = None
                if content_length is not None:
                    try:
                        expected_length = int(content_length)
                    except ValueError:
                        expected_length = None
                if expected_length is None or len(body) == expected_length:
                    with cls.remote_cache_lock:
                        cls.remote_cache = _CachedRemoteResponse(
                            status=status,
                            content_type=content_type,
                            content_encoding=content_encoding,
                            body=body,
                        )

    def _open_url(self, url: str, *, accept: str = "*/*") -> Any:
        last_error: URLError | TimeoutError | None = None
        for attempt in range(_REMOTE_FETCH_ATTEMPTS):
            request = Request(
                url,
                headers={
                    "User-Agent": f"molbo/{_PACKAGE_VERSION}",
                    "Accept": accept,
                },
            )
            try:
                return urlopen(request, timeout=self.structure_source.fetch_timeout)
            except HTTPError:
                raise
            except (TimeoutError, URLError) as exc:
                last_error = exc
                if isinstance(exc, URLError) and _is_non_retryable_url_error(exc):
                    raise
                if attempt + 1 == _REMOTE_FETCH_ATTEMPTS:
                    raise
                time.sleep(_REMOTE_FETCH_RETRY_DELAY * (attempt + 1))

        raise RuntimeError(f"unreachable remote fetch state: {last_error}")

    def _extract_client_id(self, query: str) -> str | None:
        values = parse_qs(query).get("client")
        if not values:
            return None
        client_id = values[0].strip()
        return client_id or None

    def _record_activity(self, client_id: str | None = None) -> None:
        cls = type(self)
        now = time.monotonic()
        with cls.state_lock:
            cls.last_heartbeat = now
            cls.activity_seq += 1
            if client_id is not None:
                cls.active_clients[client_id] = now

    def _serve_heartbeat(self, query: str = "") -> None:
        client_id = self._extract_client_id(query)
        self._record_activity(client_id=client_id)
        self.send_response(HTTPStatus.NO_CONTENT)
        self._cors_headers()
        self.end_headers()

    def _serve_bye(self, query: str = "") -> None:
        """Schedule shutdown when the last active browser tab closes."""
        client_id = self._extract_client_id(query)
        self.send_response(HTTPStatus.NO_CONTENT)
        self._cors_headers()
        self.end_headers()
        if not self.auto_close or self.shutdown_event is None:
            return

        cls = type(self)
        now = time.monotonic()
        with cls.state_lock:
            stale_ids = [
                cid
                for cid, last_seen in cls.active_clients.items()
                if now - last_seen > cls.idle_timeout
            ]
            for cid in stale_ids:
                del cls.active_clients[cid]
            if client_id is not None:
                cls.active_clients.pop(client_id, None)
            if cls.active_clients:
                return
            seq = cls.activity_seq

        if cls.bye_grace <= 0:
            self.shutdown_event.set()
            return

        shutdown_event = self.shutdown_event

        def _finish_bye() -> None:
            with cls.state_lock:
                if cls.activity_seq != seq or cls.active_clients:
                    return
            shutdown_event.set()

        timer = threading.Timer(cls.bye_grace, _finish_bye)
        timer.daemon = True
        timer.start()

    def _cors_headers(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Robots-Tag", "noindex, nofollow")


class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    """Small threaded HTTP server suitable for local one-user traffic."""

    daemon_threads = True

    def server_bind(self) -> None:
        TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = str(host)
        self.server_port = int(port)


class ThreadingIPv6HTTPServer(ThreadingHTTPServer):
    """Threaded HTTP server configured for IPv6 addresses."""

    address_family = socket.AF_INET6


def make_server(
    structure_source: StructureSource,
    port: int,
    host: str = "127.0.0.1",
    shutdown_event: threading.Event | None = None,
    auto_close: bool = False,
    share_url: str | None = None,
    base_path: str = "",
    idle_timeout: float = _DEFAULT_IDLE_TIMEOUT,
    bye_grace: float = _DEFAULT_BYE_GRACE,
    watch: bool = False,
) -> HTTPServer:
    """Create (but don't start) an HTTPServer for *structure_source*."""
    normalized_base_path = normalize_base_path(base_path)

    handler: type[_Handler] = type(
        "Handler",
        (_Handler,),
        {
            "structure_source": structure_source,
            "shutdown_event": shutdown_event,
            "auto_close": auto_close,
            "watch": watch,
            "base_path": normalized_base_path,
            "idle_timeout": idle_timeout,
            "bye_grace": bye_grace,
            "last_heartbeat": time.monotonic(),
            "activity_seq": 0,
            "active_clients": {},
            "state_lock": threading.Lock(),
            "remote_cache": None,
            "remote_cache_lock": threading.Lock(),
        },
    )

    bind_host = host.strip("[]")
    family = _resolve_address_family(bind_host)
    server_cls: type[HTTPServer] = (
        ThreadingIPv6HTTPServer if family == socket.AF_INET6 else ThreadingHTTPServer
    )
    server = server_cls((bind_host, port), handler)

    fallback_share_url: str | None = None
    if share_url is None:
        lan_host = detect_lan_host(host)
        if lan_host is not None:
            chosen_port = server.server_address[1]
            lan_origin = f"http://{format_host_for_url(lan_host)}:{chosen_port}"
            fallback_share_url = (
                f"{lan_origin}{route_path(normalized_base_path, '/')}"
                if normalized_base_path
                else lan_origin
            )

    handler.fallback_share_url = fallback_share_url
    handler.viewer_html_bytes = render_viewer_html(
        structure_name=structure_source.display_name,
        format_key=structure_source.format_key,
        file_url="./structure",
        auto_close=auto_close,
        share_url=share_url,
        fallback_share_url=fallback_share_url,
        molstar_css_url="./assets/molstar-4.5.0.css",
        molstar_js_url="./assets/molstar-4.5.0.js",
        qrcode_js_url="./assets/qrcode-generator-2.0.4.min.js",
        heartbeat_url="./heartbeat",
        bye_url="./bye",
        heartbeat_interval_ms=compute_heartbeat_interval_ms(idle_timeout),
        watch=watch,
        version_url="./version",
        favicon_url="./favicon.ico",
    ).encode("utf-8")
    return server


def start_heartbeat_watchdog(
    handler_cls: Any,
    shutdown_event: threading.Event,
    timeout: float = _DEFAULT_IDLE_TIMEOUT,
) -> threading.Thread:
    """Monitor heartbeats and trigger shutdown after the idle timeout expires."""
    poll_interval = min(0.5, max(0.05, timeout / 4))

    def _watch() -> None:
        while not shutdown_event.is_set():
            lock = getattr(handler_cls, "state_lock", None)
            if lock is not None:
                with lock:
                    last_heartbeat = handler_cls.last_heartbeat
            else:
                last_heartbeat = handler_cls.last_heartbeat
            elapsed = time.monotonic() - last_heartbeat
            if elapsed > timeout:
                shutdown_event.set()
                return
            shutdown_event.wait(timeout=poll_interval)

    t = threading.Thread(target=_watch, daemon=True)
    t.start()
    return t


def serve_background(server: HTTPServer) -> threading.Thread:
    """Start the server in a daemon thread and return the thread."""
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return t
