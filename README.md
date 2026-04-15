# Molbo

[![Support](https://img.shields.io/pypi/status/molbo?label=support&color=f3c539)](https://pypi.org/project/molbo/) [![PyPI](https://img.shields.io/pypi/v/molbo?label=pypi&color=107cb8)](https://pypi.org/project/molbo/)

<p align="center">
  <img src="docs/static/molbo-logo.png" width="50%" alt="poorly drawn molbo logo">
</p>

## Overview

`molbo` is a command-line launcher for interactive molecular structure viewing in [Mol*](https://molstar.org/). It accepts local structure files, remote structure URLs, 4-character PDB IDs, and starts a small HTTP server that serves a self-contained Mol* viewer from vendored frontend assets.

## Installation

```bash
uv tool install molbo
```

## Usage

Opening a structure is as easy as:

```bash
molbo structure.pdb
```

Read [the documentation](https://leightonpayne.github.io/molbo/) for more information on what `molbo` can do.

## License

`molbo` is released under the [MIT license](https://choosealicense.com/licenses/mit/). The package also vendors frontend assets available under the [MIT license](https://choosealicense.com/licenses/mit/). See `src/molbo/vendor/LICENSE-molstar` and `src/molbo/vendor/LICENSE-qrcode-generator` for more information.
