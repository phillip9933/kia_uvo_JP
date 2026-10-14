"""Unload releases optional region API network sessions outside the event loop."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from custom_components.kia_uvo import async_unload_entry
from custom_components.kia_uvo.const import DOMAIN


async def test_unload_closes_region_api_via_executor(monkeypatch):
    close = Mock()
    hass = SimpleNamespace(
        data={
            DOMAIN: {
                "synthetic": SimpleNamespace(
                    vehicle_manager=SimpleNamespace(api=SimpleNamespace(close=close))
                )
            }
        },
        config_entries=SimpleNamespace(
            async_unload_platforms=AsyncMock(return_value=True)
        ),
        async_add_executor_job=AsyncMock(),
    )
    monkeypatch.setattr("custom_components.kia_uvo.async_unload_services", Mock())
    assert await async_unload_entry(hass, SimpleNamespace(unique_id="synthetic"))
    hass.async_add_executor_job.assert_awaited_once_with(close)
    assert hass.data[DOMAIN] == {}


async def test_old_region_without_close_unloads_unchanged(monkeypatch):
    hass = SimpleNamespace(
        data={
            DOMAIN: {
                "synthetic": SimpleNamespace(
                    vehicle_manager=SimpleNamespace(api=SimpleNamespace())
                )
            }
        },
        config_entries=SimpleNamespace(
            async_unload_platforms=AsyncMock(return_value=True)
        ),
        async_add_executor_job=AsyncMock(),
    )
    monkeypatch.setattr("custom_components.kia_uvo.async_unload_services", Mock())
    assert await async_unload_entry(hass, SimpleNamespace(unique_id="synthetic"))
    hass.async_add_executor_job.assert_not_awaited()


async def test_failed_platform_unload_keeps_api_session_open():
    close = Mock()
    hass = SimpleNamespace(
        data={
            DOMAIN: {
                "synthetic": SimpleNamespace(
                    vehicle_manager=SimpleNamespace(api=SimpleNamespace(close=close))
                )
            }
        },
        config_entries=SimpleNamespace(
            async_unload_platforms=AsyncMock(return_value=False)
        ),
        async_add_executor_job=AsyncMock(),
    )
    assert not await async_unload_entry(hass, SimpleNamespace(unique_id="synthetic"))
    hass.async_add_executor_job.assert_not_awaited()
    assert "synthetic" in hass.data[DOMAIN]
