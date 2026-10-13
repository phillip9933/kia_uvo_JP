"""Cached-only regions must not expose or send vehicle commands."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from conftest import make_coordinator, make_hass
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers.update_coordinator import UpdateFailed
from hyundai_kia_connect_api import Vehicle
from hyundai_kia_connect_api.exceptions import AuthenticationError

from custom_components.kia_uvo import button, climate, cover, lock, number, switch, time
from custom_components.kia_uvo.coordinator import HyundaiKiaConnectDataUpdateCoordinator


def _coordinator(*, remote: bool = False, force: bool = False):
    coordinator = HyundaiKiaConnectDataUpdateCoordinator.__new__(
        HyundaiKiaConnectDataUpdateCoordinator
    )
    coordinator.vehicle_manager = MagicMock()
    coordinator.vehicle_manager.api = SimpleNamespace(
        supports_remote_commands=remote, supports_force_refresh=force
    )
    coordinator.hass = MagicMock()
    coordinator.hass.async_add_executor_job = AsyncMock()
    coordinator.async_check_and_refresh_token = AsyncMock()
    coordinator.data = {}
    coordinator.scan_interval = 900
    coordinator.force_refresh_interval = 5400
    return coordinator


async def test_poll_uses_cached_status_without_force_refresh() -> None:
    coordinator = _coordinator()
    await coordinator._async_update_data()
    coordinator.hass.async_add_executor_job.assert_awaited_once_with(
        coordinator.vehicle_manager.update_all_vehicles_with_cached_state
    )
    coordinator.vehicle_manager.check_and_force_update_vehicles.assert_not_called()


@pytest.mark.parametrize(
    "method", ["async_force_update_all", "async_force_refresh_vehicle"]
)
async def test_force_refresh_rejected_before_authentication(method: str) -> None:
    coordinator = _coordinator()
    args = ("v1",) if method.endswith("vehicle") else ()
    with pytest.raises(HomeAssistantError, match="cached vehicle status only"):
        await getattr(coordinator, method)(*args)
    coordinator.async_check_and_refresh_token.assert_not_awaited()
    coordinator.hass.async_add_executor_job.assert_not_awaited()


@pytest.mark.parametrize(
    "method",
    [
        "async_lock_vehicle",
        "async_unlock_vehicle",
        "async_start_charge",
        "async_start_hazard_lights",
        "async_request_svm_capture",
    ],
)
async def test_commands_rejected_before_any_request(method: str) -> None:
    coordinator = _coordinator()
    with pytest.raises(HomeAssistantError, match="does not support vehicle commands"):
        await getattr(coordinator, method)("v1")
    coordinator.async_check_and_refresh_token.assert_not_awaited()
    coordinator.hass.async_add_executor_job.assert_not_awaited()


async def test_navigation_and_schedule_service_paths_are_guarded() -> None:
    coordinator = _coordinator()
    with pytest.raises(HomeAssistantError, match="does not support vehicle commands"):
        await coordinator.async_set_navigation("v1", [])
    with pytest.raises(HomeAssistantError, match="does not support vehicle commands"):
        await coordinator.async_schedule_charging_and_climate("v1", MagicMock())
    coordinator.hass.async_add_executor_job.assert_not_awaited()


@pytest.mark.parametrize("platform", [lock, climate, number, time, cover, button])
async def test_cached_only_region_has_no_remote_entities(platform) -> None:
    vehicle = Vehicle(id="v1", name="test", model="test")
    vehicle.air_control_is_on = False
    coordinator = make_coordinator(vehicle)
    coordinator.supports_remote_commands = False
    coordinator.supports_force_refresh = False
    hass, entry = make_hass(coordinator)
    created = []
    await platform.async_setup_entry(hass, entry, created.extend)
    assert created == []


def test_older_api_defaults_preserve_existing_regions() -> None:
    coordinator = _coordinator()
    coordinator.vehicle_manager.api = SimpleNamespace()
    assert coordinator.supports_remote_commands
    assert coordinator.supports_force_refresh


@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        (AuthenticationError("private response"), ConfigEntryAuthFailed),
        (RuntimeError("private response"), UpdateFailed),
    ],
)
async def test_cached_failure_has_safe_message_and_no_force_fallback(failure, expected):
    coordinator = _coordinator()
    coordinator.hass.async_add_executor_job.side_effect = failure
    with pytest.raises(expected) as error:
        await coordinator._async_update_data()
    assert "private response" not in str(error.value)
    coordinator.hass.async_add_executor_job.assert_awaited_once_with(
        coordinator.vehicle_manager.update_all_vehicles_with_cached_state
    )


@pytest.mark.parametrize(("remote", "force"), [(False, True), (True, False)])
async def test_refresh_button_capability_is_independent(remote, force):
    vehicle = Vehicle(id="v1", name="test", model="test")
    coordinator = make_coordinator(vehicle)
    coordinator.supports_remote_commands = remote
    coordinator.supports_force_refresh = force
    hass, entry = make_hass(coordinator)
    created = []
    await button.async_setup_entry(hass, entry, created.extend)
    keys = {entity.entity_description.key for entity in created}
    assert ("force_refresh" in keys) is force
    assert ("start_hazard_lights" in keys) is remote


async def test_cached_only_switches_keep_local_svm_setting():
    vehicle = Vehicle(id="v1", name="test", model="test")
    vehicle.supports_svm = True
    vehicle.air_control_is_on = False
    coordinator = make_coordinator(vehicle)
    coordinator.supports_remote_commands = False
    hass, entry = make_hass(coordinator)
    created = []
    await switch.async_setup_entry(hass, entry, created.extend)
    assert len(created) == 1
    assert isinstance(created[0], switch.SVMDewarpSwitch)
