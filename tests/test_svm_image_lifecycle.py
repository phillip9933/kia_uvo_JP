"""Shared image lifecycle regressions, independent of any regional adapter."""

import asyncio
import datetime as dt
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.exceptions import HomeAssistantError

from custom_components.kia_uvo import coordinator as module
from custom_components.kia_uvo.coordinator import HyundaiKiaConnectDataUpdateCoordinator
from custom_components.kia_uvo.image import SVMImageEntity
from custom_components.kia_uvo.sensor import SVMStatusSensor


def make_coordinator():
    result = HyundaiKiaConnectDataUpdateCoordinator.__new__(
        HyundaiKiaConnectDataUpdateCoordinator
    )
    result._svm_details = {}
    result._svm_received_at = {}
    result._svm_views = {}
    result._svm_dewarp = {}
    result._svm_render_locks = {}
    result._svm_render_diagnostics = {}
    result.hass = MagicMock()
    result.hass.async_add_executor_job = AsyncMock()
    result.vehicle_manager = MagicMock()
    result.async_set_updated_data = MagicMock()
    result.data = {}
    return result


def details(captured_at=None, image=b"synthetic"):
    return SimpleNamespace(
        captured_at=captured_at,
        image_bytes=image,
        image_sizes=(5, 1, 1, 1, 1, 1),
        heading=None,
        speed=None,
        door_open=None,
        trunk_open=None,
    )


async def test_capture_without_vehicle_time_updates_status_and_image(monkeypatch):
    result = make_coordinator()
    received = dt.datetime(2026, 11, 3, tzinfo=dt.UTC)
    monkeypatch.setattr(module.dt_util, "utcnow", lambda: received)
    capture = details()
    result.hass.async_add_executor_job.return_value = capture
    await result.async_request_svm_capture("synthetic-private-car")
    assert result.get_svm_image_updated_at("synthetic-private-car") == received
    assert capture.captured_at is None
    sensor = SVMStatusSensor.__new__(SVMStatusSensor)
    sensor.coordinator = result
    sensor.vehicle = SimpleNamespace(id="synthetic-private-car")
    assert sensor.native_value == received
    assert sensor.extra_state_attributes["timestamp_source"] == "received"
    assert sensor.extra_state_attributes["vehicle_captured_at"] is None
    image = SVMImageEntity.__new__(SVMImageEntity)
    image.coordinator = result
    image.vehicle = sensor.vehicle
    image.async_write_ha_state = MagicMock()
    image._handle_coordinator_update()
    assert image.image_last_updated == received


def test_vehicle_capture_time_preserved():
    result = make_coordinator()
    capture_time = dt.datetime(2026, 11, 2, tzinfo=dt.UTC)
    result._cache_svm_details("synthetic-private-car", details(capture_time))
    assert result.get_svm_image_updated_at("synthetic-private-car") == capture_time
    assert result.get_svm_image_updated_at("missing") is None


async def test_concurrent_view_requests_share_one_render():
    result = make_coordinator()
    result._cache_svm_details("car", details())
    entered, finish = asyncio.Event(), asyncio.Event()

    async def render(*args):
        entered.set()
        await finish.wait()
        return {"front": b"first"}

    result.hass.async_add_executor_job.side_effect = render
    tasks = [asyncio.create_task(result.async_get_svm_views("car")) for _ in range(5)]
    await entered.wait()
    finish.set()
    assert await asyncio.gather(*tasks) == [{"front": b"first"}] * 5
    result.hass.async_add_executor_job.assert_awaited_once()


async def test_recapture_while_rendering_cannot_serve_old_image():
    result = make_coordinator()
    result._cache_svm_details("car", details(image=b"old"))
    entered, finish = asyncio.Event(), asyncio.Event()
    calls = 0

    async def render(*args):
        nonlocal calls
        calls += 1
        if calls == 1:
            entered.set()
            await finish.wait()
            return {"front": b"old"}
        return {"front": b"new"}

    result.hass.async_add_executor_job.side_effect = render
    task = asyncio.create_task(result.async_get_svm_views("car"))
    await entered.wait()
    result._cache_svm_details("car", details(image=b"new"))
    finish.set()
    assert await task == {"front": b"new"}
    assert calls == 2


