"""CLI entry-point for molbo."""

from __future__ import annotations

import contextlib
import json
import os
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import webbrowser
from collections.abc import Mapping
from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import urlparse

import segno
import typer
from rich.console import Console
from rich.markup import escape as escape_markup
from rich.panel import Panel
from rich.text import Text

from molbo import __version__
from molbo.server import (
    SUPPORTED_EXTENSIONS,
    SUPPORTED_FORMAT_KEYS,
    SUPPORTED_SUFFIXES,
    FormatChoice,
    StructureSource,
    alphafold_id_to_structure_source,
    format_host_for_url,
    local_access_host,
    looks_like_alphafold_id,
    looks_like_pdb_id,
    looks_like_url,
    make_server,
    normalize_base_path,
    pdb_id_to_structure_source,
    resolve_source,
    resolve_structure_format,
    route_path,
    serve_background,
    start_heartbeat_watchdog,
)

_READY_FILE_ENV = "MOLBO_DETACH_READY_FILE"
_STDIN_FILE_ENV = "MOLBO_DETACH_STDIN_FILE"
_DETACH_TIMEOUT_SECONDS = 10.0

_HELP_EPILOG = """
Examples:
  molbo structure.pdb
  molbo 1crn
  molbo af:P69905 --format bcif
  molbo design.cif --watch
  molbo 1crn --host 0.0.0.0 --private --qr
  molbo pred.cif --detach --auto-close --json
  cat structure.cif | molbo - --format cif
"""

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


_resolve_structure_format = resolve_structure_format
_looks_like_url = looks_like_url
_looks_like_pdb_id = looks_like_pdb_id
_looks_like_alphafold_id = looks_like_alphafold_id


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


