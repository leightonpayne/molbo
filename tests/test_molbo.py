from __future__ import annotations

import gzip
import http.client
import io
import json
import re
import socket
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from email.message import Message
from http.server import BaseHTTPRequestHandler, HTTPServer
from importlib.metadata import version
from pathlib import Path
from unittest import mock

import typer
from typer.testing import CliRunner

from molbo import __version__
from molbo.cli import (
    _build_info_grid,
    _detect_headless_reason,
    _display_source_label,
    _normalize_base_url,
    _resolve_structure_source,
    _ssh_forward_hint,
    app,
)
from molbo.server import (
    StructureSource,
    _accepts_gzip,
    _guess_mime,
    compute_heartbeat_interval_ms,
    local_access_host,
    make_server,
    render_viewer_html,
    serve_background,
    start_heartbeat_watchdog,
)

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _plain_output(text: str) -> str:
    return _ANSI_RE.sub("", text)


class RenderViewerHtmlTests(unittest.TestCase):
    def test_render_viewer_html_escapes_filename_and_serializes_script_values(self) -> None:
        filename = 'bad"><script>alert(1)</script>.pdb'
        rendered = render_viewer_html(
            structure_name=filename,
            format_key="pdb",
            file_url="/structure",
            auto_close=False,
            share_url="https://mol.example.com",
            heartbeat_interval_ms=3000,
        )

        self.assertIn("bad&quot;&gt;&lt;script&gt;alert(1)&lt;/script&gt;.pdb", rendered)
        self.assertNotIn(filename, rendered)
        self.assertIn(
            'var STRUCTURE_NAME = "bad\\"\\u003e\\u003cscript\\u003ealert(1)\\u003c/script\\u003e.pdb";',
            rendered,
        )
        self.assertIn('var FILE_URL = "/structure";', rendered)
        self.assertIn('var FORMAT_KEY = "pdb";', rendered)
        self.assertIn("var AUTO_CLOSE = false;", rendered)
        self.assertIn(
            'var SHARE_URL = "https://mol.example.com" || window.location.href;', rendered
        )
        self.assertIn("var HEARTBEAT_INTERVAL_MS = 3000;", rendered)
        self.assertIn("{ label: STRUCTURE_NAME }", rendered)
        self.assertIn('id="qr-button"', rendered)
        self.assertIn('id="qr-modal"', rendered)
        self.assertIn('id="qr-copy"', rendered)
        self.assertIn('id="qr-close"', rendered)
        self.assertIn('id="reload-button"', rendered)
        self.assertIn('href="/favicon.ico"', rendered)
        self.assertIn("var WATCH_ENABLED = false;", rendered)
        self.assertIn('var VERSION_URL = "/version";', rendered)
        self.assertIn("'pdbqt': 'pdbqt'", rendered)
        self.assertIn("'sdf':   'sdf'", rendered)
        self.assertIn("'mol2':  'mol2'", rendered)
        self.assertIn("'gro':   'gro'", rendered)
        self.assertIn('src="/assets/molstar-4.5.0.js"', rendered)
        self.assertIn('href="/assets/molstar-4.5.0.css"', rendered)
        self.assertIn("applyPublicationLook", rendered)

    def test_render_viewer_html_escapes_script_breakout_and_prevents_double_replacement(
        self,
    ) -> None:
        rendered = render_viewer_html(
            structure_name="weird-{{ file_url_json }}.pdb",
            format_key="pdb",
            file_url="/</script><script>alert(1)</script>/structure",
            auto_close=False,
            share_url="https://mol.example.com/</script><script>alert(2)</script>",
        )

        self.assertIn("weird-{{ file_url_json }}.pdb", rendered)
        self.assertNotIn("</script><script>alert(1)", rendered)
        self.assertNotIn("</script><script>alert(2)", rendered)
        self.assertIn("\\u003c/script\\u003e\\u003cscript\\u003ealert(1)", rendered)

    def test_render_viewer_html_serializes_prefixed_urls(self) -> None:
        rendered = render_viewer_html(
            structure_name="example.pdb",
            format_key="pdb",
            file_url="/s/demo/structure",
            auto_close=True,
            share_url="https://mol.example.com/s/demo/",
            molstar_css_url="/s/demo/assets/molstar-4.5.0.css",
            molstar_js_url="/s/demo/assets/molstar-4.5.0.js",
            qrcode_js_url="/s/demo/assets/qrcode-generator-2.0.4.min.js",
            heartbeat_url="/s/demo/heartbeat",
            bye_url="/s/demo/bye",
            watch=True,
            version_url="/s/demo/version",
        )

        self.assertIn('var FILE_URL = "/s/demo/structure";', rendered)
        self.assertIn("var AUTO_CLOSE = true;", rendered)
        self.assertIn("var WATCH_ENABLED = true;", rendered)
        self.assertIn('var VERSION_URL = "/s/demo/version";', rendered)
        self.assertIn(
            'var SHARE_URL = "https://mol.example.com/s/demo/" || window.location.href;', rendered
        )
        self.assertIn('var HEARTBEAT_URL = "/s/demo/heartbeat";', rendered)
        self.assertIn('var BYE_URL = "/s/demo/bye";', rendered)
        self.assertIn('src="/s/demo/assets/molstar-4.5.0.js"', rendered)
        self.assertIn('href="/s/demo/assets/molstar-4.5.0.css"', rendered)


