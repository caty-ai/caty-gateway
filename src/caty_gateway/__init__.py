"""Caty gateway package."""

from importlib.metadata import PackageNotFoundError, version as _dist_version

try:
    __version__ = _dist_version("caty-gateway")
except PackageNotFoundError:  # source checkout without an installed dist
    __version__ = "0+unknown"
