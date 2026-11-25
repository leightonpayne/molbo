"""molbo — Inspect PDB/CIF structures from the command line."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("molbo")
except PackageNotFoundError:  # pragma: no cover
    __version__ = "0+unknown"