async def test_dewarp_toggle_while_rendering_invalidates_old_result():
    result = make_coordinator()
    result._cache_svm_details("car", details())
    entered, finish = asyncio.Event(), asyncio.Event()
    calls = 0

    async def render(*args):
        nonlocal calls
        calls += 1
        if calls == 1:
            entered.set()
            await finish.wait()
            return {"front": b"old"}
        return {"front": b"dewarped"}

    result.hass.async_add_executor_job.side_effect = render
    task = asyncio.create_task(result.async_get_svm_views("car"))
    await entered.wait()
    result._svm_dewarp["car"] = True
    finish.set()
    assert await task == {"front": b"dewarped"}
    assert calls == 2


async def test_cancellation_drains_render_and_keeps_cached_result():
    result = make_coordinator()
    result._cache_svm_details("car", details())
    entered, finish = asyncio.Event(), asyncio.Event()

    async def render(*args):
        entered.set()
        await finish.wait()
        return {"front": b"rendered"}

    result.hass.async_add_executor_job.side_effect = render
    first = asyncio.create_task(result.async_get_svm_views("car"))
    await entered.wait()
    first.cancel()
    second = asyncio.create_task(result.async_get_svm_views("car"))
    await asyncio.sleep(0)
    first.cancel()
    await asyncio.sleep(0)
    assert not first.done()
    result.hass.async_add_executor_job.assert_awaited_once()
    finish.set()
    with pytest.raises(asyncio.CancelledError):
        await first
    assert await second == {"front": b"rendered"}
    result.hass.async_add_executor_job.assert_awaited_once()


async def test_new_capture_invalidates_equal_timestamp_cache():
    result = make_coordinator()
    timestamp = dt.datetime(2026, 11, 3, tzinfo=dt.UTC)
    result._cache_svm_details("car", details(timestamp))
    result.hass.async_add_executor_job.return_value = {"front": b"old"}
    assert await result.async_get_svm_views("car") == {"front": b"old"}
    result._cache_svm_details("car", details(timestamp, b"new"))
    result.hass.async_add_executor_job.return_value = {"front": b"new"}
    assert await result.async_get_svm_views("car") == {"front": b"new"}
    assert result.hass.async_add_executor_job.await_count == 2


async def test_empty_render_has_safe_diagnostic():
    result = make_coordinator()
    result._cache_svm_details("synthetic-private-car", details())
    result.hass.async_add_executor_job.return_value = {}
    with pytest.raises(HomeAssistantError, match="could not be decoded"):
        await result.async_get_svm_views("synthetic-private-car")
    summary = result.get_svm_render_diagnostics()
    assert summary == [
        {
            "state": "failed",
            "view_count": 0,
            "failure_category": "invalid_image_or_layout",
        }
    ]
    assert "synthetic-private" not in repr(summary)


async def test_render_exception_does_not_expose_error_contents():
    result = make_coordinator()
    result._cache_svm_details("car", details())
    result.hass.async_add_executor_job.side_effect = ValueError(
        "synthetic-private-image"
    )
    with pytest.raises(HomeAssistantError, match="could not be rendered") as error:
        await result.async_get_svm_views("car")
    assert "synthetic-private" not in str(error.value)
    assert "synthetic-private" not in repr(result.get_svm_render_diagnostics())


async def test_missing_capture_and_geometry_are_distinguished():
    result = make_coordinator()
    assert await result.async_get_svm_views("car") is None
    assert result.get_svm_render_diagnostics() == [
        {"state": "awaiting_capture", "view_count": 0}
    ]
    incomplete = details()
    incomplete.image_sizes = None
    result._cache_svm_details("car", incomplete)
    assert await result.async_get_svm_views("car") is None
    assert result.get_svm_render_diagnostics() == [
        {"state": "missing_image_or_layout", "view_count": 0}
    ]
    result.hass.async_add_executor_job.assert_not_awaited()