class ServerIntegrationTests(unittest.TestCase):
    def test_server_binds_ephemeral_port_and_serves_html_and_structure(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            structure_path = Path(tmpdir) / "example.pdb"
            structure_bytes = b"HEADER test\nEND\n"
            structure_path.write_bytes(structure_bytes)
            shutdown_event = threading.Event()
            server = make_server(
                StructureSource(
                    display_name=structure_path.name, format_key="pdb", local_path=structure_path
                ),
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

                head_req = urllib.request.Request(f"http://127.0.0.1:{port}/", method="HEAD")
                with urllib.request.urlopen(head_req) as response:
                    self.assertEqual(response.status, 200)
                    self.assertEqual(response.read(), b"")
                    self.assertGreater(int(response.headers.get("Content-Length", "0")), 0)

                with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/assets/molstar-4.5.0.css"
                ) as response:
                    self.assertEqual(response.status, 200)
                    self.assertIn("text/css", response.headers.get("Content-Type", ""))
                    self.assertEqual(response.headers.get("Vary"), "Accept-Encoding")
                    self.assertIsNone(response.headers.get("Content-Encoding"))
                    raw_css = response.read()

                gz_req = urllib.request.Request(
                    f"http://127.0.0.1:{port}/assets/molstar-4.5.0.css",
                    headers={"Accept-Encoding": "gzip, deflate"},
                )
                with urllib.request.urlopen(gz_req) as response:
                    self.assertEqual(response.status, 200)
                    self.assertEqual(response.headers.get("Content-Encoding"), "gzip")
                    self.assertEqual(response.headers.get("Vary"), "Accept-Encoding")
                    self.assertEqual(gzip.decompress(response.read()), raw_css)

                no_gz_req = urllib.request.Request(
                    f"http://127.0.0.1:{port}/assets/molstar-4.5.0.css",
                    headers={"Accept-Encoding": "gzip;q=0"},
                )
                with urllib.request.urlopen(no_gz_req) as response:
                    self.assertEqual(response.status, 200)
                    self.assertIsNone(response.headers.get("Content-Encoding"))
                    self.assertEqual(response.read(), raw_css)

                with urllib.request.urlopen(f"http://127.0.0.1:{port}/structure") as response:
                    self.assertEqual(response.status, 200)
                    self.assertEqual(response.read(), structure_bytes)

                with urllib.request.urlopen(f"http://127.0.0.1:{port}/favicon.ico") as response:
                    self.assertEqual(response.status, 200)
                    self.assertEqual(response.headers.get("Content-Type"), "image/svg+xml")
                    self.assertIn(b"<svg", response.read())

                head_fav = urllib.request.Request(
                    f"http://127.0.0.1:{port}/favicon.ico", method="HEAD"
                )
                with urllib.request.urlopen(head_fav) as response:
                    self.assertEqual(response.status, 200)
                    self.assertEqual(response.read(), b"")

                with urllib.request.urlopen(f"http://127.0.0.1:{port}/version") as response:
                    self.assertEqual(response.status, 200)
                    v1 = json.loads(response.read().decode("utf-8"))["version"]
                self.assertIsNotNone(v1)

                structure_path.write_bytes(b"HEADER updated structure content\nEND\n")
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/version") as response:
                    v2 = json.loads(response.read().decode("utf-8"))["version"]
                self.assertNotEqual(v1, v2)

                with self.assertRaises(urllib.error.HTTPError) as ctx:
                    urllib.request.urlopen(f"http://127.0.0.1:{port}/does-not-exist")
                self.assertEqual(ctx.exception.code, 404)
                ctx.exception.close()

                post_unknown = urllib.request.Request(
                    f"http://127.0.0.1:{port}/unknown", method="POST"
                )
                with self.assertRaises(urllib.error.HTTPError) as ctx:
                    urllib.request.urlopen(post_unknown)
                self.assertEqual(ctx.exception.code, 404)
                ctx.exception.close()
            finally:
                shutdown_event.set()
                server.shutdown()
                server.server_close()
                thread.join(timeout=1.0)

    def test_server_serves_extensionless_file_using_explicit_format(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            structure_path = Path(tmpdir) / "structure"
            structure_bytes = b"data_example\n#\n"
            structure_path.write_bytes(structure_bytes)
            source = _resolve_structure_source(
                str(structure_path),
                fetch_timeout=30.0,
                format_override="cif",
            )
            self.assertIsNotNone(source)
            assert source is not None

            server = make_server(source, 0)
            thread = serve_background(server)

            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{server.server_address[1]}/structure"
                ) as response:
                    self.assertEqual(response.status, 200)
                    self.assertEqual(response.headers.get("Content-Type"), "chemical/x-cif")
                    self.assertEqual(response.read(), structure_bytes)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=1.0)

    def test_server_returns_500_when_local_file_cannot_be_opened(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            structure_path = Path(tmpdir) / "example.pdb"
            structure_path.write_bytes(b"HEADER test\nEND\n")
            server = make_server(
                StructureSource(
                    display_name=structure_path.name, format_key="pdb", local_path=structure_path
                ),
                0,
            )
            thread = serve_background(server)

            try:
                with (
                    mock.patch.object(Path, "open", side_effect=PermissionError("denied")),
                    self.assertRaises(urllib.error.HTTPError) as ctx,
                ):
                    urllib.request.urlopen(f"http://127.0.0.1:{server.server_address[1]}/structure")
                self.assertEqual(ctx.exception.code, 500)
                ctx.exception.close()
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=1.0)

    def test_server_binds_ipv6_loopback(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            structure_path = Path(tmpdir) / "example.pdb"
            structure_path.write_bytes(b"HEADER ipv6\nEND\n")
            try:
                server = make_server(
                    StructureSource(
                        display_name=structure_path.name,
                        format_key="pdb",
                        local_path=structure_path,
                    ),
                    0,
                    host="::1",
                )
            except OSError:
                self.skipTest("IPv6 loopback is unavailable in this environment")

            thread = serve_background(server)
            try:
                self.assertEqual(server.address_family, socket.AF_INET6)
                port = server.server_address[1]
                conn = http.client.HTTPConnection("::1", port, timeout=5)
                try:
                    conn.request("GET", "/structure")
                    response = conn.getresponse()
                    self.assertEqual(response.status, 200)
                    self.assertEqual(response.read(), b"HEADER ipv6\nEND\n")
                finally:
                    conn.close()
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=1.0)


