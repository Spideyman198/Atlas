"""Adapters for the Odoo gateway port.

The engine reaches Odoo over HTTP and never imports it — a rule enforced by an
``import-linter`` contract, not by review
(:doc:`ADR-0002 </adr/0002-sidecar-service-topology>`).
"""

from atlas.infrastructure.odoo.fakes import FakeOdooGateway, FakeSourceReader
from atlas.infrastructure.odoo.http_gateway import OdooHttpGateway
from atlas.infrastructure.odoo.source_reader import OdooHttpSourceReader

__all__ = [
    "FakeOdooGateway",
    "FakeSourceReader",
    "OdooHttpGateway",
    "OdooHttpSourceReader",
]
