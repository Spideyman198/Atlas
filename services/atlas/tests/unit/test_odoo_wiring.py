"""The composition root's treatment of the Odoo gateway."""

from __future__ import annotations

import pytest
from pydantic import SecretStr

from atlas.config.container import _build_odoo_gateway
from atlas.config.settings import DatabaseSettings, OdooSettings, Settings
from atlas.domain.errors import ConfigurationError
from atlas.domain.ports.odoo_gateway import OdooGateway

pytestmark = pytest.mark.unit

_URL = "postgresql://atlas:atlas@localhost:5432/atlas"


def settings_with(odoo: OdooSettings) -> Settings:
    return Settings(database=DatabaseSettings(url=_URL), odoo=odoo)


async def test_the_gateway_is_built_from_settings() -> None:
    gateway = _build_odoo_gateway(
        settings_with(
            OdooSettings(
                base_url="http://odoo:8069",
                database="odoo",
                service_token=SecretStr("a-token"),
            )
        )
    )

    try:
        assert isinstance(gateway, OdooGateway)
    finally:
        await gateway.aclose()


@pytest.mark.parametrize("token", [None, SecretStr("")])
def test_a_missing_service_token_stops_the_engine_at_boot(token: SecretStr | None) -> None:
    """Not a degraded mode: Odoo would refuse every call.

    The engine would retrieve candidates and clear none of them, which looks
    like an assistant that knows nothing rather than like a missing environment
    variable. Failing here says which one.
    """
    with pytest.raises(ConfigurationError, match="ATLAS_ODOO__SERVICE_TOKEN"):
        _build_odoo_gateway(settings_with(OdooSettings(service_token=token)))


def test_the_engine_is_never_given_the_context_signing_key() -> None:
    """The property ADR-0006 rests on, asserted against the settings surface.

    The engine holds the shared service token and nothing else. If it could also
    sign context tokens it could name any user it liked, and Odoo would believe
    it — which would make the whole authorization story decorative.
    """
    fields = set(OdooSettings.model_fields)

    assert "service_token" in fields
    assert not {name for name in fields if "secret" in name or "signing" in name}
