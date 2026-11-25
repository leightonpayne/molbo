---
icon: lucide/package
---

# Installation

## Run without installing

Try `molbo` directly with `uvx`:

```bash
uvx molbo 1crn
```

## Install as a CLI tool

For regular use, install `molbo` globally with `uv` (or `pipx` / `pip`):

```bash
uv tool install molbo
```

```bash
pipx install molbo
```

```bash
pip install molbo
```

## Development setup

If you are working from a clone of the repository:

```bash
git clone https://github.com/leightonpayne/molbo.git
cd molbo
uv sync
```

Run the CLI from the project environment with:

```bash
uv run molbo
```
