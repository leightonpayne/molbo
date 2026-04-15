from __future__ import annotations

import gzip
import tempfile
import threading
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from typer.testing import CliRunner

from molbo.cli import _resolve_structure_source, app
from molbo.server import (
    StructureSource,
    make_server,
    render_viewer_html,
    serve_background,
)


class RenderViewerHtmlTests(unittest.TestCase):
    def test_render_viewer_html_escapes_filename_and_serializes_script_values(self) -> None:
        filename = 'bad"><script>alert(1)</script>.pdb'
        rendered = render_viewer_html(
            structure_name=filename,
            format_key="pdb",
            file_url="/structure",
            auto_close=False,
            share_url="https://mol.example.com",
        )

        self.assertIn("bad&quot;&gt;&lt;script&gt;alert(1)&lt;/script&gt;.pdb", rendered)
        self.assertNotIn(filename, rendered)
        self.assertIn('var FILE_URL = "/structure";', rendered)
        self.assertIn('var FORMAT_KEY = "pdb";', rendered)
        self.assertIn("var AUTO_CLOSE = false;", rendered)
        self.assertIn('var SHARE_URL = "https://mol.example.com" || window.location.href;', rendered)
        self.assertIn('id="qr-button"', rendered)
        self.assertIn('id="qr-modal"', rendered)
        self.assertIn('src="/assets/molstar-4.5.0.js"', rendered)
        self.assertIn('href="/assets/molstar-4.5.0.css"', rendered)
        self.assertIn("applyPublicationLook", rendered)
        self.assertIn("polymer-cartoon+ligand-ball-and-stick", rendered)


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
                self.assertEqual(server.server_address[0], "127.0.0.1")
                port = server.server_address[1]
                self.assertGreater(port, 0)

                with urllib.request.urlopen(f"http://127.0.0.1:{port}/") as response:
                    self.assertEqual(response.status, 200)
                    html = response.read().decode("utf-8")
                self.assertIn("example.pdb", html)
                self.assertIn("var AUTO_CLOSE = false;", html)
                self.assertIn('var FILE_URL = "/structure";', html)

                with urllib.request.urlopen(f"http://127.0.0.1:{port}/assets/molstar-4.5.0.css") as response:
                    self.assertEqual(response.status, 200)
                    self.assertIn("text/css", response.headers.get("Content-Type", ""))

                with urllib.request.urlopen(f"http://127.0.0.1:{port}/structure") as response:
                    self.assertEqual(response.status, 200)
                    self.assertEqual(response.read(), structure_bytes)
            finally:
                shutdown_event.set()
                server.shutdown()
                server.server_close()
                thread.join(timeout=1.0)


class CliTests(unittest.TestCase):
    def test_cli_accepts_pdb_id_source(self) -> None:
        source = _resolve_structure_source("1crn", fetch_timeout=9.0)
        self.assertIsNotNone(source)
        assert source is not None
        self.assertEqual(source.display_name, "1CRN.cif")
        self.assertEqual(source.format_key, "cif")
        self.assertEqual(source.remote_url, "https://files.rcsb.org/download/1CRN.cif")
        self.assertEqual(source.fetch_timeout, 9.0)

    def test_cli_accepts_remote_url_with_explicit_format(self) -> None:
        source = _resolve_structure_source(
            "https://example.org/download?id=1crn",
            fetch_timeout=9.0,
            format_override="cif",
        )
        self.assertIsNotNone(source)
        assert source is not None
        self.assertEqual(source.display_name, "download")
        self.assertEqual(source.format_key, "cif")
        self.assertEqual(source.remote_url, "https://example.org/download?id=1crn")
        self.assertEqual(source.fetch_timeout, 9.0)

    def test_cli_rejects_unsupported_extensions(self) -> None:
        runner = CliRunner()
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "bad.txt"
            path.write_text("not a structure", encoding="utf-8")

            result = runner.invoke(app, [str(path)])

        self.assertEqual(result.exit_code, 1)
        self.assertIn("Unsupported file type", result.output)


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
