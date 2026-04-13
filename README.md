# molbo

A little tool for quickly opening PDB/CIF molecular structures in [Mol\*](https://molstar.org/) from the terminal.

`molbo` starts a local HTTP server, serves an HTML page that loads the Mol\* Viewer from CDN, the viewer fetches the structure file from the same local server, and the browser renders the visualization. Inputs can be local files or remote `http(s)` URLs. By default the server stays alive until you press **Ctrl+C**. If you want the old "close the tab and exit" behavior, enable `--auto-close`.

## Installation

```bash
pip install -e .
```

## Usage

```bash
molbo structure.pdb

# Gzipped inputs work too
molbo structure.pdb.gz

# Remote URLs work too
molbo https://files.rcsb.org/download/1CRN.pdb

# Open on a specific port
molbo model.cif --port 8080

# Don't auto-open the browser
molbo structure.pdb --no-open

# Use the plain Mol* look instead of the stylized publication preset
molbo structure.pdb --style default

# Shut down automatically when the viewer tab closes
molbo structure.pdb --auto-close

# Auto-close after 30 seconds without browser heartbeats
molbo structure.pdb --idle-timeout 30
```

## Notes

- The viewer assets are loaded from CDN, so the browser needs network access to fetch Mol*.
- Supported inputs: local or remote `.pdb`, `.cif`, `.mmcif`, `.bcif`, and their `.gz` variants.
- Available styles: `publication` and `default`.
- `--idle-timeout` implies `--auto-close`.

## License

MIT
