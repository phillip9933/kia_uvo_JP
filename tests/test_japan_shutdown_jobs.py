"""Full-build Japan executor work participates in shared shutdown ownership."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
import pytest

from custom_components.kia_uvo.japan_coordinator import JapanDataUpdateCoordinator


@pytest.mark.parametrize("operation", ["svm", "token"])
@pytest.mark.parametrize("cancel_caller", [False, True])
async def test_unlocked_japan_read_drains_before_shared_and_adapter_cleanup(
    monkeypatch, operation, cancel_caller
):
    monkeypatch.setattr(DataUpdateCoordinator, "async_shutdown", AsyncMock())
    result = JapanDataUpdateCoordinator.__new__(JapanDataUpdateCoordinator)
    result._action_lock = asyncio.Lock()
    result._svm_details = {}
    result._svm_received_at = {}
    result._svm_views = {}
    result._async_save_token = AsyncMock()
    result._sync_vehicle_enabled_states = Mock()
    started, release = asyncio.Event(), asyncio.Event()
    events = []
    details = SimpleNamespace(captured_at=None, image_bytes=b"synthetic")
    read = Mock(return_value=details if operation == "svm" else True)
    manager_close = Mock(side_effect=lambda: events.append("manager_closed"))
    adapter_close = Mock(side_effect=lambda: events.append("adapter_closed"))
    result.vehicle_manager = SimpleNamespace(
        get_svm_details=read,
        check_and_refresh_token=read,
        close=manager_close,
        api=SimpleNamespace(close=adapter_close),
    )

    async def execute(target, *args):
        if target is read:
            events.append("read_started")
            started.set()
            await release.wait()
            events.append("read_finished")
        return target(*args)

    result.hass = SimpleNamespace(async_add_executor_job=AsyncMock(side_effect=execute))
    caller = asyncio.create_task(
        result.async_get_svm_details("synthetic")
        if operation == "svm"
        else result.async_check_and_refresh_token()
    )
    await started.wait()
    assert not result._action_lock.locked()
    assert len(result._api_jobs) == 1
    if cancel_caller:
        caller.cancel()
        await asyncio.sleep(0)
        assert not caller.done()
    shutdown = asyncio.create_task(result.async_shutdown())
    while not getattr(result, "_shutdown_started", False):
        await asyncio.sleep(0)
    manager_close.assert_not_called()
    adapter_close.assert_not_called()
    with pytest.raises(HomeAssistantError, match="shutting down"):
        await result.async_get_svm_details("synthetic")
    release.set()
    if cancel_caller:
        with pytest.raises(asyncio.CancelledError):
            await caller
    else:
        await caller
    await shutdown

    assert events == [
        "read_started",
        "read_finished",
        "manager_closed",
        "adapter_closed",
    ]
    manager_close.assert_called_once()
    adapter_close.assert_called_once()
    assert not result._api_jobs
    if operation == "svm":
        if not cancel_caller:
            assert result.get_cached_svm_details("synthetic") is details
        read.assert_called_once_with("synthetic")
    else:
        read.assert_called_once_with()
