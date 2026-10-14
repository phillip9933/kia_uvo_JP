"""A Japan MQTT ACK without completion must never report HA action success."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.exceptions import HomeAssistantError
from hyundai_kia_connect_api.const import ORDER_STATUS

from custom_components.kia_uvo.coordinator import HyundaiKiaConnectDataUpdateCoordinator


def coordinator(strict=True):
    result = HyundaiKiaConnectDataUpdateCoordinator.__new__(
        HyundaiKiaConnectDataUpdateCoordinator
    )
    result.vehicle_manager = MagicMock()
    result.vehicle_manager.api = SimpleNamespace(
        supports_remote_commands=True,
        supports_force_refresh=True,
        requires_command_confirmation=strict,
    )
    result.hass = MagicMock()
    result.hass.async_add_executor_job = AsyncMock()
    result.async_check_and_refresh_token = AsyncMock()
    result.async_refresh = AsyncMock()
    result.async_set_updated_data = MagicMock()
    result._action_lock = asyncio.Lock()
    result.data = {}
    return result


@pytest.mark.parametrize(
    "status",
    [
        ORDER_STATUS.FAILED,
        ORDER_STATUS.TIMEOUT,
        ORDER_STATUS.UNKNOWN,
        ORDER_STATUS.PENDING,
        None,
    ],
)
async def test_unconfirmed_command_raises_after_refresh(monkeypatch, status):
    monkeypatch.setattr(
        "custom_components.kia_uvo.coordinator.asyncio.sleep", AsyncMock()
    )
    result = coordinator()
    result.hass.async_add_executor_job.side_effect = ["SYNTHETIC_ID", status, None]
    with pytest.raises(HomeAssistantError, match="not confirmed"):
        await result.async_lock_vehicle("CAR")
    result.async_refresh.assert_not_awaited()
    result.hass.async_add_executor_job.assert_awaited_with(
        result.vehicle_manager.update_all_vehicles_with_cached_state
    )


async def test_confirmed_command_completes(monkeypatch):
    monkeypatch.setattr(
        "custom_components.kia_uvo.coordinator.asyncio.sleep", AsyncMock()
    )
    result = coordinator()
    result.hass.async_add_executor_job.side_effect = [
        "SYNTHETIC_ID",
        ORDER_STATUS.SUCCESS,
        None,
    ]
    await result.async_lock_vehicle("CAR")
    result.async_refresh.assert_not_awaited()
    result.hass.async_add_executor_job.assert_awaited_with(
        result.vehicle_manager.update_all_vehicles_with_cached_state
    )


async def test_older_region_preserves_unknown_confirmation_behavior(monkeypatch):
    monkeypatch.setattr(
        "custom_components.kia_uvo.coordinator.asyncio.sleep", AsyncMock()
    )
    result = coordinator(False)
    result.hass.async_add_executor_job.side_effect = [
        "SYNTHETIC_ID",
        ORDER_STATUS.UNKNOWN,
    ]
    await result.async_lock_vehicle("CAR")
    result.async_refresh.assert_awaited_once()


async def test_forced_refresh_wait_also_checks_confirmation(monkeypatch):
    monkeypatch.setattr(
        "custom_components.kia_uvo.coordinator.asyncio.sleep", AsyncMock()
    )
    result = coordinator()
    result.hass.async_add_executor_job.side_effect = [ORDER_STATUS.FAILED, None]
    with pytest.raises(HomeAssistantError, match="not confirmed"):
        await result.async_await_action_and_force_refresh("CAR", "SYNTHETIC_ID")
    result.async_set_updated_data.assert_called_once()
