"""Odoo Atlas engine.

Retrieval, orchestration and generation for the Odoo Atlas copilot. Runs as a
sidecar service beside Odoo and never imports ``odoo`` — see
``docs/adr/0002-sidecar-service-topology.md``.
"""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("atlas")
except PackageNotFoundError:  # pragma: no cover - source checkout without an install
    __version__ = "0.0.0+unknown"

__all__ = ["__version__"]
