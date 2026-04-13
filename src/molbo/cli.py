"""CLI entry-point for molbo."""

from __future__ import annotations

from enum import Enum
import threading
import webbrowser
from pathlib import Path
from typing import Annotated, Optional
from urllib.parse import urlparse

import typer
from rich.console import Console
from rich.panel import Panel
from rich.text import Text

from molbo import __version__
from molbo.server import StructureSource, display_name_from_url, make_server, serve_background, start_heartbeat_watchdog

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


class ViewerStyle(str, Enum):
    DEFAULT = "default"
    PUBLICATION = "publication"

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


def _resolve_structure_source(value: str) -> StructureSource | None:
    if _looks_like_url(value):
        display_name = display_name_from_url(value)
        fmt = _resolve_structure_format(Path(display_name))
        if fmt is None:
            return None
        return StructureSource(display_name=display_name, format_key=fmt, remote_url=value)

    path = Path(value).expanduser().resolve()
    if not path.exists() or not path.is_file():
        err_console.print(f"[bold red]Error:[/] File not found: [yellow]{value}[/]")
        raise typer.Exit(code=1)
    fmt = _resolve_structure_format(path)
    if fmt is None:
        return None
    return StructureSource(display_name=path.name, format_key=fmt, local_path=path)


# ── Main command ─────────────────────────────────────────────────────────────


@app.command()
def view(
    source: Annotated[
        str,
        typer.Argument(
            help="Local path or http(s) URL to a .pdb, .cif, .mmcif, .bcif, or matching .gz-compressed structure.",
        ),
    ],
    port: Annotated[
        Optional[int],
        typer.Option("--port", "-p", min=0, max=65535, help="Port to serve on (default: auto)."),
    ] = None,
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
    style: Annotated[
        ViewerStyle,
        typer.Option("--style", help="Viewer style preset."),
    ] = ViewerStyle.PUBLICATION,
    version: Annotated[  # noqa: ARG001
        Optional[bool],
        typer.Option("--version", "-v", callback=_version_callback, is_eager=True, help="Show version."),
    ] = None,
) -> None:
    """Open a molecular structure in the Mol* 3D viewer."""

    # ── Validate extension ───────────────────────────────────────────────
    structure_source = _resolve_structure_source(source)
    if structure_source is None:
        err_console.print(
            f"[bold red]Error:[/] Unsupported file type [yellow]{source}[/]. "
            f"Supported: {SUPPORTED_EXTENSIONS}"
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
            shutdown_event=shutdown_event,
            auto_close=effective_auto_close,
            viewer_style=style.value,
        )
    except OSError as exc:
        port_label = str(port) if port is not None else "auto"
        err_console.print(f"[bold red]Error:[/] Could not bind local server on port [yellow]{port_label}[/].")
        err_console.print(f"[dim]{exc}[/dim]")
        raise typer.Exit(code=1) from exc

    chosen_port = server.server_address[1]
    url = f"http://localhost:{chosen_port}"

    # Pretty banner
    info = Text.assemble(
        ("  Source ", "dim"),
        (source, "bold"),
        "\n",
        ("  Format ", "dim"),
        (structure_source.format_key.upper(), "bold cyan"),
        "\n",
        ("  Style  ", "dim"),
        (style.value, "bold magenta"),
        "\n",
        ("  URL    ", "dim"),
        (url, "bold green underline"),
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
        if not webbrowser.open(url):
            err_console.print(f"[yellow]Warning:[/] Could not auto-open a browser. Open [underline]{url}[/] manually.")

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
