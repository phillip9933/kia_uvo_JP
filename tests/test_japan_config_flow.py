"""Japan is Hyundai-only and uses the shared credential flow."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.const import CONF_REGION

from custom_components.kia_uvo.config_flow import ConfigFlow
from custom_components.kia_uvo.const import CONF_BRAND, REGIONS


@pytest.mark.parametrize("brand", ["1", "3"])
async def test_japan_rejects_unsupported_brand_before_login(brand: str) -> None:
    flow = ConfigFlow()
    flow.async_show_form = MagicMock(return_value={"type": "form"})
    flow.async_step_credentials_password = AsyncMock()
    await flow.async_step_user({CONF_REGION: "10", CONF_BRAND: brand})
    assert flow.async_show_form.call_args.kwargs["errors"] == {
        CONF_BRAND: "unsupported_japan_brand"
    }
    flow.async_step_credentials_password.assert_not_awaited()


async def test_japan_hyundai_enters_shared_password_flow() -> None:
    flow = ConfigFlow()
    flow.async_step_credentials_password = AsyncMock()
    await flow.async_step_user({CONF_REGION: "10", CONF_BRAND: "2"})
    assert flow._region_data == {CONF_REGION: 10, CONF_BRAND: 2}
    flow.async_step_credentials_password.assert_awaited_once_with()
    assert REGIONS[10] == "Japan"


async def test_existing_region_keeps_all_brand_choices() -> None:
    flow = ConfigFlow()
    flow.async_step_credentials_password = AsyncMock()
    await flow.async_step_user({CONF_REGION: "1", CONF_BRAND: "1"})
    flow.async_step_credentials_password.assert_awaited_once_with()


async def test_japan_login_builds_manager_and_creates_entry(monkeypatch) -> None:
    from custom_components.kia_uvo import config_flow

    flow = ConfigFlow()
    flow._region_data = {CONF_REGION: 10, CONF_BRAND: 2}
    flow.hass = MagicMock()
    flow.hass.config.language = "ja"
    flow.hass.async_add_executor_job = AsyncMock(return_value=config_flow.Token())
    flow.async_set_unique_id = AsyncMock()
    flow._abort_if_unique_id_configured = MagicMock()
    flow.async_create_entry = MagicMock(return_value={"type": "create_entry"})
    constructor = MagicMock()
    monkeypatch.setattr(config_flow, "VehicleManager", constructor)
    result = await flow.async_step_credentials_password(
        {"username": "synthetic", "password": "synthetic", "pin": ""}
    )
    assert result["type"] == "create_entry"
    assert constructor.call_args.kwargs["region"] == 10
    assert constructor.call_args.kwargs["brand"] == 2
    assert (
        flow.async_create_entry.call_args.kwargs["title"] == "Hyundai Japan synthetic"
    )


async def test_japan_invalid_auth_stays_on_credentials_form(monkeypatch) -> None:
    from custom_components.kia_uvo import config_flow

    flow = ConfigFlow()
    flow._region_data = {CONF_REGION: 10, CONF_BRAND: 2}
    flow.hass = MagicMock()
    flow.hass.async_add_executor_job = AsyncMock(
        side_effect=config_flow.AuthenticationError("synthetic failure")
    )
    flow.async_show_form = MagicMock(return_value={"type": "form"})
    monkeypatch.setattr(config_flow, "VehicleManager", MagicMock())
    await flow.async_step_credentials_password(
        {"username": "synthetic", "password": "synthetic", "pin": ""}
    )
    assert flow.async_show_form.call_args.kwargs["errors"] == {"base": "invalid_auth"}
