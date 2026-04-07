# molbo

A little tool for quickly opening PDB/CIF molecular structures in [Mol\*](https://molstar.org/) from the terminal.

`molbo` starts a local HTTP server, serves an HTML page that loads the Mol\* Viewer from CDN, the viewer fetches the structure file from the same local server, and the browser renders the visualization. Press **Ctrl+C** in the terminal or close the browser window to shut down the server.

## Installation

```bash
pip install -e .
```

## Usage

```bash
molbo structure.pdb

# Open on a specific port
molbo model.cif --port 8080

# Don't auto-open the browser
molbo structure.pdb --no-open
```

## License

MIT