class LifecycleTests(unittest.TestCase):
    def test_bye_grace_allows_page_reload_and_shuts_down_when_idle(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            structure_path = Path(tmpdir) / "example.pdb"
            structure_path.write_bytes(b"HEADER test\nEND\n")
            shutdown_event = threading.Event()
            server = make_server(
                StructureSource(
                    display_name=structure_path.name, format_key="pdb", local_path=structure_path
                ),
                0,
                shutdown_event=shutdown_event,
                auto_close=True,
                bye_grace=0.15,
            )
            thread = serve_background(server)
            port = server.server_address[1]

            try:
                bye_req = urllib.request.Request(
                    f"http://127.0.0.1:{port}/bye?client=tab-1", method="POST"
                )
                with urllib.request.urlopen(bye_req) as response:
                    self.assertEqual(response.status, 204)
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/") as response:
                    self.assertEqual(response.status, 200)

                self.assertFalse(shutdown_event.wait(timeout=0.25))

                bye_req2 = urllib.request.Request(
                    f"http://127.0.0.1:{port}/bye?client=tab-2", method="POST"
                )
                with urllib.request.urlopen(bye_req2) as response:
                    self.assertEqual(response.status, 204)

                self.assertTrue(shutdown_event.wait(timeout=1.0))
            finally:
                shutdown_event.set()
                server.shutdown()
                server.server_close()
                thread.join(timeout=1.0)

    def test_bye_keeps_server_alive_while_second_client_remains_active(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            structure_path = Path(tmpdir) / "example.pdb"
            structure_path.write_bytes(b"HEADER test\nEND\n")
            shutdown_event = threading.Event()
            server = make_server(
                StructureSource(
                    display_name=structure_path.name, format_key="pdb", local_path=structure_path
                ),
                0,
                shutdown_event=shutdown_event,
                auto_close=True,
                bye_grace=0.05,
            )
            thread = serve_background(server)
            port = server.server_address[1]

            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/heartbeat?client=tab-1").close()
                urllib.request.urlopen(f"http://127.0.0.1:{port}/heartbeat?client=tab-2").close()

                urllib.request.urlopen(
                    urllib.request.Request(
                        f"http://127.0.0.1:{port}/bye?client=tab-1", method="POST"
                    )
                ).close()
                self.assertFalse(shutdown_event.wait(timeout=0.15))

                urllib.request.urlopen(
                    urllib.request.Request(
                        f"http://127.0.0.1:{port}/bye?client=tab-2", method="POST"
                    )
                ).close()
                self.assertTrue(shutdown_event.wait(timeout=1.0))
            finally:
                shutdown_event.set()
                server.shutdown()
                server.server_close()
                thread.join(timeout=1.0)

    def test_heartbeat_watchdog_shuts_down_after_idle_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            structure_path = Path(tmpdir) / "example.pdb"
            structure_path.write_bytes(b"HEADER test\nEND\n")
            shutdown_event = threading.Event()
            server = make_server(
                StructureSource(
                    display_name=structure_path.name, format_key="pdb", local_path=structure_path
                ),
                0,
                shutdown_event=shutdown_event,
                auto_close=True,
                idle_timeout=0.2,
            )
            thread = serve_background(server)
            watchdog = start_heartbeat_watchdog(
                server.RequestHandlerClass, shutdown_event, timeout=0.2
            )
            port = server.server_address[1]

            try:
                time.sleep(0.1)
                urllib.request.urlopen(f"http://127.0.0.1:{port}/heartbeat?client=tab-1").close()
                self.assertFalse(shutdown_event.is_set())
                self.assertTrue(shutdown_event.wait(timeout=1.0))
            finally:
                shutdown_event.set()
                watchdog.join(timeout=1.0)
                server.shutdown()
                server.server_close()
                thread.join(timeout=1.0)

    def test_compute_heartbeat_interval_scales_with_idle_timeout(self) -> None:
        self.assertEqual(compute_heartbeat_interval_ms(1.0), 333)
        self.assertEqual(compute_heartbeat_interval_ms(2.0), 666)
        self.assertEqual(compute_heartbeat_interval_ms(15.0), 5000)
        self.assertEqual(compute_heartbeat_interval_ms(60.0), 5000)


class CliTests(unittest.TestCase):
    def test_cli_reports_installed_distribution_version(self) -> None:
        result = CliRunner().invoke(app, ["--version"])

        self.assertEqual(result.exit_code, 0)
        self.assertIn(f"molbo {version('molbo')}", result.output)
        self.assertEqual(__version__, version("molbo"))

    def test_base_url_accepts_path_prefix(self) -> None:
        viewer_url, base_path = _normalize_base_url("https://mol.example.com/s/demo/")
        self.assertEqual(viewer_url, "https://mol.example.com/s/demo/")
        self.assertEqual(base_path, "/s/demo")

    def test_base_url_rejects_invalid_urls(self) -> None:
        for bad in (
            "ftp://mol.example.com",
            "/relative/path",
            "https://mol.example.com/s/demo?foo=1",
            "https://mol.example.com/s/demo#frag",
        ):
            with self.subTest(bad=bad), self.assertRaises(typer.BadParameter):
                _normalize_base_url(bad)

    def test_cli_accepts_pdb_id_source(self) -> None:
        source = _resolve_structure_source("1crn", fetch_timeout=9.0)
        self.assertIsNotNone(source)
        assert source is not None
        self.assertEqual(source.display_name, "1CRN.cif")
        self.assertEqual(source.format_key, "cif")
        self.assertEqual(source.pdb_id, "1CRN")
        self.assertEqual(source.remote_url, "https://files.rcsb.org/download/1CRN.cif")
        self.assertEqual(source.fetch_timeout, 9.0)

    def test_cli_accepts_pdb_id_format_overrides(self) -> None:
        mmcif = _resolve_structure_source("1crn", fetch_timeout=9.0, format_override="mmcif")
        assert mmcif is not None
        self.assertEqual(mmcif.format_key, "cif")
        self.assertEqual(mmcif.remote_url, "https://files.rcsb.org/download/1CRN.cif")

        pdb = _resolve_structure_source("1crn", fetch_timeout=9.0, format_override="pdb")
        assert pdb is not None
        self.assertEqual(pdb.format_key, "pdb")
        self.assertEqual(pdb.display_name, "1CRN.pdb")
        self.assertEqual(pdb.remote_url, "https://files.rcsb.org/download/1CRN.pdb")

        bcif = _resolve_structure_source("1crn", fetch_timeout=9.0, format_override="bcif")
        assert bcif is not None
        self.assertEqual(bcif.format_key, "bcif")
        self.assertEqual(bcif.display_name, "1CRN.bcif")
        self.assertEqual(bcif.remote_url, "https://models.rcsb.org/1CRN.bcif")

    def test_cli_accepts_extended_pdb_ids_and_alphafold_ids(self) -> None:
        ext_cif = _resolve_structure_source("PDB_00001CRN", fetch_timeout=9.0)
        assert ext_cif is not None
        self.assertEqual(ext_cif.pdb_id, "pdb_00001crn")
        self.assertEqual(ext_cif.format_key, "cif")
        self.assertEqual(ext_cif.remote_url, "https://files.rcsb.org/download/pdb_00001crn.cif")
        self.assertEqual(_display_source_label(ext_cif), "PDB ID pdb_00001crn")

        ext_pdb = _resolve_structure_source(
            "pdb_00001crn", fetch_timeout=9.0, format_override="pdb"
        )
        assert ext_pdb is not None
        self.assertEqual(ext_pdb.remote_url, "https://files.rcsb.org/download/1CRN.pdb")

        with self.assertRaises(typer.BadParameter):
            _resolve_structure_source("pdb_0001abcd", fetch_timeout=9.0, format_override="pdb")

        af_short = _resolve_structure_source("af:p12345", fetch_timeout=9.0)
        assert af_short is not None
        self.assertEqual(af_short.alphafold_id, "P12345")
        self.assertEqual(af_short.format_key, "cif")
        self.assertEqual(
            af_short.alphafold_api_url, "https://alphafold.ebi.ac.uk/api/prediction/P12345"
        )
        self.assertEqual(_display_source_label(af_short), "AlphaFold DB P12345")

        af_frag = _resolve_structure_source(
            "AF-P12345-F2", fetch_timeout=9.0, format_override="bcif"
        )
        assert af_frag is not None
        self.assertEqual(af_frag.alphafold_id, "AF-P12345-F2")
        self.assertEqual(af_frag.format_key, "bcif")
        self.assertEqual(
            af_frag.alphafold_api_url, "https://alphafold.ebi.ac.uk/api/prediction/P12345"
        )

        af_ver = _resolve_structure_source("AF-P12345-F1-model_v4", fetch_timeout=9.0)
        assert af_ver is not None
        self.assertEqual(
            af_ver.remote_url, "https://alphafold.ebi.ac.uk/files/AF-P12345-F1-model_v4.cif"
        )

        with self.assertRaises(typer.BadParameter):
            _resolve_structure_source("af:P12345", fetch_timeout=9.0, format_override="sdf")

    def test_cli_accepts_additional_local_structure_formats(self) -> None:
        cases = [
            ("ligand.sdf", "sdf"),
            ("ligand.sd.gz", "sdf"),
            ("docked.pdbqt", "pdbqt"),
            ("small.mol", "mol"),
            ("small.mol2.gz", "mol2"),
            ("frame.gro", "gro"),
            ("cluster.xyz", "xyz"),
            ("legacy.ent", "pdb"),
        ]
        with tempfile.TemporaryDirectory() as tmpdir:
            for filename, expected_fmt in cases:
                with self.subTest(filename=filename):
                    file_path = Path(tmpdir) / filename
                    file_path.write_bytes(b"data\n")
                    source = _resolve_structure_source(str(file_path), fetch_timeout=5.0)
                    assert source is not None
                    self.assertEqual(source.format_key, expected_fmt)

    def test_cli_rejects_watch_for_remote_sources(self) -> None:
        runner = CliRunner()
        result = runner.invoke(app, ["1crn", "--watch"])
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn(
            "--watch is only supported for local structure files", _plain_output(result.output)
        )

    def test_detect_headless_reason_and_ssh_hint(self) -> None:
        self.assertEqual(_detect_headless_reason({"SSH_CONNECTION": "1 2 3 4"}, "darwin"), "ssh")
        self.assertEqual(_detect_headless_reason({"SSH_TTY": "/dev/pts/0"}, "linux"), "ssh")
        self.assertEqual(_detect_headless_reason({}, "linux"), "headless")
        self.assertIsNone(_detect_headless_reason({"DISPLAY": ":0"}, "linux"))
        self.assertIsNone(_detect_headless_reason({"WAYLAND_DISPLAY": "wayland-0"}, "linux"))
        self.assertIsNone(_detect_headless_reason({}, "darwin"))
        self.assertEqual(
            _ssh_forward_hint("0.0.0.0", 8080), "ssh -L 8080:127.0.0.1:8080 <remote-host>"
        )
        self.assertEqual(_ssh_forward_hint("::", 8080), "ssh -L 8080:[::1]:8080 <remote-host>")

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

    def test_cli_rejects_directories_even_with_four_char_name(self) -> None:
        runner = CliRunner()
        with tempfile.TemporaryDirectory() as tmpdir:
            dir_path = Path(tmpdir) / "1abc"
            dir_path.mkdir()
            result = runner.invoke(app, [str(dir_path)])

        self.assertEqual(result.exit_code, 1)
        self.assertIn("Path is a directory", result.output)

    def test_cli_rejects_non_digit_four_letter_words_as_file_not_found(self) -> None:
        runner = CliRunner()
        result = runner.invoke(app, ["test"])
        self.assertEqual(result.exit_code, 1)
        self.assertIn("File not found", result.output)

    def test_cli_rejects_keep_alive_with_idle_timeout(self) -> None:
        runner = CliRunner()
        result = runner.invoke(app, ["1crn", "--keep-alive", "--idle-timeout", "5"])
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn(
            "--idle-timeout cannot be combined with --keep-alive", _plain_output(result.output)
        )

    def test_cli_escapes_rich_markup_in_error_messages(self) -> None:
        runner = CliRunner()
        result = runner.invoke(app, ["missing[/red].pdb"])
        self.assertEqual(result.exit_code, 1)
        self.assertIn("missing[/red].pdb", _plain_output(result.output))

    def test_display_source_label_matches_resolved_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            local_file = Path(tmpdir) / "1crn"
            local_file.write_text("data_local\n#\n", encoding="utf-8")
            local_source = _resolve_structure_source(
                str(local_file), fetch_timeout=5.0, format_override="cif"
            )
            assert local_source is not None
            self.assertNotEqual(_display_source_label(local_source), "PDB ID 1CRN")
            self.assertIn("1crn", _display_source_label(local_source))

        pdb_source = _resolve_structure_source("1crn", fetch_timeout=5.0)
        assert pdb_source is not None
        self.assertEqual(_display_source_label(pdb_source), "PDB ID 1CRN")

        grid = _build_info_grid(
            source=pdb_source,
            format_key="cif",
            host="0.0.0.0",
            local_url="http://127.0.0.1:8080",
            share_url="https://mol.example.com",
        )
        plain = grid.plain
        self.assertIn("PDB ID 1CRN", plain)
        self.assertIn("Local URL", plain)
        self.assertIn("Share URL", plain)

    def test_local_access_host_maps_wildcard_addresses(self) -> None:
        self.assertEqual(local_access_host("0.0.0.0"), "127.0.0.1")
        self.assertEqual(local_access_host(""), "127.0.0.1")
        self.assertEqual(local_access_host("::"), "::1")
        self.assertEqual(local_access_host("[::]"), "::1")
        self.assertEqual(local_access_host("100.64.0.1"), "100.64.0.1")


class MimeTests(unittest.TestCase):
    def test_explicit_format_supplies_mime_for_extensionless_name(self) -> None:
        self.assertEqual(_guess_mime("download", format_key="cif"), "chemical/x-cif")

    def test_unknown_extensionless_name_uses_binary_fallback(self) -> None:
        self.assertEqual(_guess_mime("download"), "application/octet-stream")

    def test_additional_format_mimes(self) -> None:
        self.assertEqual(_guess_mime("ligand.sdf"), "chemical/x-mdl-sdfile")
        self.assertEqual(_guess_mime("ligand.sdf.gz"), "chemical/x-mdl-sdfile")
        self.assertEqual(_guess_mime("small.mol2"), "chemical/x-mol2")
        self.assertEqual(_guess_mime("cluster.xyz"), "chemical/x-xyz")
        self.assertEqual(_guess_mime("frame.gro"), "text/plain; charset=utf-8")
        self.assertEqual(_guess_mime("docked.pdbqt"), "chemical/x-pdb")

    def test_accepts_gzip_parses_quality_values(self) -> None:
        self.assertTrue(_accepts_gzip("gzip, deflate, br"))
        self.assertTrue(_accepts_gzip("br;q=1.0, gzip;q=0.5"))
        self.assertFalse(_accepts_gzip("gzip;q=0"))
        self.assertFalse(_accepts_gzip("deflate, br"))
        self.assertFalse(_accepts_gzip(None))


class GzipServerTests(unittest.TestCase):
    def test_server_sets_gzip_content_encoding_for_compressed_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            structure_path = Path(tmpdir) / "example.pdb.gz"
            raw_structure = b"HEADER gzip test\nEND\n"
            structure_path.write_bytes(gzip.compress(raw_structure))
            shutdown_event = threading.Event()
            server = make_server(
                StructureSource(
                    display_name=structure_path.name, format_key="pdb", local_path=structure_path
                ),
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
    def test_local_proxy_serves_and_caches_remote_structure(self) -> None:
        remote_body = b"HEADER remote test\nEND\n"
        request_count = 0

        class RemoteHandler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: object) -> None:  # noqa: A002
                pass

            def do_GET(self) -> None:  # noqa: N802
                nonlocal request_count
                request_count += 1
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
            url = f"http://127.0.0.1:{proxy.server_address[1]}/structure"
            for _ in range(2):
                with urllib.request.urlopen(url) as response:
                    self.assertEqual(response.status, 200)
                    self.assertEqual(response.headers.get("Content-Type"), "chemical/x-pdb")
                    self.assertEqual(response.read(), remote_body)
            self.assertEqual(request_count, 1)

            with urllib.request.urlopen(f"{url}?refresh=1") as response:
                self.assertEqual(response.status, 200)
                self.assertEqual(response.read(), remote_body)
            self.assertEqual(request_count, 2)
        finally:
            shutdown_event.set()
            proxy.shutdown()
            proxy.server_close()
            proxy_thread.join(timeout=1.0)

            upstream.shutdown()
            upstream.server_close()
            upstream_thread.join(timeout=1.0)

    def test_local_proxy_propagates_upstream_errors_and_timeouts(self) -> None:
        proxy = make_server(
            StructureSource(
                display_name="example.pdb",
                format_key="pdb",
                remote_url="https://example.invalid/example.pdb",
                fetch_timeout=1.0,
            ),
            0,
        )
        proxy_thread = serve_background(proxy)
        url = f"http://127.0.0.1:{proxy.server_address[1]}/structure"

        try:
            with (
                mock.patch(
                    "molbo.server.urlopen",
                    side_effect=urllib.error.URLError(TimeoutError("timed out")),
                ),
                mock.patch("molbo.server.time.sleep"),
                self.assertRaises(urllib.error.HTTPError) as ctx,
            ):
                urllib.request.urlopen(url)
            self.assertEqual(ctx.exception.code, 504)
            ctx.exception.close()

            with (
                mock.patch(
                    "molbo.server.urlopen",
                    side_effect=urllib.error.URLError(OSError("connection refused")),
                ),
                mock.patch("molbo.server.time.sleep"),
                self.assertRaises(urllib.error.HTTPError) as ctx,
            ):
                urllib.request.urlopen(url)
            self.assertEqual(ctx.exception.code, 502)
            ctx.exception.close()

            with (
                mock.patch(
                    "molbo.server.urlopen",
                    side_effect=urllib.error.HTTPError(
                        "https://example.invalid/example.pdb", 404, "Not Found", Message(), None
                    ),
                ),
                self.assertRaises(urllib.error.HTTPError) as ctx,
            ):
                urllib.request.urlopen(url)
            self.assertEqual(ctx.exception.code, 404)
            ctx.exception.close()
        finally:
            proxy.shutdown()
            proxy.server_close()
            proxy_thread.join(timeout=1.0)

    def test_local_proxy_resolves_alphafold_api_entries(self) -> None:
        api_payload = json.dumps(
            [
                {
                    "entryId": "AF-P12345-F1",
                    "cifUrl": "https://alphafold.ebi.ac.uk/files/AF-P12345-F1-model_v4.cif",
                },
                {
                    "entryId": "AF-P12345-F2",
                    "cifUrl": "https://alphafold.ebi.ac.uk/files/AF-P12345-F2-model_v4.cif",
                },
            ]
        ).encode("utf-8")
        structure_payload = b"data_AF_P12345_F2\n#\n"
        seen_urls: list[str] = []

        class FakeResponse(io.BytesIO):
            def __init__(self, data: bytes, content_type: str) -> None:
                super().__init__(data)
                self.status = 200
                self.headers = {
                    "Content-Type": content_type,
                    "Content-Length": str(len(data)),
                }

        def fake_urlopen(request: urllib.request.Request, timeout: float = 30.0) -> FakeResponse:  # noqa: ARG001
            seen_urls.append(request.full_url)
            if "api/prediction" in request.full_url:
                return FakeResponse(api_payload, "application/json")
            return FakeResponse(structure_payload, "chemical/x-cif")

        source = _resolve_structure_source("AF-P12345-F2", fetch_timeout=5.0)
        assert source is not None
        proxy = make_server(source, 0)
        proxy_thread = serve_background(proxy)

        try:
            with (
                mock.patch("molbo.server.urlopen", side_effect=fake_urlopen),
                urllib.request.urlopen(
                    f"http://127.0.0.1:{proxy.server_address[1]}/structure"
                ) as response,
            ):
                self.assertEqual(response.status, 200)
                self.assertEqual(response.read(), structure_payload)
            self.assertEqual(
                seen_urls,
                [
                    "https://alphafold.ebi.ac.uk/api/prediction/P12345",
                    "https://alphafold.ebi.ac.uk/files/AF-P12345-F2-model_v4.cif",
                ],
            )
        finally:
            proxy.shutdown()
            proxy.server_close()
            proxy_thread.join(timeout=1.0)


class PrefixedServerTests(unittest.TestCase):
    def test_server_serves_session_under_path_prefix(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            structure_path = Path(tmpdir) / "example.pdb"
            structure_bytes = b"HEADER prefixed test\nEND\n"
            structure_path.write_bytes(structure_bytes)
            shutdown_event = threading.Event()
            server = make_server(
                StructureSource(
                    display_name=structure_path.name, format_key="pdb", local_path=structure_path
                ),
                0,
                shutdown_event=shutdown_event,
                share_url="https://mol.example.com/s/demo/",
                base_path="/s/demo",
            )
            thread = serve_background(server)

            try:
                port = server.server_address[1]

                with urllib.request.urlopen(f"http://127.0.0.1:{port}/s/demo/") as response:
                    self.assertEqual(response.status, 200)
                    html = response.read().decode("utf-8")
                self.assertIn('var FILE_URL = "/s/demo/structure";', html)
                self.assertIn('src="/s/demo/assets/molstar-4.5.0.js"', html)
                self.assertIn('var HEARTBEAT_URL = "/s/demo/heartbeat";', html)

                with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/s/demo/assets/molstar-4.5.0.css"
                ) as response:
                    self.assertEqual(response.status, 200)
                    self.assertIn("text/css", response.headers.get("Content-Type", ""))

                with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/s/demo/structure"
                ) as response:
                    self.assertEqual(response.status, 200)
                    self.assertEqual(response.read(), structure_bytes)

                with self.assertRaises(urllib.error.HTTPError) as ctx:
                    urllib.request.urlopen(f"http://127.0.0.1:{port}/structure")
                self.assertEqual(ctx.exception.code, 404)
                ctx.exception.close()

                conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                try:
                    conn.request("GET", "/s/demo")
                    response = conn.getresponse()
                    self.assertEqual(response.status, 308)
                    self.assertEqual(response.getheader("Location"), "/s/demo/")
                    response.read()
                finally:
                    conn.close()
            finally:
                shutdown_event.set()
                server.shutdown()
                server.server_close()
                thread.join(timeout=1.0)


if __name__ == "__main__":
    unittest.main()
