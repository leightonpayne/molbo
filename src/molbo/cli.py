"""CLI entry-point for molbo."""

from __future__ import annotations

import re
import threading
import webbrowser
from pathlib import Path
from typing import Annotated, Literal, Optional
from urllib.parse import urlparse

import typer
from rich.console import Console
from rich.panel import Panel
from rich.text import Text

from molbo import __version__
from molbo.server import (
    StructureSource,
    display_name_from_url,
    format_host_for_url,
    make_server,
    serve_background,
    start_heartbeat_watchdog,
)

# ── Constants ────────────────────────────────────────────────────────────────

SUPPORTED_SUFFIXES: dict[tuple[str, ...], str] = {
    (".pdb",): "pdb",
    (".cif",): "cif",
    (".mmcif",): "mmcif",
    (".bcif",): "bcif",
    (".pdb", ".gz"): "pdb",
    (".cif", ".gz"): "cif",
    (".mmcif", ".gz"): "mmcif",
    (".bcif", ".gz"): "bcif",
}
SUPPORTED_EXTENSIONS = ", ".join("".join(suffixes) for suffixes in SUPPORTED_SUFFIXES)
SUPPORTED_FORMAT_KEYS = ("pdb", "cif", "mmcif", "bcif")
_PDB_ID_RE = re.compile(r"^[A-Za-z0-9]{4}$")
_RCSB_DOWNLOAD_URL = "https://files.rcsb.org/download/{pdb_id}.cif"

# ── App setup ────────────────────────────────────────────────────────────────

app = typer.Typer(
    name="molbo",
    help="Inspect PDB/CIF molecular structures in a Mol* browser viewer.",
    add_completion=False,
    no_args_is_help=True,
    rich_markup_mode="rich",
)
console = Console()
err_console = Console(stderr=True)


def _version_callback(value: bool) -> None:
    if value:
        console.print(f"molbo [bold]{__version__}[/bold]")
        raise typer.Exit()


def _resolve_structure_format(file: Path) -> str | None:
    suffixes = tuple(suffix.lower() for suffix in file.suffixes)
    for width in (2, 1):
        key = suffixes[-width:]
        if key in SUPPORTED_SUFFIXES:
            return SUPPORTED_SUFFIXES[key]
    return None


def _looks_like_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _looks_like_pdb_id(value: str) -> bool:
    return _PDB_ID_RE.fullmatch(value) is not None


def _normalize_base_url(value: str | None) -> str | None:
    if value is None:
        return None

    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise typer.BadParameter("Base URL must be an absolute http(s) URL.")
    if parsed.params or parsed.query or parsed.fragment:
        raise typer.BadParameter("Base URL must not include params, query, or fragment components.")
    if parsed.path not in {"", "/"}:
        raise typer.BadParameter("Base URL must point to the viewer root, not a path prefix.")

    return f"{parsed.scheme}://{parsed.netloc}"


def _pdb_id_to_structure_source(
    value: str,
    fetch_timeout: float,
    format_override: str | None = None,
) -> StructureSource:
    pdb_id = value.upper()
    if format_override not in {None, "cif"}:
        raise typer.BadParameter(
            "PDB IDs resolve to mmCIF downloads, so --format must be omitted or set to cif."
        )
    return StructureSource(
        display_name=f"{pdb_id}.cif",
        format_key="cif",
        remote_url=_RCSB_DOWNLOAD_URL.format(pdb_id=pdb_id),
        fetch_timeout=fetch_timeout,
    )


def _resolve_structure_source(
    value: str,
    fetch_timeout: float,
    format_override: str | None = None,
) -> StructureSource | None:
    if _looks_like_url(value):
        display_name = display_name_from_url(value)
        fmt = format_override or _resolve_structure_format(Path(display_name))
        if fmt is None:
            return None
        return StructureSource(
            display_name=display_name,
            format_key=fmt,
            remote_url=value,
            fetch_timeout=fetch_timeout,
        )

    path = Path(value).expanduser().resolve()
    if path.exists() and path.is_file():
        fmt = format_override or _resolve_structure_format(path)
        if fmt is None:
            return None
        return StructureSource(display_name=path.name, format_key=fmt, local_path=path)

    if _looks_like_pdb_id(value):
        return _pdb_id_to_structure_source(value, fetch_timeout, format_override=format_override)

    err_console.print(f"[bold red]Error:[/] File not found: [yellow]{value}[/]")
    raise typer.Exit(code=1)


def _display_source_label(source: str) -> str:
    if _looks_like_url(source):
        return source
    if _looks_like_pdb_id(source):
        return f"PDB ID {source.upper()}"

    path = Path(source).expanduser()
    try:
        home = Path.home()
        display = path.resolve().relative_to(home)
        return str(Path("~") / display)
    except Exception:
        return path.name or str(path)


def _build_info_grid(
    source: str,
    format_key: str,
    host: str,
    local_url: str,
    share_url: str | None = None,
) -> Text:
    rows: list[tuple[str, Text]] = [
        ("Source", Text(_display_source_label(source), style="bold")),
        ("Format", Text(format_key.upper(), style="bold cyan")),
        ("Bind", Text(host, style="bold yellow")),
    ]
    if share_url is not None and share_url != local_url:
        rows.append(("Local URL", Text(local_url, style="bold green underline")))
        rows.append(("Share URL", Text(share_url, style="bold magenta underline")))
    else:
        rows.append(("URL", Text(local_url, style="bold green underline")))

    label_width = max(len(label) for label, _ in rows)
    info = Text()
    for index, (label, value) in enumerate(rows):
        info.append(f"{label:<{label_width}} ", style="dim")
        info.append_text(value)
        if index != len(rows) - 1:
            info.append("\n")
    return info


