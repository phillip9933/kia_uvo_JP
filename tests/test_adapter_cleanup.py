"""Adapter session cleanup is optional and independent of regional support."""

import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from custom_components.kia_uvo import async_unload_entry
from custom_components.kia_uvo.const import DOMAIN


def unload_fixture(api, *, platform_success=True):
    coordinator = SimpleNamespace(vehicle_manager=SimpleNamespace(api=api))
    hass = SimpleNamespace(
        data={DOMAIN: {"synthetic-entry": coordinator}},
        config_entries=SimpleNamespace(
            async_unload_platforms=AsyncMock(return_value=platform_success)
        ),
        async_add_executor_job=AsyncMock(),
    )
    return hass, SimpleNamespace(unique_id="synthetic-entry")


async def test_optional_close_executes_off_event_loop(monkeypatch):
    loop_thread = threading.get_ident()
    close_threads = []
    api = SimpleNamespace(close=lambda: close_threads.append(threading.get_ident()))
    hass, entry = unload_fixture(api)

    async def execute(target):
        return await asyncio.get_running_loop().run_in_executor(None, target)

    hass.async_add_executor_job.side_effect = execute
    unload_services = Mock()
    monkeypatch.setattr(
        "custom_components.kia_uvo.async_unload_services", unload_services
    )
    assert await async_unload_entry(hass, entry)
    assert close_threads and close_threads[0] != loop_thread
    assert hass.data[DOMAIN] == {}
    unload_services.assert_called_once_with(hass)


async def test_adapter_without_close_retains_legacy_unload(monkeypatch):
    hass, entry = unload_fixture(SimpleNamespace())
    monkeypatch.setattr("custom_components.kia_uvo.async_unload_services", Mock())
    assert await async_unload_entry(hass, entry)
    hass.async_add_executor_job.assert_not_awaited()
    assert hass.data[DOMAIN] == {}


async def test_failed_platform_unload_does_not_close_or_remove_adapter():
    close = Mock()
    hass, entry = unload_fixture(SimpleNamespace(close=close), platform_success=False)
    assert not await async_unload_entry(hass, entry)
    hass.async_add_executor_job.assert_not_awaited()
    assert entry.unique_id in hass.data[DOMAIN]


async def test_cleanup_failure_does_not_block_unload_or_log_private_error(
    monkeypatch, caplog
):
    hass, entry = unload_fixture(SimpleNamespace(close=Mock()))
    hass.async_add_executor_job.side_effect = RuntimeError("SYNTHETIC_PRIVATE_VALUE")
    monkeypatch.setattr("custom_components.kia_uvo.async_unload_services", Mock())
    assert await async_unload_entry(hass, entry)
    assert hass.data[DOMAIN] == {}
    assert "Unable to close region API session" in caplog.text
    assert "SYNTHETIC_PRIVATE_VALUE" not in caplog.text


async def test_unloading_one_account_keeps_other_adapter_and_services(monkeypatch):
    close = Mock()
    other_close = Mock()
    hass, entry = unload_fixture(SimpleNamespace(close=close))
    hass.data[DOMAIN]["other-entry"] = SimpleNamespace(
        vehicle_manager=SimpleNamespace(api=SimpleNamespace(close=other_close))
    )
    unload_services = Mock()
    monkeypatch.setattr(
        "custom_components.kia_uvo.async_unload_services", unload_services
    )
    assert await async_unload_entry(hass, entry)
    hass.async_add_executor_job.assert_awaited_once_with(close)
    assert list(hass.data[DOMAIN]) == ["other-entry"]
    other_close.assert_not_called()
    unload_services.assert_not_called()