def _apply_private_path(
    base_url: str | None,
    base_path: str,
    token: str | None = None,
) -> tuple[str | None, str]:
    secret_token = token or secrets.token_urlsafe(12)
    private_base_path = normalize_base_path(
        f"{base_path}/{secret_token}" if base_path else f"/s/{secret_token}"
    )
    if base_url is None:
        return None, private_base_path
    parsed = urlparse(base_url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    return _viewer_url(origin, private_base_path), private_base_path


def _render_terminal_qr(url: str) -> Text:
    qr = segno.make(url, error="m")
    border = 2
    matrix = [list(row) for row in qr.matrix]
    width = len(matrix[0]) + border * 2
    padded = [[0] * width for _ in range(border)]
    for row in matrix:
        padded.append([0] * border + row + [0] * border)
    padded.extend([[0] * width for _ in range(border)])
    if len(padded) % 2:
        padded.append([0] * width)

    blocks = {
        (0, 0): " ",
        (1, 0): "▀",
        (0, 1): "▄",
        (1, 1): "█",
    }
    lines = [
        "".join(
            blocks[(bool(top), bool(bottom))]
            for top, bottom in zip(padded[i], padded[i + 1], strict=True)
        )
        for i in range(0, len(padded), 2)
    ]
    return Text("\n".join(lines), style="black on white")


def _pdb_id_to_structure_source(
    value: str,
    fetch_timeout: float,
    format_override: str | None = None,
) -> StructureSource:
    try:
        return pdb_id_to_structure_source(value, fetch_timeout, format_override=format_override)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc


def _alphafold_id_to_structure_source(
    value: str,
    fetch_timeout: float,
    format_override: str | None = None,
) -> StructureSource:
    try:
        return alphafold_id_to_structure_source(
            value, fetch_timeout, format_override=format_override
        )
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc


def _read_stdin_structure_source(format_override: str | None) -> StructureSource:
    if format_override is None:
        raise typer.BadParameter("--format is required when reading a structure from stdin ('-').")
    fmt = format_override.lower()
    if fmt not in SUPPORTED_FORMAT_KEYS:
        raise typer.BadParameter(
            f"Unsupported --format {format_override!r}. Choose from {', '.join(SUPPORTED_FORMAT_KEYS)}."
        )

    stdin_file_env = os.environ.get(_STDIN_FILE_ENV)
    if stdin_file_env:
        stdin_path = Path(stdin_file_env)
        try:
            data = stdin_path.read_bytes()
        finally:
            with contextlib.suppress(OSError):
                stdin_path.unlink(missing_ok=True)
    else:
        data = sys.stdin.buffer.read()

    if not data:
        err_console.print("[bold red]Error:[/] No input received on stdin.")
        raise typer.Exit(code=1)

    return StructureSource(display_name=f"stdin.{fmt}", format_key=fmt, inline_bytes=data)


def _resolve_structure_source(
    value: str,
    fetch_timeout: float,
    format_override: str | None = None,
) -> StructureSource | None:
    if value == "-":
        return _read_stdin_structure_source(format_override)
    try:
        return resolve_source(value, fetch_timeout=fetch_timeout, format_override=format_override)
    except IsADirectoryError:
        err_console.print(
            f"[bold red]Error:[/] Path is a directory, not a structure file: "
            f"[yellow]{escape_markup(value)}[/]"
        )
        raise typer.Exit(code=1) from None
    except FileNotFoundError:
        err_console.print(f"[bold red]Error:[/] File not found: [yellow]{escape_markup(value)}[/]")
        raise typer.Exit(code=1) from None
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    except OSError:
        err_console.print(
            f"[bold red]Error:[/] Path is not a regular file: [yellow]{escape_markup(value)}[/]"
        )
        raise typer.Exit(code=1) from None


def _format_local_path_for_display(path: Path) -> str:
    try:
        home = Path.home()
        display = path.resolve().relative_to(home)
        return str(Path("~") / display)
    except Exception:
        return path.name or str(path)


def _display_source_label(source: StructureSource) -> str:
    if source.is_inline:
        return "stdin"
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


def _build_session_payload(
    *,
    structure_source: StructureSource,
    host: str,
    port: int,
    local_url: str,
    share_url: str,
    watch: bool,
    private: bool,
    auto_close: bool,
    pid: int,
) -> dict[str, Any]:
    return {
        "url": local_url,
        "share_url": share_url,
        "host": host,
        "port": port,
        "source": _display_source_label(structure_source),
        "name": structure_source.display_name,
        "format": structure_source.format_key,
        "source_kind": structure_source.source_kind,
        "watch": watch,
        "private": private,
        "auto_close": auto_close,
        "pid": pid,
    }


def _emit_ready_ui(
    *,
    structure_source: StructureSource,
    host: str,
    port: int,
    local_url: str,
    share_url: str,
    watch: bool,
    private: bool,
    effective_auto_close: bool,
    heartbeat_timeout: float,
    qr: bool,
    json_output: bool,
    no_open: bool,
    pid: int,
    detached: bool = False,
) -> None:
    if json_output:
        payload = _build_session_payload(
            structure_source=structure_source,
            host=host,
            port=port,
            local_url=local_url,
            share_url=share_url,
            watch=watch,
            private=private,
            auto_close=effective_auto_close,
            pid=pid,
        )
        print(json.dumps(payload), flush=True)
    else:
        info = _build_info_grid(
            source=structure_source,
            format_key=structure_source.format_key,
            host=host,
            local_url=local_url,
            share_url=share_url,
        )
        subtitle_parts = [f"detached (PID {pid})" if detached else "Ctrl+C to quit"]
        if private:
            subtitle_parts.append("private link")
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
        if qr:
            console.print(_render_terminal_qr(share_url))

    if not no_open:
        headless_reason = _detect_headless_reason()
        if headless_reason == "ssh":
            if not json_output:
                console.print(
                    f"[dim]SSH session detected; skipping browser launch. "
                    f"Forward locally with:[/] [bold]{escape_markup(_ssh_forward_hint(host, port))}[/]"
                )
        elif headless_reason == "headless":
            if not json_output:
                console.print(
                    f"[dim]Headless environment detected; skipping browser launch. "
                    f"Open[/] [underline]{escape_markup(local_url)}[/] [dim]manually.[/]"
                )
        elif not webbrowser.open(local_url) and not json_output:
            err_console.print(
                f"[yellow]Warning:[/] Could not auto-open a browser. "
                f"Open [underline]{escape_markup(local_url)}[/] manually."
            )


def _write_ready_file(ready_file_env: str, payload: dict[str, Any]) -> None:
    ready_path = Path(ready_file_env)
    tmp_path = ready_path.with_suffix(".tmp")
    tmp_path.write_text(json.dumps(payload), encoding="utf-8")
    tmp_path.replace(ready_path)


def _read_ready_file(ready_path: Path) -> dict[str, Any] | None:
    if not ready_path.exists():
        return None
    try:
        raw = ready_path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _spawn_detached_server(
    *,
    source: str,
    structure_source: StructureSource,
    port: int | None,
    host: str,
    watch: bool,
    auto_close: bool | None,
    idle_timeout: float | None,
    fetch_timeout: float,
    format_override: str | None,
    base_url: str | None,
    private: bool,
) -> dict[str, Any]:
    cmd = [sys.executable, "-m", "molbo", source, "--host", host, "--no-open"]
    if port is not None:
        cmd.extend(["--port", str(port)])
    if watch:
        cmd.append("--watch")
    if auto_close is True:
        cmd.append("--auto-close")
    elif auto_close is False:
        cmd.append("--keep-alive")
    if idle_timeout is not None:
        cmd.extend(["--idle-timeout", str(idle_timeout)])
    if fetch_timeout != 30.0:
        cmd.extend(["--fetch-timeout", str(fetch_timeout)])
    if format_override is not None:
        cmd.extend(["--format", format_override])
    if base_url is not None:
        cmd.extend(["--base-url", base_url])
    if private:
        cmd.append("--private")

    handoff_dir = Path(tempfile.mkdtemp(prefix="molbo-detach-"))
    ready_path = handoff_dir / "ready.json"

    env = os.environ.copy()
    env[_READY_FILE_ENV] = str(ready_path)
    if structure_source.inline_bytes is not None:
        stdin_path = handoff_dir / "stdin.bin"
        stdin_path.write_bytes(structure_source.inline_bytes)
        env[_STDIN_FILE_ENV] = str(stdin_path)

    popen_kwargs: dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "env": env,
    }
    if os.name == "nt":
        popen_kwargs["creationflags"] = getattr(
            subprocess, "CREATE_NEW_PROCESS_GROUP", 0
        ) | getattr(subprocess, "DETACHED_PROCESS", 0)
    else:
        popen_kwargs["start_new_session"] = True

    try:
        proc = subprocess.Popen(cmd, **popen_kwargs)

        deadline = time.monotonic() + _DETACH_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            payload = _read_ready_file(ready_path)
            if payload is not None:
                if "error" in payload:
                    err_console.print(f"[bold red]Error:[/] {escape_markup(str(payload['error']))}")
                    raise typer.Exit(code=int(payload.get("exit_code", 1)))
                return payload
            exit_code = proc.poll()
            if exit_code is not None:
                payload = _read_ready_file(ready_path)
                if payload is not None and "error" in payload:
                    err_console.print(f"[bold red]Error:[/] {escape_markup(str(payload['error']))}")
                    raise typer.Exit(code=int(payload.get("exit_code", exit_code or 1)))
                err_console.print(
                    f"[bold red]Error:[/] Detached molbo server exited with code {exit_code}."
                )
                raise typer.Exit(code=exit_code or 1)
            time.sleep(0.02)

        proc.terminate()
        err_console.print("[bold red]Error:[/] Timed out waiting for detached molbo server.")
        raise typer.Exit(code=1)
    finally:
        shutil.rmtree(handoff_dir, ignore_errors=True)


