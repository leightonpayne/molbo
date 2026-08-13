"""molbo — Inspect PDB/CIF structures from the command line."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("molbo")
except PackageNotFoundError:  # pragma: no cover - only possible from an uninstalled source tree
    __version__ = "0+unknown"
