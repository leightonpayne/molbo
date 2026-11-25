---
name: molbo
description: Launch an interactive Mol* 3D molecular structure viewer from the command line or Python for local files, stdin, PDB IDs, AlphaFold DB IDs, or remote URLs. Use when inspecting folding, docking, MD, or structure-prep outputs, live-reloading iterative designs, sharing structures with a colleague, or visually verifying 3D geometry in a browser.
---

# Using `molbo`

`molbo` serves a self-contained, offline Mol* 3D viewer over a lightweight local HTTP server.

## Run without installing

Prefer `molbo` if already on `PATH`, otherwise invoke with `uvx molbo ...`.

## Recommended patterns for AI agents

### 1. Hand a user an instant interactive view (non-blocking)

Use `--detach --json` so the command exits immediately once the server is listening and returns structured JSON (`url`, `share_url`, `pid`):

```bash
molbo top_hit.cif --detach --auto-close --json
```

- Add `--no-open` if you want to return the URL in chat or open it in a browser automation tool instead of the system browser.
- Stop a detached session later with `kill <pid>` (handled cleanly via `SIGTERM`).

### 2. Live-reload while iterating on a structure

When modifying a local structure file across multiple steps (docking, repacking, chain cleanup, MD frame export), launch once with `--watch`:

```bash
molbo candidate.cif --watch --detach --json
```

Every subsequent write to `candidate.cif` reloads automatically in the user's browser while preserving the current camera angle.

### 3. Inspect structures from stdin without temp files

```bash
cat output.pdb | molbo - --format pdb --detach --json
```

`--format` is required for `-`; gzipped stdin bytes are auto-detected.

### 4. Open database entries or remote URLs directly

- 4-char PDB ID: `molbo 1crn` (optional `--format cif|mmcif|pdb|bcif`)
- Extended 12-char wwPDB ID: `molbo pdb_00001crn`
- AlphaFold DB: `molbo af:P69905` or `molbo AF-P69905-F1 --format bcif`
- Remote URL: `molbo 'https://example.org/file/content' --format cif`

Supported local/stdin formats (plus `.gz`): `pdb`, `ent`, `cif`, `mmcif`, `bcif`, `pdbqt`, `sdf`, `sd`, `mol`, `mol2`, `xyz`, `gro`.

### 5. Share across LAN, Tailscale, or SSH

- LAN / Wi-Fi: `molbo structure.cif --host 0.0.0.0 --qr --detach --json` (auto-detects LAN IP for `share_url`)
- Private unguessable link behind a tunnel/Funnel: `molbo secret.cif --port 8080 --no-open --private --base-url https://host.ts.net --qr`
- Over SSH: `molbo` automatically skips browser launch and prints an `ssh -L` port-forwarding command.

### 6. Browser automation & screenshots

When opening a `molbo` URL in a headless/preview browser to take screenshots or inspect a pose:

1. Check `GET <url>/info` (or `/health`) if you want JSON metadata (`name`, `format`, `source_kind`, `watch`).
2. Wait for Mol* to finish rendering with `await window.molbo.ready` instead of fixed sleeps.
3. Capture or share the exact camera angle with `window.molbo.getShareUrl(true)` or restore a saved angle with `window.molbo.setCameraHash("#cam=...")`.

### 7. Python API

```python
import molbo

with molbo.serve("structure.cif", watch=True) as session:
    print(session.url)
```
