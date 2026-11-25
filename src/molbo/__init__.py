"""molbo — Inspect molecular structures in Mol* from the command line or Python."""

from __future__ import annotations

import secrets
import threading
import webbrowser
from dataclasses import dataclass
from http.server import HTTPServer
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from types import TracebackType
from urllib.parse import urlparse

from molbo.server import (
    SUPPORTED_FORMAT_KEYS,
    StructureSource,
    format_host_for_url,
    local_access_host,
    make_server,
    normalize_base_path,
    resolve_source,
    route_path,
    serve_background,
    start_heartbeat_watchdog,
)

try:
    __version__ = version("molbo")
except PackageNotFoundError:  # pragma: no cover
    __version__ = "0+unknown"


@dataclass
class MolboSession:
    """Handle to a running background Molbo HTTP viewer session."""

    url: str
    share_url: str
    host: str
    port: int
    source: StructureSource
    server: HTTPServer
    thread: threading.Thread
    shutdown_event: threading.Event

    def close(self) -> None:
        self.shutdown_event.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=1.0)

    def __enter__(self) -> MolboSession:
        return self

    def __exit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc_val: BaseException | None,
        _exc_tb: TracebackType | None,
    ) -> None:
        self.close()


def _viewer_url(origin: str, base_path: str) -> str:
    normalized = normalize_base_path(base_path)
    return f"{origin}{route_path(normalized, '/')}" if normalized else origin


def serve(
    source: str | Path | bytes | StructureSource,
    *,
    format: str | None = None,  # noqa: A002
    name: str | None = None,
    host: str = "127.0.0.1",
    port: int = 0,
    base_url: str | None = None,
    private: bool = False,
    watch: bool = False,
    auto_close: bool = False,
    idle_timeout: float = 15.0,
    fetch_timeout: float = 30.0,
    open_browser: bool = False,
) -> MolboSession:
    """Start a background Mol* viewer server and return a closeable :class:`MolboSession`."""
    if isinstance(source, StructureSource):
        structure_source = source
    elif isinstance(source, bytes):
        if format is None:
            raise ValueError("format is required when serving raw structure bytes.")
        fmt = format.lower()
        if fmt not in SUPPORTED_FORMAT_KEYS:
            raise ValueError(
                f"Unsupported format {format!r}. Choose from {', '.join(SUPPORTED_FORMAT_KEYS)}."
            )
        structure_source = StructureSource(
            display_name=name or f"structure.{fmt}",
            format_key=fmt,
            inline_bytes=source,
        )
    else:
        resolved = resolve_source(source, fetch_timeout=fetch_timeout, format_override=format)
        if resolved is None:
            raise ValueError(
                f"Cannot infer structure format for {source!r}; pass format= explicitly."
            )
        structure_source = resolved

    if watch and (structure_source.is_remote or structure_source.is_inline):
        raise ValueError("watch=True is only supported for local structure files.")

    normalized_base_url: str | None = None
    base_path = ""
    if base_url is not None:
        parsed = urlparse(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("base_url must be an absolute http(s) URL.")
        if parsed.params or parsed.query or parsed.fragment:
            raise ValueError("base_url must not include params, query, or fragment components.")
        origin = f"{parsed.scheme}://{parsed.netloc}"
        base_path = normalize_base_path(parsed.path)
        normalized_base_url = _viewer_url(origin, base_path)

    if private:
        token = secrets.token_urlsafe(12)
        base_path = normalize_base_path(f"{base_path}/{token}" if base_path else f"/s/{token}")
        if normalized_base_url is not None:
            parsed = urlparse(normalized_base_url)
            normalized_base_url = _viewer_url(f"{parsed.scheme}://{parsed.netloc}", base_path)

    shutdown_event = threading.Event()
    server = make_server(
        structure_source,
        port,
        host=host,
        shutdown_event=shutdown_event,
        auto_close=auto_close,
        share_url=normalized_base_url,
        base_path=base_path,
        idle_timeout=idle_timeout,
        watch=watch,
    )
    chosen_port = server.server_address[1]
    local_origin = f"http://{format_host_for_url(local_access_host(host))}:{chosen_port}"
    local_url = _viewer_url(local_origin, base_path)
    fallback_share_url = getattr(server.RequestHandlerClass, "fallback_share_url", None)
    effective_share_url = normalized_base_url or fallback_share_url or local_url

    thread = serve_background(server)
    if auto_close:
        start_heartbeat_watchdog(server.RequestHandlerClass, shutdown_event, timeout=idle_timeout)

    if open_browser:
        webbrowser.open(local_url)

    return MolboSession(
        url=local_url,
        share_url=effective_share_url,
        host=host,
        port=chosen_port,
        source=structure_source,
        server=server,
        thread=thread,
        shutdown_event=shutdown_event,
    )


__all__ = ["MolboSession", "StructureSource", "__version__", "serve"]