# ── Main command ─────────────────────────────────────────────────────────────


@app.command()
def view(
    source: Annotated[
        str,
        typer.Argument(
            help="Local path, PDB ID, or http(s) URL to a .pdb, .cif, .mmcif, .bcif, or matching .gz-compressed structure.",
        ),
    ],
    port: Annotated[
        Optional[int],
        typer.Option("--port", "-p", min=0, max=65535, help="Port to serve on (default: auto)."),
    ] = None,
    host: Annotated[
        str,
        typer.Option("--host", help="Interface or IP to bind to (default: 127.0.0.1)."),
    ] = "127.0.0.1",
    no_open: Annotated[
        bool,
        typer.Option("--no-open", help="Don't auto-open the browser."),
    ] = False,
    auto_close: Annotated[
        bool,
        typer.Option(
            "--auto-close/--keep-alive",
            help="Shut down when the browser tab closes. Default keeps the server alive until Ctrl+C.",
        ),
    ] = False,
    idle_timeout: Annotated[
        Optional[float],
        typer.Option(
            "--idle-timeout",
            min=1.0,
            help="Seconds without browser heartbeats before auto-close. Implies --auto-close.",
        ),
    ] = None,
    fetch_timeout: Annotated[
        float,
        typer.Option(
            "--fetch-timeout",
            min=1.0,
            help="Seconds to wait for remote structure downloads before timing out.",
        ),
    ] = 30.0,
    format_override: Annotated[
        Optional[Literal["pdb", "cif", "mmcif", "bcif"]],
        typer.Option(
            "--format",
            case_sensitive=False,
            help="Explicit structure format for inputs without a recognizable suffix.",
        ),
    ] = None,
    base_url: Annotated[
        Optional[str],
        typer.Option(
            "--base-url",
            help="Public viewer URL to display and encode in the QR modal, e.g. https://mol.example.com.",
        ),
    ] = None,
    version: Annotated[  # noqa: ARG001
        Optional[bool],
        typer.Option("--version", "-v", callback=_version_callback, is_eager=True, help="Show version."),
    ] = None,
) -> None:
    """Open a molecular structure in the Mol* 3D viewer."""

    base_url = _normalize_base_url(base_url)
    format_override = format_override.lower() if format_override is not None else None

    # ── Validate extension ───────────────────────────────────────────────
    structure_source = _resolve_structure_source(
        source,
        fetch_timeout=fetch_timeout,
        format_override=format_override,
    )
    if structure_source is None:
        err_console.print(
            f"[bold red]Error:[/] Unsupported file type [yellow]{source}[/]. "
            f"Supported: {SUPPORTED_EXTENSIONS}, or a 4-character PDB ID. "
            f"Use --format with one of {', '.join(SUPPORTED_FORMAT_KEYS)} for extensionless inputs."
        )
        raise typer.Exit(code=1)

    # ── Start server ─────────────────────────────────────────────────────
    effective_auto_close = auto_close or idle_timeout is not None
    heartbeat_timeout = idle_timeout if idle_timeout is not None else 15.0
    shutdown_event = threading.Event()

    try:
        server = make_server(
            structure_source,
            port if port is not None else 0,
            host=host,
            shutdown_event=shutdown_event,
            auto_close=effective_auto_close,
            share_url=base_url,
        )
    except OSError as exc:
        port_label = str(port) if port is not None else "auto"
        err_console.print(
            f"[bold red]Error:[/] Could not bind local server on [yellow]{host}:{port_label}[/]."
        )
        err_console.print(f"[dim]{exc}[/dim]")
        raise typer.Exit(code=1) from exc

    chosen_port = server.server_address[1]
    local_url = f"http://{format_host_for_url(host)}:{chosen_port}"
    share_url = base_url or local_url

    # Pretty banner
    info = _build_info_grid(
        source=source,
        format_key=structure_source.format_key,
        host=host,
        local_url=local_url,
        share_url=share_url,
    )
    console.print()
    console.print(
        Panel(
            info,
            title="[bold]molbo[/bold]",
            subtitle=(
                "[dim]Ctrl+C to quit[/dim]"
                if not effective_auto_close
                else f"[dim]Ctrl+C to quit • auto-close after {heartbeat_timeout:g}s idle[/dim]"
            ),
            border_style="bright_blue",
            padding=(1, 2),
        )
    )

    # Run server in background before opening the browser, so the first request can succeed.
    server_thread = serve_background(server)

    if effective_auto_close:
        # Get the handler class so the watchdog can read last_heartbeat
        handler_cls = server.RequestHandlerClass
        start_heartbeat_watchdog(handler_cls, shutdown_event, timeout=heartbeat_timeout)

    # Open browser
    if not no_open:
        if not webbrowser.open(local_url):
            err_console.print(
                f"[yellow]Warning:[/] Could not auto-open a browser. "
                f"Open [underline]{local_url}[/] manually."
            )

    try:
        shutdown_event.wait()  # blocks until browser tab closes or Ctrl+C
    except KeyboardInterrupt:
        pass
    finally:
        shutdown_event.set()
        console.print("\n[dim]Shutting down…[/dim]")
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=1.0)


# ── Allow `python -m molbo` ──────────────────────────────────────────────────

if __name__ == "__main__":
    app()
