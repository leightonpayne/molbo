---
icon: lucide/circle-power
---

# Quickstart

`molbo` opens a molecular structure in a local Mol* session and serves it from a small HTTP server.

## Basic examples

Open by PDB ID:

```bash
molbo 9QXS
```

Open a local file:

```bash
molbo structure.pdb
```

Open a remote structure URL:

```bash
molbo https://alphafold.ebi.ac.uk/files/AF-0000000066236528-model_v1.bcif
```

Bind to a specific port:

```bash
molbo 9QXS --port 8080
```

Do not open the browser automatically:

```bash
molbo 9QXS --no-open
```

Auto-close when the browser tab goes away:

```bash
molbo 9QXS --auto-close
```

Watch a local file and live-reload the viewer on save:

```bash
molbo output.pdb --watch
```

## CLI options

| Option | Default | Description |
| --- | --- | --- |
| `--port`, `-p` | auto | Port to bind the local HTTP server (`0` chooses an ephemeral port). |
| `--host` | `127.0.0.1` | Interface or IP address (IPv4 or IPv6) to bind to. |
| `--no-open` | off | Start the server without auto-opening a browser tab (also skipped automatically in SSH/headless sessions). |
| `--watch`, `-w` | off | Watch a local structure file for changes and live-reload the viewer while keeping the camera angle. |
| `--auto-close` / `--keep-alive` | `--keep-alive` | Shut down automatically after the last browser tab closes or goes idle. |
| `--idle-timeout` | `15` seconds | Seconds without browser activity before auto-close (implies `--auto-close`). |
| `--fetch-timeout` | `30` seconds | Timeout for downloading remote structures. |
| `--format` | inferred | Explicit format (`pdb`, `cif`, `mmcif`, `bcif`, `pdbqt`, `sdf`, `mol`, `mol2`, `xyz`, `gro`) for extensionless inputs or database IDs. |
| `--base-url` | none | Public viewer URL shown in the banner and QR modal; any path prefix is also used for routing. |
| `--version`, `-v` | — | Print the installed `molbo` version and exit. |
