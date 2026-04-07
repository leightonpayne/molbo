"""CLI entry-point for molbo."""

from __future__ import annotations

import threading
import webbrowser
from pathlib import Path
from typing import Annotated, Optional

import typer
from rich.console import Console
from rich.panel import Panel
from rich.text import Text

from molbo import __version__
from molbo.server import find_free_port, make_server, serve_background, start_heartbeat_watchdog

# ── Constants ────────────────────────────────────────────────────────────────

SUPPORTED_EXTENSIONS: dict[str, str] = {
    ".pdb": "pdb",
    ".cif": "cif",
    ".mmcif": "mmcif",
    ".bcif": "bcif",
}

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


# ── Main command ─────────────────────────────────────────────────────────────


@app.command()
def view(
    file: Annotated[
        Path,
        typer.Argument(
            help="Path to a .pdb, .cif, .mmcif, or .bcif structure file.",
            exists=True,
            dir_okay=False,
            readable=True,
            resolve_path=True,
        ),
    ],
    port: Annotated[
        Optional[int],
        typer.Option("--port", "-p", help="Port to serve on (default: auto)."),
    ] = None,
    no_open: Annotated[
        bool,
        typer.Option("--no-open", help="Don't auto-open the browser."),
    ] = False,
    version: Annotated[  # noqa: ARG001
        Optional[bool],
        typer.Option("--version", "-v", callback=_version_callback, is_eager=True, help="Show version."),
    ] = None,
) -> None:
    """Open a molecular structure in the Mol* 3D viewer."""

    # ── Validate extension ───────────────────────────────────────────────
    ext = file.suffix.lower()
    fmt = SUPPORTED_EXTENSIONS.get(ext)
    if fmt is None:
        err_console.print(
            f"[bold red]Error:[/] Unsupported file type [yellow]{ext}[/]. "
            f"Supported: {', '.join(SUPPORTED_EXTENSIONS)}"
        )
        raise typer.Exit(code=1)

    # ── Resolve port ─────────────────────────────────────────────────────
    chosen_port = port if port is not None else find_free_port()

    # ── Start server ─────────────────────────────────────────────────────
    server = make_server(file, fmt, chosen_port)
    url = f"http://localhost:{chosen_port}"

    # Pretty banner
    info = Text.assemble(
        ("  File   ", "dim"),
        (str(file), "bold"),
        "\n",
        ("  Format ", "dim"),
        (fmt.upper(), "bold cyan"),
        "\n",
        ("  URL    ", "dim"),
        (url, "bold green underline"),
    )
    console.print()
    console.print(
        Panel(
            info,
            title="[bold]molbo[/bold]",
            subtitle="[dim]Ctrl+C to quit[/dim]",
            border_style="bright_blue",
            padding=(1, 2),
        )
    )

    # Open browser
    if not no_open:
        webbrowser.open(url)

    # Run server in background, with heartbeat watchdog
    shutdown_event = threading.Event()
    serve_background(server)

    # Get the handler class so the watchdog can read last_heartbeat
    handler_cls = server.RequestHandlerClass
    handler_cls.shutdown_event = shutdown_event  # type: ignore[attr-defined]
    start_heartbeat_watchdog(handler_cls, server, shutdown_event)

    try:
        shutdown_event.wait()  # blocks until browser tab closes or Ctrl+C
    except KeyboardInterrupt:
        pass
    finally:
        console.print("\n[dim]Shutting down…[/dim]")
        server.shutdown()


# ── Allow `python -m molstar_cli` ────────────────────────────────────────────

if __name__ == "__main__":
    app()
