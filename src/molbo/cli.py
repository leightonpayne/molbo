"""CLI entry-point for molbo."""

from __future__ import annotations

import os
import re
import signal
import sys
import threading
import webbrowser
from collections.abc import Mapping
from pathlib import Path
from typing import Annotated, Literal, get_args
from urllib.parse import urlparse

import typer
from rich.console import Console
from rich.markup import escape as escape_markup
from rich.panel import Panel
from rich.text import Text

from molbo import __version__
from molbo.server import (
    StructureSource,
    display_name_from_url,
    format_host_for_url,
    local_access_host,
    make_server,
    normalize_base_path,
    route_path,
    serve_background,
    start_heartbeat_watchdog,
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

app = typer.Typer(
    name="molbo",
    help="Inspect molecular structures in a Mol* browser viewer.",
    add_completion=False,
    no_args_is_help=True,
    rich_markup_mode="rich",
)
console = Console()
err_console = Console(stderr=True)


def _version_callback(value: bool) -> None:
    if value:
        console.print(f"molbo [bold]{escape_markup(__version__)}[/bold]")
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
    return (
        _PDB_ID_RE.fullmatch(value) is not None or _EXTENDED_PDB_ID_RE.fullmatch(value) is not None
    )


def _looks_like_alphafold_id(value: str) -> bool:
    return (
        _ALPHAFOLD_PREFIX_RE.fullmatch(value) is not None
        or _ALPHAFOLD_ENTRY_RE.fullmatch(value) is not None
    )


def _detect_headless_reason(
    env: Mapping[str, str] | None = None,
    platform: str | None = None,
) -> Literal["ssh", "headless"] | None:
    """Detect SSH or headless sessions where auto-opening a browser should be skipped."""
    environ = os.environ if env is None else env
    plat = sys.platform if platform is None else platform
    if any(environ.get(var) for var in ("SSH_CONNECTION", "SSH_CLIENT", "SSH_TTY")):
        return "ssh"
    if (
        plat.startswith("linux")
        and not environ.get("DISPLAY")
        and not environ.get("WAYLAND_DISPLAY")
    ):
        return "headless"
    return None


def _ssh_forward_hint(host: str, port: int) -> str:
    target_host = format_host_for_url(local_access_host(host))
    return f"ssh -L {port}:{target_host}:{port} <remote-host>"


def _viewer_url(origin: str, base_path: str) -> str:
    normalized_base_path = normalize_base_path(base_path)
    return f"{origin}{route_path(normalized_base_path, '/')}" if normalized_base_path else origin


def _normalize_base_url(value: str | None) -> tuple[str | None, str]:
    if value is None:
        return None, ""

    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise typer.BadParameter("Base URL must be an absolute http(s) URL.")
    if parsed.params or parsed.query or parsed.fragment:
        raise typer.BadParameter("Base URL must not include params, query, or fragment components.")

    origin = f"{parsed.scheme}://{parsed.netloc}"
    base_path = normalize_base_path(parsed.path)
    return _viewer_url(origin, base_path), base_path


def _pdb_id_to_structure_source(
    value: str,
    fetch_timeout: float,
    format_override: str | None = None,
) -> StructureSource:
    requested_format = (format_override or "cif").lower()
    if requested_format not in _RCSB_DOWNLOAD_URLS:
        raise typer.BadParameter(
            "Unsupported --format for PDB IDs. Choose from cif, mmcif, pdb, or bcif."
        )

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
            raise typer.BadParameter(
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


def _alphafold_id_to_structure_source(
    value: str,
    fetch_timeout: float,
    format_override: str | None = None,
) -> StructureSource:
    requested_format = (format_override or "cif").lower()
    if requested_format not in _ALPHAFOLD_FORMATS:
        raise typer.BadParameter(
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
    if path.exists():
        if path.is_dir():
            err_console.print(
                f"[bold red]Error:[/] Path is a directory, not a structure file: "
                f"[yellow]{escape_markup(value)}[/]"
            )
            raise typer.Exit(code=1)
        if not path.is_file():
            err_console.print(
                f"[bold red]Error:[/] Path is not a regular file: [yellow]{escape_markup(value)}[/]"
            )
            raise typer.Exit(code=1)
        fmt = format_override or _resolve_structure_format(path)
        if fmt is None:
            return None
        return StructureSource(display_name=path.name, format_key=fmt, local_path=path)

    if _looks_like_pdb_id(value):
        return _pdb_id_to_structure_source(value, fetch_timeout, format_override=format_override)

    if _looks_like_alphafold_id(value):
        return _alphafold_id_to_structure_source(
            value, fetch_timeout, format_override=format_override
        )

    err_console.print(f"[bold red]Error:[/] File not found: [yellow]{escape_markup(value)}[/]")
    raise typer.Exit(code=1)


def _format_local_path_for_display(path: Path) -> str:
    try:
        home = Path.home()
        display = path.resolve().relative_to(home)
        return str(Path("~") / display)
    except Exception:
        return path.name or str(path)


def _display_source_label(source: StructureSource) -> str:
    if source.pdb_id is not None:
        return f"PDB ID {source.pdb_id}"
    if source.alphafold_id is not None:
        return f"AlphaFold DB {source.alphafold_id}"
    if source.remote_url is not None:
        return source.remote_url
    if source.local_path is not None:
        return _format_local_path_for_display(source.local_path)
    return source.display_name


def _build_info_grid(
    source: StructureSource,
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


@app.command()
def view(
    source: Annotated[
        str,
        typer.Argument(
            help=(
                "Local path, PDB ID (e.g. 1crn or pdb_00001crn), AlphaFold ID "
                "(e.g. af:P12345 or AF-P12345-F1), or http(s) URL."
            ),
        ),
    ],
    port: Annotated[
        int | None,
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
    watch: Annotated[
        bool,
        typer.Option(
            "--watch",
            "-w",
            help="Watch a local structure file for changes and reload the viewer automatically.",
        ),
    ] = False,
    auto_close: Annotated[
        bool | None,
        typer.Option(
            "--auto-close/--keep-alive",
            help="Shut down when the browser tab closes. Default keeps the server alive until Ctrl+C.",
        ),
    ] = None,
    idle_timeout: Annotated[
        float | None,
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
        FormatChoice | None,
        typer.Option(
            "--format",
            case_sensitive=False,
            help="Explicit structure format for extensionless inputs or database ID downloads.",
        ),
    ] = None,
    base_url: Annotated[
        str | None,
        typer.Option(
            "--base-url",
            help="Public viewer URL to display and encode in the QR modal, e.g. https://mol.example.com or https://mol.example.com/s/demo.",
        ),
    ] = None,
    version: Annotated[  # noqa: ARG001
        bool | None,
        typer.Option(
            "--version", "-v", callback=_version_callback, is_eager=True, help="Show version."
        ),
    ] = None,
) -> None:
    """Open a molecular structure in the Mol* 3D viewer."""
    if auto_close is False and idle_timeout is not None:
        raise typer.BadParameter("--idle-timeout cannot be combined with --keep-alive.")

    base_url, base_path = _normalize_base_url(base_url)
    format_override_str = format_override.lower() if format_override is not None else None

    structure_source = _resolve_structure_source(
        source,
        fetch_timeout=fetch_timeout,
        format_override=format_override_str,
    )
    if structure_source is None:
        err_console.print(
            f"[bold red]Error:[/] Unsupported file type [yellow]{escape_markup(source)}[/]. "
            f"Supported: {SUPPORTED_EXTENSIONS}, PDB IDs, or AlphaFold IDs. "
            f"Use --format with one of {', '.join(SUPPORTED_FORMAT_KEYS)} for extensionless inputs."
        )
        raise typer.Exit(code=1)

    if watch and structure_source.is_remote:
        raise typer.BadParameter("--watch is only supported for local structure files.")

    effective_auto_close = bool(auto_close) or idle_timeout is not None
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
            base_path=base_path,
            idle_timeout=heartbeat_timeout,
            watch=watch,
        )
    except OSError as exc:
        port_label = str(port) if port is not None else "auto"
        err_console.print(
            f"[bold red]Error:[/] Could not bind local server on "
            f"[yellow]{escape_markup(host)}:{port_label}[/]."
        )
        err_console.print(f"[dim]{escape_markup(str(exc))}[/dim]")
        raise typer.Exit(code=1) from exc

    chosen_port = server.server_address[1]
    local_origin = f"http://{format_host_for_url(local_access_host(host))}:{chosen_port}"
    local_url = _viewer_url(local_origin, base_path)
    share_url = base_url or local_url

    info = _build_info_grid(
        source=structure_source,
        format_key=structure_source.format_key,
        host=host,
        local_url=local_url,
        share_url=share_url,
    )
    subtitle_parts = ["Ctrl+C to quit"]
    if watch:
        subtitle_parts.append("watching for file changes")
    if effective_auto_close:
        subtitle_parts.append(f"auto-close after {heartbeat_timeout:g}s idle")
    subtitle = f"[dim]{' • '.join(subtitle_parts)}[/dim]"

    console.print()
    console.print(
        Panel(
            info,
            title="[bold]molbo[/bold]",
            subtitle=subtitle,
            border_style="bright_blue",
            padding=(1, 2),
        )
    )

    server_thread = serve_background(server)

    if effective_auto_close:
        handler_cls = server.RequestHandlerClass
        start_heartbeat_watchdog(handler_cls, shutdown_event, timeout=heartbeat_timeout)

    if not no_open:
        headless_reason = _detect_headless_reason()
        if headless_reason == "ssh":
            console.print(
                f"[dim]SSH session detected; skipping browser launch. "
                f"Forward locally with:[/] [bold]{escape_markup(_ssh_forward_hint(host, chosen_port))}[/]"
            )
        elif headless_reason == "headless":
            console.print(
                f"[dim]Headless environment detected; skipping browser launch. "
                f"Open[/] [underline]{escape_markup(local_url)}[/] [dim]manually.[/]"
            )
        elif not webbrowser.open(local_url):
            err_console.print(
                f"[yellow]Warning:[/] Could not auto-open a browser. "
                f"Open [underline]{escape_markup(local_url)}[/] manually."
            )

    previous_sigterm = None
    has_sigterm = (
        hasattr(signal, "SIGTERM") and threading.current_thread() is threading.main_thread()
    )
    if has_sigterm:
        previous_sigterm = signal.getsignal(signal.SIGTERM)
        signal.signal(signal.SIGTERM, lambda _signum, _frame: shutdown_event.set())

    try:
        # Short timeout keeps Ctrl+C responsive on Windows.
        while not shutdown_event.wait(timeout=0.25):
            pass
    except KeyboardInterrupt:
        pass
    finally:
        if has_sigterm and previous_sigterm is not None:
            signal.signal(signal.SIGTERM, previous_sigterm)
        shutdown_event.set()
        console.print("\n[dim]Shutting down…[/dim]")
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=1.0)


if __name__ == "__main__":
    app()
