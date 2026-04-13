from __future__ import annotations

import gzip
import os
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from typer.testing import CliRunner

from molbo.cli import _resolve_structure_format, _resolve_structure_source, app
from molbo.server import StructureSource, make_server, render_viewer_html, serve_background, start_heartbeat_watchdog


class RenderViewerHtmlTests(unittest.TestCase):
    def test_render_viewer_html_escapes_filename_and_serializes_script_values(self) -> None:
        filename = 'bad"><script>alert(1)</script>.pdb'
        rendered = render_viewer_html(
            structure_name=filename,
            format_key="pdb",
            file_url="http://localhost:1234/structure",
            auto_close=False,
            viewer_style="publication",
        )

        self.assertIn("bad&quot;&gt;&lt;script&gt;alert(1)&lt;/script&gt;.pdb", rendered)
        self.assertNotIn(filename, rendered)
        self.assertIn('var FILE_URL = "http://localhost:1234/structure";', rendered)
        self.assertIn('var FORMAT_KEY = "pdb";', rendered)
        self.assertIn("var AUTO_CLOSE = false;", rendered)
        self.assertIn('var VIEWER_STYLE = "publication";', rendered)


class ServerIntegrationTests(unittest.TestCase):
    def test_server_binds_ephemeral_port_and_serves_html_and_structure(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            structure_path = Path(tmpdir) / "example.pdb"
            structure_bytes = b"HEADER test\nEND\n"
            structure_path.write_bytes(structure_bytes)
            shutdown_event = threading.Event()
            server = make_server(
                StructureSource(display_name=structure_path.name, format_key="pdb", local_path=structure_path),
                0,
                shutdown_event=shutdown_event,
            )
            thread = serve_background(server)

            try:
                port = server.server_address[1]
                self.assertGreater(port, 0)

                with urllib.request.urlopen(f"http://127.0.0.1:{port}/") as response:
                    self.assertEqual(response.status, 200)
                    html = response.read().decode("utf-8")
                self.assertIn("example.pdb", html)
                self.assertIn("var AUTO_CLOSE = false;", html)

                with urllib.request.urlopen(f"http://127.0.0.1:{port}/structure") as response:
                    self.assertEqual(response.status, 200)
                    self.assertEqual(response.read(), structure_bytes)
            finally:
                shutdown_event.set()
                server.shutdown()
                server.server_close()
                thread.join(timeout=1.0)

    def test_bye_only_stops_server_when_auto_close_is_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            structure_path = Path(tmpdir) / "example.pdb"
            structure_path.write_text("HEADER test\nEND\n", encoding="utf-8")

            keep_alive_event = threading.Event()
            keep_alive_server = make_server(
                StructureSource(display_name=structure_path.name, format_key="pdb", local_path=structure_path),
                0,
                shutdown_event=keep_alive_event,
                auto_close=False,
            )
            keep_alive_thread = serve_background(keep_alive_server)

            auto_close_event = threading.Event()
            auto_close_server = make_server(
                StructureSource(display_name=structure_path.name, format_key="pdb", local_path=structure_path),
                0,
                shutdown_event=auto_close_event,
                auto_close=True,
            )
            auto_close_thread = serve_background(auto_close_server)

            try:
                keep_alive_port = keep_alive_server.server_address[1]
                auto_close_port = auto_close_server.server_address[1]

                keep_alive_request = urllib.request.Request(
                    f"http://127.0.0.1:{keep_alive_port}/bye",
                    method="POST",
                )
                auto_close_request = urllib.request.Request(
                    f"http://127.0.0.1:{auto_close_port}/bye",
                    method="POST",
                )

                with urllib.request.urlopen(keep_alive_request) as response:
                    self.assertEqual(response.status, 204)
                with urllib.request.urlopen(auto_close_request) as response:
                    self.assertEqual(response.status, 204)

                self.assertFalse(keep_alive_event.wait(0.2))
                self.assertTrue(auto_close_event.wait(1.0))
            finally:
                keep_alive_event.set()
                keep_alive_server.shutdown()
                keep_alive_server.server_close()
                keep_alive_thread.join(timeout=1.0)

                auto_close_event.set()
                auto_close_server.shutdown()
                auto_close_server.server_close()
                auto_close_thread.join(timeout=1.0)

    def test_heartbeat_watchdog_sets_shutdown_event_after_timeout(self) -> None:
        shutdown_event = threading.Event()
        handler_cls = type("FakeHandler", (), {"last_heartbeat": time.monotonic() - 10})

        watcher = start_heartbeat_watchdog(handler_cls, shutdown_event, timeout=0.1)

        self.assertTrue(shutdown_event.wait(1.0))
        watcher.join(timeout=1.0)


class CliTests(unittest.TestCase):
    def test_cli_accepts_gzip_suffixes(self) -> None:
        self.assertEqual(_resolve_structure_format(Path("example.pdb.gz")), "pdb")
        self.assertEqual(_resolve_structure_format(Path("example.cif.gz")), "cif")
        self.assertEqual(_resolve_structure_format(Path("example.mmcif.gz")), "mmcif")
        self.assertEqual(_resolve_structure_format(Path("example.bcif.gz")), "bcif")

    def test_cli_accepts_http_source(self) -> None:
        source = _resolve_structure_source("https://example.test/structures/example.pdb.gz")
        self.assertIsNotNone(source)
        assert source is not None
        self.assertEqual(source.display_name, "example.pdb.gz")
        self.assertEqual(source.format_key, "pdb")
        self.assertEqual(source.remote_url, "https://example.test/structures/example.pdb.gz")

    def test_cli_rejects_unsupported_extensions(self) -> None:
        runner = CliRunner()
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "bad.txt"
            path.write_text("not a structure", encoding="utf-8")

            result = runner.invoke(app, [str(path)])

        self.assertEqual(result.exit_code, 1)
        self.assertIn("Unsupported file type", result.output)

    def test_cli_process_serves_structure_and_shuts_down_on_sigint(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            structure_path = Path(tmpdir) / "example.pdb"
            structure_path.write_text("HEADER cli smoke\nEND\n", encoding="utf-8")

            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.bind(("127.0.0.1", 0))
                port = sock.getsockname()[1]

            env = os.environ.copy()
            env["PYTHONUNBUFFERED"] = "1"
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "molbo",
                    str(structure_path),
                    "--no-open",
                    "--port",
                    str(port),
                ],
                cwd=Path(__file__).resolve().parents[1],
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

            try:
                deadline = time.monotonic() + 5.0
                while True:
                    if process.poll() is not None:
                        self.fail(f"molbo exited early with code {process.returncode}")

                    try:
                        with urllib.request.urlopen(f"http://127.0.0.1:{port}/structure", timeout=0.5) as response:
                            body = response.read()
                        break
                    except Exception:
                        if time.monotonic() >= deadline:
                            self.fail("molbo did not start serving within 5 seconds")
                        time.sleep(0.1)

                self.assertIn(b"HEADER cli smoke", body)
            finally:
                if process.poll() is None:
                    process.send_signal(signal.SIGINT)
                    process.wait(timeout=5.0)


class GzipServerTests(unittest.TestCase):
    def test_server_sets_gzip_content_encoding_for_compressed_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            structure_path = Path(tmpdir) / "example.pdb.gz"
            raw_structure = b"HEADER gzip test\nEND\n"
            structure_path.write_bytes(gzip.compress(raw_structure))
            shutdown_event = threading.Event()
            server = make_server(
                StructureSource(display_name=structure_path.name, format_key="pdb", local_path=structure_path),
                0,
                shutdown_event=shutdown_event,
            )
            thread = serve_background(server)

            try:
                port = server.server_address[1]
                request = urllib.request.Request(f"http://127.0.0.1:{port}/structure")
                with urllib.request.urlopen(request) as response:
                    self.assertEqual(response.status, 200)
                    self.assertEqual(response.headers.get("Content-Type"), "chemical/x-pdb")
                    self.assertEqual(response.headers.get("Content-Encoding"), "gzip")
                    self.assertEqual(gzip.decompress(response.read()), raw_structure)
            finally:
                shutdown_event.set()
                server.shutdown()
                server.server_close()
                thread.join(timeout=1.0)


class RemoteServerTests(unittest.TestCase):
    def test_local_proxy_serves_remote_structure(self) -> None:
        remote_body = b"HEADER remote test\nEND\n"

        class RemoteHandler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: object) -> None:  # noqa: A002
                pass

            def do_GET(self) -> None:  # noqa: N802
                if self.path != "/remote/example.pdb":
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header("Content-Type", "chemical/x-pdb")
                self.send_header("Content-Length", str(len(remote_body)))
                self.end_headers()
                self.wfile.write(remote_body)

        upstream = HTTPServer(("127.0.0.1", 0), RemoteHandler)
        upstream_thread = threading.Thread(target=upstream.serve_forever, daemon=True)
        upstream_thread.start()

        shutdown_event = threading.Event()
        proxy = make_server(
            StructureSource(
                display_name="example.pdb",
                format_key="pdb",
                remote_url=f"http://127.0.0.1:{upstream.server_address[1]}/remote/example.pdb",
            ),
            0,
            shutdown_event=shutdown_event,
        )
        proxy_thread = serve_background(proxy)

        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{proxy.server_address[1]}/structure") as response:
                self.assertEqual(response.status, 200)
                self.assertEqual(response.headers.get("Content-Type"), "chemical/x-pdb")
                self.assertEqual(response.read(), remote_body)
        finally:
            shutdown_event.set()
            proxy.shutdown()
            proxy.server_close()
            proxy_thread.join(timeout=1.0)

            upstream.shutdown()
            upstream.server_close()
            upstream_thread.join(timeout=1.0)


if __name__ == "__main__":
    unittest.main()