@app.command(epilog=_HELP_EPILOG)
def view(
    source: Annotated[
        str,
        typer.Argument(
            help=(
                "Local path, '-' for stdin, PDB ID (e.g. 1crn or pdb_00001crn), "
                "AlphaFold ID (e.g. af:P12345 or AF-P12345-F1), or http(s) URL."
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
            help="Explicit structure format for extensionless inputs, stdin, or database IDs.",
        ),
    ] = None,
    base_url: Annotated[
        str | None,
        typer.Option(
            "--base-url",
            help="Public viewer URL to display and encode in the QR modal, e.g. https://mol.example.com or https://mol.example.com/s/demo.",
        ),
    ] = None,
    private: Annotated[
        bool,
        typer.Option(
            "--private",
            help="Mount the session under a random unguessable URL path prefix.",
        ),
    ] = False,
    qr: Annotated[
        bool,
        typer.Option(
            "--qr",
            help="Print a scannable QR code for the share URL in the terminal.",
        ),
    ] = False,
    json_output: Annotated[
        bool,
        typer.Option(
            "--json",
            help="Print session info as a single JSON line to stdout once the server is listening.",
        ),
    ] = False,
    detach: Annotated[
        bool,
        typer.Option(
            "--detach",
            help="Run the server in a detached background process and exit once it is listening.",
        ),
    ] = False,
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

    normalized_base_url, base_path = _normalize_base_url(base_url)
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

    if watch and (structure_source.is_remote or structure_source.is_inline):
        raise typer.BadParameter("--watch is only supported for local structure files.")

    effective_auto_close = bool(auto_close) or idle_timeout is not None
    heartbeat_timeout = idle_timeout if idle_timeout is not None else 15.0

    if detach:
        payload = _spawn_detached_server(
            source=source,
            structure_source=structure_source,
            port=port,
            host=host,
            watch=watch,
            auto_close=auto_close,
            idle_timeout=idle_timeout,
            fetch_timeout=fetch_timeout,
            format_override=format_override_str,
            base_url=normalized_base_url,
            private=private,
        )
        _emit_ready_ui(
            structure_source=structure_source,
            host=str(payload["host"]),
            port=int(payload["port"]),
            local_url=str(payload["url"]),
            share_url=str(payload["share_url"]),
            watch=watch,
            private=private,
            effective_auto_close=effective_auto_close,
            heartbeat_timeout=heartbeat_timeout,
            qr=qr,
            json_output=json_output,
            no_open=no_open,
            pid=int(payload["pid"]),
            detached=True,
        )
        return

    if private:
        normalized_base_url, base_path = _apply_private_path(normalized_base_url, base_path)

    ready_file_env = os.environ.get(_READY_FILE_ENV)
    shutdown_event = threading.Event()
    try:
        server = make_server(
            structure_source,
            port if port is not None else 0,
            host=host,
            shutdown_event=shutdown_event,
            auto_close=effective_auto_close,
            share_url=normalized_base_url,
            base_path=base_path,
            idle_timeout=heartbeat_timeout,
            watch=watch,
        )
    except OSError as exc:
        port_label = str(port) if port is not None else "auto"
        if ready_file_env:
            _write_ready_file(
                ready_file_env,
                {
                    "error": f"Could not bind local server on {host}:{port_label}: {exc}",
                    "exit_code": 1,
                },
            )
        else:
            err_console.print(
                f"[bold red]Error:[/] Could not bind local server on "
                f"[yellow]{escape_markup(host)}:{port_label}[/]."
            )
            err_console.print(f"[dim]{escape_markup(str(exc))}[/dim]")
        raise typer.Exit(code=1) from exc

    chosen_port = server.server_address[1]
    local_origin = f"http://{format_host_for_url(local_access_host(host))}:{chosen_port}"
    local_url = _viewer_url(local_origin, base_path)
    fallback_share_url = getattr(server.RequestHandlerClass, "fallback_share_url", None)
    share_url = normalized_base_url or fallback_share_url or local_url

    server_thread = serve_background(server)

    if effective_auto_close:
        start_heartbeat_watchdog(
            server.RequestHandlerClass, shutdown_event, timeout=heartbeat_timeout
        )

    if ready_file_env:
        payload = _build_session_payload(
            structure_source=structure_source,
            host=host,
            port=chosen_port,
            local_url=local_url,
            share_url=share_url,
            watch=watch,
            private=private,
            auto_close=effective_auto_close,
            pid=os.getpid(),
        )
        _write_ready_file(ready_file_env, payload)
    else:
        _emit_ready_ui(
            structure_source=structure_source,
            host=host,
            port=chosen_port,
            local_url=local_url,
            share_url=share_url,
            watch=watch,
            private=private,
            effective_auto_close=effective_auto_close,
            heartbeat_timeout=heartbeat_timeout,
            qr=qr,
            json_output=json_output,
            no_open=no_open,
            pid=os.getpid(),
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
        if not ready_file_env and not json_output:
            console.print("\n[dim]Shutting down…[/dim]")
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=1.0)


__all__ = ["SUPPORTED_EXTENSIONS", "SUPPORTED_FORMAT_KEYS", "SUPPORTED_SUFFIXES", "app", "view"]

if __name__ == "__main__":
    app()
