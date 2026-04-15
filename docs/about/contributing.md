---
icon: lucide/heart-handshake
---

# Contributing

Contributions to `molbo` are welcome.

## Quick Start

    git clone https://github.com/leightonpayne/molbo.git
    cd molbo
    uv sync

## Development Commands

| Command                 | Description                 |
| ----------------------- | --------------------------- |
| `uv run python -m unittest discover -s tests -v` | Run tests |
| `uv run zensical serve` | Serve docs locally |
| `uv run zensical build` | Build docs locally |
| `uv build` | Build sdist and wheel |

## Project Structure

    src/molbo/
    ├── cli.py        # Typer CLI entrypoint
    ├── server.py     # Local HTTP server
    ├── viewer.html   # Browser UI template
    └── vendor/       # Vendored frontend assets

## Making Changes

1. Make changes in `src/molbo/`
2. Run the test suite
3. If you changed docs, preview them with Zensical
4. If you changed packaged assets or templates, check `uv build`

## Submitting Changes

1. Fork the repository
2. Create a feature branch
3. Make your changes
4. Run tests and linting
5. Submit a pull request

## Release Process

Maintainers should verify both the test suite and the built wheel before publishing a release.
