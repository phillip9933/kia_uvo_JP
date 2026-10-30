"""Coordinator for Hyundai / Kia Connect integration."""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import traceback
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import timedelta
from functools import partial
from typing import Any, TypeVar

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    CONF_PASSWORD,
    CONF_PIN,
    CONF_REGION,
    CONF_SCAN_INTERVAL,
    CONF_USERNAME,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util
from hyundai_kia_connect_api import (
    ClimateRequestOptions,
    POIInfo,
    ScheduleChargingClimateRequestOptions,
    SVMDetails,
    Token,
    Vehicle,
    VehicleManager,
    WindowRequestOptions,
)
from hyundai_kia_connect_api.const import ORDER_STATUS, WINDOW_STATE
from hyundai_kia_connect_api.exceptions import (
    AuthenticationError,
    UnsupportedControlError,
)
from hyundai_kia_connect_api.svm_image import render_views

from .const import (
    CONF_BRAND,
    CONF_ENABLE_GEOLOCATION_ENTITY,
    CONF_FORCE_REFRESH_INTERVAL,
    CONF_NO_FORCE_REFRESH_HOUR_FINISH,
    CONF_NO_FORCE_REFRESH_HOUR_START,
    CONF_TOKEN,
    CONF_USE_EMAIL_WITH_GEOCODE_API,
    DEFAULT_ENABLE_GEOLOCATION_ENTITY,
    DEFAULT_FORCE_REFRESH_INTERVAL,
    DEFAULT_NO_FORCE_REFRESH_HOUR_FINISH,
    DEFAULT_NO_FORCE_REFRESH_HOUR_START,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_USE_EMAIL_WITH_GEOCODE_API,
    DOMAIN,
    OffPeakChargingMode,
)

_LOGGER = logging.getLogger(__name__)

_T = TypeVar("_T")

# Render-invalidation signal for the SVM image entities: sent by
# set_svm_dewarp, consumed in image.py.
SIGNAL_SVM_RENDER = DOMAIN + "_{}_svm_render"


class _ConfirmedCommandRefreshError(HomeAssistantError):
    """The command completed but its subsequent telemetry refresh failed."""


class _UnconfirmedCommandError(HomeAssistantError):
    """A submitted command has a known, unsuccessful confirmation outcome."""


class HyundaiKiaConnectDataUpdateCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Class to manage fetching data from the API."""

    def __init__(self, hass: HomeAssistant, config_entry: ConfigEntry) -> None:
        """Initialize."""
        self.platforms: set[str] = set()
        self._action_lock = asyncio.Lock()
        self._closing = False
        self._svm_details: dict[str, SVMDetails] = {}
        self._svm_received_at: dict[str, dt.datetime] = {}
        self._svm_render_locks: dict[str, asyncio.Lock] = {}
        self._svm_render_diagnostics: dict[str, dict[str, Any]] = {}
        # Per-vehicle SVM fisheye dewarp toggle (local UI state, off by default).
        self._svm_dewarp: dict[str, bool] = {}
        # Rendered SVM views cache: vehicle_id -> ((captured_at, dewarp), views).
        # Invalidated by key change on a new capture or a switch toggle — no
        # explicit invalidation hooks needed.
        self._svm_views: dict[
            str, tuple[tuple[dt.datetime | None, bool], dict[str, bytes]]
        ] = {}

        self.vehicle_manager = VehicleManager(
            region=config_entry.data.get(CONF_REGION),
            brand=config_entry.data.get(CONF_BRAND),
            username=config_entry.data.get(CONF_USERNAME),
            password=config_entry.data.get(CONF_PASSWORD),
            pin=config_entry.data.get(CONF_PIN),
            geocode_api_enable=config_entry.options.get(
                CONF_ENABLE_GEOLOCATION_ENTITY, DEFAULT_ENABLE_GEOLOCATION_ENTITY
            ),
            geocode_api_use_email=config_entry.options.get(
                CONF_USE_EMAIL_WITH_GEOCODE_API, DEFAULT_USE_EMAIL_WITH_GEOCODE_API
            ),
            language=hass.config.language,
            token=Token.from_dict(config_entry.data.get(CONF_TOKEN, None))
            if config_entry.data.get(CONF_TOKEN, None)
            else None,
        )
        self.scan_interval: int = (
            config_entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL) * 60
        )
        self.force_refresh_interval: int = (
            config_entry.options.get(
                CONF_FORCE_REFRESH_INTERVAL, DEFAULT_FORCE_REFRESH_INTERVAL
            )
            * 60
        )
        self.no_force_refresh_hour_start: int = config_entry.options.get(
            CONF_NO_FORCE_REFRESH_HOUR_START, DEFAULT_NO_FORCE_REFRESH_HOUR_START
        )
        self.no_force_refresh_hour_finish: int = config_entry.options.get(
            CONF_NO_FORCE_REFRESH_HOUR_FINISH, DEFAULT_NO_FORCE_REFRESH_HOUR_FINISH
        )
        self.enable_geolocation_entity = config_entry.options.get(
            CONF_ENABLE_GEOLOCATION_ENTITY, DEFAULT_ENABLE_GEOLOCATION_ENTITY
        )
        self.use_email_with_geocode_api = config_entry.options.get(
            CONF_USE_EMAIL_WITH_GEOCODE_API, DEFAULT_USE_EMAIL_WITH_GEOCODE_API
        )

        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(
                seconds=min(self.scan_interval, self.force_refresh_interval)
            ),
        )
        _LOGGER.debug(
            "%s - Polling configured: scan_interval=%ds, "
            "force_refresh_interval=%ds, update_interval=%ds, "
            "no_force_refresh_hours=%d-%d",
            DOMAIN,
            self.scan_interval,
            self.force_refresh_interval,
            min(self.scan_interval, self.force_refresh_interval),
            self.no_force_refresh_hour_start,
            self.no_force_refresh_hour_finish,
        )

    @property
    def supports_remote_commands(self) -> bool:
        """Whether this API implements vehicle commands."""
        return bool(getattr(self.vehicle_manager.api, "supports_remote_commands", True))

    @property
    def supports_force_refresh(self) -> bool:
        """Whether this API implements requests for fresh vehicle status."""
        return bool(getattr(self.vehicle_manager.api, "supports_force_refresh", True))

    def supports_command(self, command: str) -> bool:
        """Whether this API implements the named vehicle command."""
        commands = getattr(self.vehicle_manager.api, "supported_commands", None)
        return self.supports_remote_commands and (
            commands is None or command in commands
        )

    def _require_command(self, command: str) -> None:
        self._require_remote_commands()
        if not self.supports_command(command):
            raise HomeAssistantError(
                "This region does not support this vehicle command"
            )

    def _ensure_not_closing(self) -> None:
        if getattr(self, "_closing", False):
            raise HomeAssistantError("The integration is shutting down")

    def _require_remote_commands(self) -> None:
        self._ensure_not_closing()
        if not self.supports_remote_commands:
            raise HomeAssistantError("This region does not support vehicle commands")

    def _require_force_refresh(self) -> None:
        self._ensure_not_closing()
        if not self.supports_force_refresh:
            raise HomeAssistantError("This region supports cached vehicle status only")

    @property
    def requires_command_confirmation(self) -> bool:
        return (
            getattr(self.vehicle_manager.api, "requires_command_confirmation", False)
            is True
        )

    @asynccontextmanager
    async def _async_region_operation(self) -> AsyncIterator[None]:
        self._ensure_not_closing()
        if not self.requires_command_confirmation:
            yield
            return
        if self._action_lock.locked():
            raise HomeAssistantError("Another vehicle operation is in progress")
        async with self._action_lock:
            yield

    async def _async_complete_operation(self, operation: Awaitable[_T]) -> _T:
        """Finish Japan work before propagating cancellation to its lock owner."""
        if not self.requires_command_confirmation:
            return await operation
        task = asyncio.ensure_future(operation)
        cancelled = False
        while True:
            try:
                result = await asyncio.shield(task)
            except asyncio.CancelledError:
                if task.cancelled():
                    raise
                cancelled = True
            except Exception:
                if cancelled:
                    raise asyncio.CancelledError from None
                raise
            else:
                if cancelled:
                    raise asyncio.CancelledError
                return result

    async def _async_run_executor_job(
        self, target: Callable[..., _T], *args: Any
    ) -> _T:
        """Drain non-cancellable executor work before releasing Japan's lock."""
        return await self._async_complete_operation(
            self.hass.async_add_executor_job(target, *args)
        )

    async def async_shutdown(self) -> None:
        """Drain active Japan operations before disposing of their API sessions."""
        self._closing = True
        await self._async_complete_operation(self._async_close_api())

    async def _async_close_api(self) -> None:
        async with self._action_lock:
            close = getattr(self.vehicle_manager.api, "close", None)
            if callable(close):
                await self._async_run_executor_job(close)

    async def _async_update_data(self) -> dict[str, Any]:
        if getattr(self, "_closing", False):
            return self.data
        if not self.requires_command_confirmation:
            return await self._async_update_data_unlocked()
        if self._action_lock.locked():
            return self.data
        async with self._action_lock:
            return await self._async_update_data_unlocked()

    async def _async_update_data_unlocked(self) -> dict[str, Any]:
        """Update data via library. Called by update_coordinator periodically.

        Allow to update for the first time without further checking
        Allow force update, if time diff between latest update and `now` is greater than force refresh delta
        """
        _LOGGER.debug(
            "%s - _async_update_data called, scan_interval=%ds, force_refresh_interval=%ds",
            DOMAIN,
            self.scan_interval,
            self.force_refresh_interval,
        )
        try:
            await self.async_check_and_refresh_token()
        except AuthenticationError as AuthError:
            raise ConfigEntryAuthFailed(AuthError) from AuthError
        except Exception as err:
            # Transient API errors (e.g. DeviceIDError, ReadTimeoutError) from
            # Kia's EU backend must be surfaced as UpdateFailed rather than
            # propagating as unexpected exceptions.  HA's update coordinator
            # counts unexpected exceptions and cancels the config entry after
            # enough consecutive failures, which makes all entities permanently
            # unavailable until the integration is manually reloaded.
            # Raising UpdateFailed(retry_after=60) keeps entities temporarily
            # unavailable and schedules an automatic retry after 60 seconds
            # instead of waiting for the next full poll interval.
            # See: https://github.com/Hyundai-Kia-Connect/kia_uvo/issues/1538
            raise UpdateFailed(
                f"Token refresh failed, will retry in 60s: {err}",
                retry_after=60,
            ) from err
        if not self.supports_force_refresh:
            try:
                await self._async_run_executor_job(
                    self.vehicle_manager.update_all_vehicles_with_cached_state
                )
            except AuthenticationError as err:
                raise ConfigEntryAuthFailed(
                    "Cached status authentication failed"
                ) from err
            except Exception as err:
                raise UpdateFailed("Unable to retrieve cached vehicle status") from err
            return self.data
        current_hour = dt_util.now().hour

        if (
            (self.no_force_refresh_hour_start <= self.no_force_refresh_hour_finish)
            and (
                current_hour < self.no_force_refresh_hour_start
                or current_hour >= self.no_force_refresh_hour_finish
            )
        ) or (
            (self.no_force_refresh_hour_start >= self.no_force_refresh_hour_finish)
            and (
                current_hour < self.no_force_refresh_hour_start
                and current_hour >= self.no_force_refresh_hour_finish
            )
        ):
            try:
                await self._async_run_executor_job(
                    self.vehicle_manager.check_and_force_update_vehicles,
                    self.force_refresh_interval,
                )
            except Exception:
                try:
                    _LOGGER.exception(
                        f"Force update failed, falling back to cached: {traceback.format_exc()}"
                    )
                    await self._async_run_executor_job(
                        self.vehicle_manager.update_all_vehicles_with_cached_state
                    )
                except Exception:
                    _LOGGER.exception(f"Cached update failed: {traceback.format_exc()}")
                    raise UpdateFailed(
                        f"Error communicating with API: {traceback.format_exc()}"
                    )

        else:
            await self._async_run_executor_job(
                self.vehicle_manager.update_all_vehicles_with_cached_state
            )

        return self.data

    async def async_update_all(self) -> None:
        """Update vehicle data."""
        async with self._async_region_operation():
            await self.async_check_and_refresh_token()
            await self._async_run_executor_job(
                self.vehicle_manager.update_all_vehicles_with_cached_state
            )
            self.async_set_updated_data(self.data)

    async def async_force_update_all(self) -> None:
        """Force refresh vehicle data and update it."""
        self._require_force_refresh()
        async with self._async_region_operation():
            await self.async_check_and_refresh_token()
            await self._async_run_executor_job(
                self.vehicle_manager.force_refresh_all_vehicles_states
            )
            self.async_set_updated_data(self.data)

    async def async_force_refresh_vehicle(self, vehicle_id: str) -> None:
        """Force refresh a single vehicle's state."""
        self._require_force_refresh()
        async with self._async_region_operation():
            await self.async_check_and_refresh_token()
            await self._async_run_executor_job(
                self.vehicle_manager.force_refresh_vehicle_state, vehicle_id
            )
            self.async_set_updated_data(self.data)

    async def async_supports_svm(self, vehicle_id: str) -> bool:
        """Return whether the given vehicle supports SVM.

        Capability is a per-region class attribute stamped on the Vehicle by
        the API library (like supports_window_control), so this is a plain
        attribute read — no API call, no executor job needed.
        """
        vehicle = self.vehicle_manager.vehicles.get(vehicle_id)
        if vehicle is None:
            return False
        return bool(vehicle.supports_svm)

    def svm_dewarp_enabled(self, vehicle_id: str) -> bool:
        """Return the per-vehicle SVM fisheye dewarp preference."""
        return self._svm_dewarp.get(vehicle_id, False)

    def set_svm_dewarp(self, vehicle_id: str, enabled: bool) -> None:
        """Set the per-vehicle SVM fisheye dewarp preference."""
        self._svm_dewarp[vehicle_id] = enabled
        # The render key changes with the toggle, but captured_at does not —
        # without this signal the image proxy keeps serving the pre-toggle
        # image until the next capture.
        async_dispatcher_send(self.hass, SIGNAL_SVM_RENDER.format(vehicle_id))

    def get_cached_svm_details(self, vehicle_id: str) -> SVMDetails | None:
        """Return cached SVM details for a vehicle, or None if not yet fetched."""
        return self._svm_details.get(vehicle_id)

    def get_svm_image_updated_at(self, vehicle_id: str) -> dt.datetime | None:
        """Return the capture time, or local receipt time when GPS time is absent."""
        details = self.get_cached_svm_details(vehicle_id)
        if details is None:
            return None
        return details.captured_at or self._svm_received_at.get(vehicle_id)

    def _cache_svm_details(self, vehicle_id: str, details: SVMDetails) -> None:
        """Store a completed capture and invalidate previous rendered views."""
        self._svm_details[vehicle_id] = details
        self._svm_received_at[vehicle_id] = dt_util.utcnow()
        self._svm_views.pop(vehicle_id, None)

    async def async_get_svm_details(self, vehicle_id: str) -> SVMDetails:
        """Fetch the latest cached SVM image and metadata from the API."""
        details = await self._async_run_executor_job(
            self.vehicle_manager.get_svm_details, vehicle_id
        )
        self._cache_svm_details(vehicle_id, details)
        return details

    async def async_request_svm_capture(self, vehicle_id: str) -> SVMDetails:
        """Trigger a fresh SVM capture and update the cached details."""
        self._require_command("request_svm_capture")
        async with self._async_region_operation():
            return await self._async_complete_operation(
                self._async_capture_svm_locked(vehicle_id)
            )

    async def _async_capture_svm_locked(self, vehicle_id: str) -> SVMDetails:
        """Complete capture and cache updates even if the initiating task stops."""
        if self.requires_command_confirmation:
            await self.async_check_and_refresh_token()
        details = await self._async_run_executor_job(
            self.vehicle_manager.request_svm_capture,
            vehicle_id,
            True,  # acknowledged_warning — capture is always a user-initiated action
        )
        self._cache_svm_details(vehicle_id, details)
        self.async_set_updated_data(self.data)
        return details

    async def async_get_svm_views(self, vehicle_id: str) -> dict[str, bytes] | None:
        """Return the 5 rendered SVM views (front/rear/left/right/top) as JPEG.

        Renders once per (captured_at, dewarp switch) state: every image
        entity serves from the same dict, and a new capture or a switch
        toggle invalidates it by key change. The render runs in an executor
        thread (dewarp is CPU-bound numpy work); render errors (e.g. missing
        Pillow) propagate to the caller.
        """
        if not hasattr(self, "_svm_render_locks"):
            self._svm_render_locks = {}
            self._svm_render_diagnostics = {}
        lock = self._svm_render_locks.setdefault(vehicle_id, asyncio.Lock())
        async with lock:
            while True:
                details = self.get_cached_svm_details(vehicle_id)
                if (
                    details is None
                    or not details.image_bytes
                    or not details.image_sizes
                ):
                    self._svm_render_diagnostics[vehicle_id] = {
                        "state": "awaiting_capture"
                        if details is None
                        else "missing_image_or_layout",
                        "view_count": 0,
                    }
                    return None
                key = (
                    self.get_svm_image_updated_at(vehicle_id),
                    self.svm_dewarp_enabled(vehicle_id),
                )
                cached = self._svm_views.get(vehicle_id)
                if cached is not None and cached[0] == key:
                    return cached[1]
                self._svm_render_diagnostics[vehicle_id] = {
                    "state": "rendering",
                    "view_count": 0,
                }
                try:
                    views: dict[str, bytes] = await self._async_run_executor_job(
                        partial(render_views, details, dewarp=key[1])
                    )
                except Exception:
                    self._svm_render_diagnostics[vehicle_id] = {
                        "state": "failed",
                        "view_count": 0,
                        "failure_category": "render_failed",
                    }
                    raise HomeAssistantError(
                        "Camera image could not be rendered"
                    ) from None
                if (
                    self.get_cached_svm_details(vehicle_id) is not details
                    or self.svm_dewarp_enabled(vehicle_id) != key[1]
                ):
                    continue
                if not views:
                    self._svm_render_diagnostics[vehicle_id] = {
                        "state": "failed",
                        "view_count": 0,
                        "failure_category": "invalid_image_or_layout",
                    }
                    raise HomeAssistantError(
                        "Camera image or layout could not be decoded"
                    )
                self._svm_views[vehicle_id] = (key, views)
                self._svm_render_diagnostics[vehicle_id] = {
                    "state": "complete",
                    "view_count": len(views),
                }
                return views

    def get_svm_render_diagnostics(self) -> list[dict[str, Any]]:
        """Return local render outcomes without images or vehicle identifiers."""
        return [
            dict(summary)
            for summary in list(getattr(self, "_svm_render_diagnostics", {}).values())[
                :10
            ]
        ]

    async def async_check_and_refresh_token(self) -> None:
        """Refresh token if needed via library."""
        await self._async_run_executor_job(self.vehicle_manager.check_and_refresh_token)
        await self._async_save_token()

    def _check_command_confirmation(self, status: ORDER_STATUS | None) -> None:
        """Require a confirmed result when the adapter declares this contract."""
        if (
            getattr(self.vehicle_manager.api, "requires_command_confirmation", False)
            and status != ORDER_STATUS.SUCCESS
        ):
            reason = {
                ORDER_STATUS.FAILED: "the service reported failure",
                ORDER_STATUS.TIMEOUT: "no matching completion reply arrived before timeout",
                ORDER_STATUS.PENDING: "completion is still pending",
            }.get(status, "the completion status is unknown")
            raise _UnconfirmedCommandError(
                "The vehicle command was sent but successful completion was not confirmed: "
                + reason
            )

    async def _async_refresh_confirmed_command(self, confirmed: bool) -> None:
        """Read cached status directly while the operation lock is already held."""
        try:
            await self._async_run_executor_job(
                self.vehicle_manager.update_all_vehicles_with_cached_state
            )
            self.async_set_updated_data(self.data)
        except Exception as err:
            if confirmed:
                raise _ConfirmedCommandRefreshError(
                    "The vehicle command completed, but updated telemetry could not be retrieved"
                ) from err
            _LOGGER.warning("Cached status refresh after unconfirmed command failed")

    async def async_await_action_and_refresh(
        self, vehicle_id: str, action_id: str
    ) -> None:
        confirmed = False
        try:
            await asyncio.sleep(5)
            status = await self._async_run_executor_job(
                self.vehicle_manager.check_action_status,
                vehicle_id,
                action_id,
                True,
                60,
            )
            confirmed = status == ORDER_STATUS.SUCCESS
            self._check_command_confirmation(status)
        finally:
            if self.requires_command_confirmation:
                await self._async_refresh_confirmed_command(confirmed)
            else:
                await self.async_refresh()

    async def async_await_action_and_force_refresh(
        self, vehicle_id: str, action_id: str
    ) -> None:
        """Wait for action then force refresh to get fresh vehicle data.

        Used after setting charge limits because the soft refresh (cmm/gvi)
        does not return targetSOC for some vehicles. A force refresh (rems/rvs)
        ensures the fresh charge limits are read back immediately.

        Uses async_set_updated_data instead of async_refresh to avoid a
        redundant cmm/gvi API call — the force refresh already updates the
        vehicle objects in-place (rems/rvs + cmm/gvi), so we just need to
        notify HA entities to re-read their state.
        """
        confirmed = False
        try:
            await asyncio.sleep(5)
            status = await self._async_run_executor_job(
                self.vehicle_manager.check_action_status,
                vehicle_id,
                action_id,
                True,
                60,
            )
            confirmed = status == ORDER_STATUS.SUCCESS
            self._check_command_confirmation(status)
        finally:
            if self.requires_command_confirmation:
                await self._async_refresh_confirmed_command(confirmed)
            else:
                try:
                    await self._async_run_executor_job(
                        self.vehicle_manager.force_refresh_vehicle_state, vehicle_id
                    )
                except Exception:
                    _LOGGER.exception("Force refresh after call failed")
                self.async_set_updated_data(self.data)

    async def _async_send_action(
        self,
        vehicle_id: str,
        action_fn: Callable[[], Any],
        error_label: str,
        *,
        force_refresh: bool = False,
        command: str | None = None,
    ) -> None:
        """Send a vehicle action, wait for completion, and refresh data.

        Serializes actions with a lock to prevent DuplicateRequestError
        from the Hyundai API when commands overlap. If another action is
        already in progress, raises HomeAssistantError immediately so
        the user gets a clear message instead of a mysterious long wait.
        """
        if command is None:
            self._require_remote_commands()
        else:
            self._require_command(command)
        if force_refresh:
            self._require_force_refresh()
        if self._action_lock.locked():
            _LOGGER.warning(
                "Vehicle action '%s' rejected: another action is already in progress",
                error_label,
            )
            raise HomeAssistantError(
                "Another vehicle action is in progress. "
                "Please wait for it to complete and try again."
            )
        async with self._action_lock:
            await self._async_complete_operation(
                self._async_send_action_locked(
                    vehicle_id, action_fn, error_label, force_refresh
                )
            )

    async def _async_send_action_locked(
        self,
        vehicle_id: str,
        action_fn: Callable[[], Any],
        error_label: str,
        force_refresh: bool,
    ) -> None:
        """Hold the caller's operation lock through submission and confirmation."""
        await self.async_check_and_refresh_token()
        try:
            action_id = await self._async_run_executor_job(action_fn)
        except UnsupportedControlError as err:
            raise HomeAssistantError(
                f"Vehicle does not support this action: {err}"
            ) from err
        except Exception as err:
            raise HomeAssistantError(f"Failed to {error_label}: {err}") from err
        try:
            if force_refresh:
                await self.async_await_action_and_force_refresh(vehicle_id, action_id)
            else:
                await self.async_await_action_and_refresh(vehicle_id, action_id)
        except (_ConfirmedCommandRefreshError, _UnconfirmedCommandError):
            raise
        except Exception as err:
            if self.requires_command_confirmation:
                raise HomeAssistantError(
                    "The vehicle command was sent but successful completion was not confirmed"
                ) from err
            _LOGGER.exception(
                "Action '%s' was sent but confirmation polling failed",
                error_label,
            )

    async def async_lock_vehicle(self, vehicle_id: str) -> None:
        await self._async_send_action(
            vehicle_id,
            lambda: self.vehicle_manager.lock(vehicle_id),
            "lock vehicle",
            command="lock_action",
        )

    async def async_unlock_vehicle(self, vehicle_id: str) -> None:
        await self._async_send_action(
            vehicle_id,
            lambda: self.vehicle_manager.unlock(vehicle_id),
            "unlock vehicle",
            command="lock_action",
        )

    async def async_open_charge_port(self, vehicle_id: str) -> None:
        await self._async_send_action(
            vehicle_id,
            lambda: self.vehicle_manager.open_charge_port(vehicle_id),
            "open charge port",
            command="charge_port_action",
        )

    async def async_close_charge_port(self, vehicle_id: str) -> None:
        await self._async_send_action(
            vehicle_id,
            lambda: self.vehicle_manager.close_charge_port(vehicle_id),
            "close charge port",
            command="charge_port_action",
        )

    async def async_start_climate_default(self, vehicle_id: str) -> None:
        """Start climate with default options (API fills sensible defaults)."""
        await self.async_start_climate(vehicle_id, ClimateRequestOptions())

    async def async_start_climate(
        self, vehicle_id: str, climate_options: ClimateRequestOptions
    ) -> None:
        await self._async_send_action(
            vehicle_id,
            lambda: self.vehicle_manager.start_climate(vehicle_id, climate_options),
            "start climate",
            command="start_climate",
        )

    async def async_stop_climate(self, vehicle_id: str) -> None:
        await self._async_send_action(
            vehicle_id,
            lambda: self.vehicle_manager.stop_climate(vehicle_id),
            "stop climate",
            command="stop_climate",
        )

    async def async_start_charge(self, vehicle_id: str) -> None:
        await self._async_send_action(
            vehicle_id,
            lambda: self.vehicle_manager.start_charge(vehicle_id),
            "start charge",
            command="start_charge",
        )

    async def async_stop_charge(self, vehicle_id: str) -> None:
        await self._async_send_action(
            vehicle_id,
            lambda: self.vehicle_manager.stop_charge(vehicle_id),
            "stop charge",
            command="stop_charge",
        )

    async def async_set_charge_limits(self, vehicle_id: str, ac: int, dc: int) -> None:
        await self._async_send_action(
            vehicle_id,
            lambda: self.vehicle_manager.set_charge_limits(vehicle_id, ac, dc),
            "set charge limits",
            command="set_charge_limits",
        )

    async def async_set_charging_current(self, vehicle_id: str, level: int) -> None:
        await self._async_send_action(
            vehicle_id,
            lambda: self.vehicle_manager.set_charging_current(vehicle_id, level),
            "set charging current",
            command="set_charging_current",
        )

    async def async_schedule_charging_and_climate(
        self, vehicle_id: str, schedule_options: ScheduleChargingClimateRequestOptions
    ) -> None:
        await self._async_send_action(
            vehicle_id,
            lambda: self.vehicle_manager.schedule_charging_and_climate(
                vehicle_id, schedule_options
            ),
            "schedule charging and climate",
            command="schedule_charging_and_climate",
        )

    def _build_schedule_options_from_vehicle(
        self, vehicle: Vehicle
    ) -> ScheduleChargingClimateRequestOptions:
        """Build schedule options from current vehicle state for partial updates."""
        return ScheduleChargingClimateRequestOptions(
            first_departure=ScheduleChargingClimateRequestOptions.DepartureOptions(
                enabled=vehicle.ev_first_departure_enabled or False,
                days=vehicle.ev_first_departure_days or [0],
                time=vehicle.ev_first_departure_time or dt.time(),
            ),
            second_departure=ScheduleChargingClimateRequestOptions.DepartureOptions(
                enabled=vehicle.ev_second_departure_enabled or False,
                days=vehicle.ev_second_departure_days or [0],
                time=vehicle.ev_second_departure_time or dt.time(),
            ),
            charging_enabled=vehicle.ev_schedule_charge_enabled or False,
            off_peak_start_time=vehicle.ev_off_peak_start_time or dt.time(),
            off_peak_end_time=vehicle.ev_off_peak_end_time or dt.time(),
            off_peak_charge_only_enabled=vehicle.ev_off_peak_charge_only_enabled
            or False,
            climate_enabled=vehicle.ev_first_departure_climate_enabled or False,
            temperature=vehicle.ev_first_departure_climate_temperature or 21.0,
            temperature_unit=vehicle._ev_first_departure_climate_temperature_unit or 0,
            defrost=vehicle.ev_first_departure_climate_defrost or False,
        )

    async def async_set_schedule_charge_enabled(
        self, vehicle_id: str, enabled: bool
    ) -> None:
        """Toggle scheduled charging on/off."""
        vehicle = self.vehicle_manager.vehicles[vehicle_id]
        options = self._build_schedule_options_from_vehicle(vehicle)
        options.charging_enabled = enabled
        await self.async_schedule_charging_and_climate(vehicle_id, options)

    async def async_set_off_peak_charge_only_enabled(
        self, vehicle_id: str, enabled: bool
    ) -> None:
        """Toggle off-peak charge only on/off."""
        vehicle = self.vehicle_manager.vehicles[vehicle_id]
        options = self._build_schedule_options_from_vehicle(vehicle)
        options.off_peak_charge_only_enabled = enabled
        await self.async_schedule_charging_and_climate(vehicle_id, options)

    async def async_set_off_peak_charging(
        self,
        vehicle_id: str,
        *,
        mode: OffPeakChargingMode | None = None,
        start: dt.time | None = None,
        end: dt.time | None = None,
    ) -> None:
        """Set the off-peak charging schedule mode and/or window.

        ``mode`` maps to the (charging_enabled, off_peak_charge_only_enabled)
        pair the API expects: ``OFF`` disables scheduled charging, ``TIME``
        charges only during the off-peak window (time priority), ``TARGET``
        prefers off-peak tariffs but continues past the window to reach the
        target SoC (target priority). ``mode=None`` preserves the current mode
        and only adjusts the window — used by the time entities. Other
        schedule fields (departures) are preserved.
        """
        vehicle = self.vehicle_manager.vehicles[vehicle_id]
        options = self._build_schedule_options_from_vehicle(vehicle)
        if mode is not None:
            if mode is OffPeakChargingMode.OFF:
                options.charging_enabled = False
            elif mode is OffPeakChargingMode.TIME:
                options.charging_enabled = True
                options.off_peak_charge_only_enabled = True
            elif mode is OffPeakChargingMode.TARGET:
                options.charging_enabled = True
                options.off_peak_charge_only_enabled = False
        if start is not None:
            options.off_peak_start_time = start
        if end is not None:
            options.off_peak_end_time = end
        await self.async_schedule_charging_and_climate(vehicle_id, options)

    async def async_set_departure_enabled(
        self, vehicle_id: str, departure_num: int, enabled: bool
    ) -> None:
        """Toggle a departure schedule on/off."""
        vehicle = self.vehicle_manager.vehicles[vehicle_id]
        options = self._build_schedule_options_from_vehicle(vehicle)
        if departure_num == 1:
            options.first_departure.enabled = enabled
        else:
            options.second_departure.enabled = enabled
        # reservFlag (charging_enabled) must be 1 for departure slots to take
        # effect. If the vehicle doesn't expose ev_schedule_charge_enabled
        # (None), the builder defaults it to False, causing the API to accept
        # the request but ignore per-slot reservChargeSet.
        if enabled and not options.charging_enabled:
            options.charging_enabled = True
        await self.async_schedule_charging_and_climate(vehicle_id, options)

    async def async_set_departure_climate_enabled(
        self, vehicle_id: str, departure_num: int, enabled: bool
    ) -> None:
        """Toggle departure climate on/off."""
        vehicle = self.vehicle_manager.vehicles[vehicle_id]
        options = self._build_schedule_options_from_vehicle(vehicle)
        options.climate_enabled = enabled
        await self.async_schedule_charging_and_climate(vehicle_id, options)

    async def async_set_departure_defrost(
        self, vehicle_id: str, departure_num: int, enabled: bool
    ) -> None:
        """Toggle departure defrost on/off."""
        vehicle = self.vehicle_manager.vehicles[vehicle_id]
        options = self._build_schedule_options_from_vehicle(vehicle)
        options.defrost = enabled
        await self.async_schedule_charging_and_climate(vehicle_id, options)

    async def async_start_hazard_lights(self, vehicle_id: str) -> None:
        await self._async_send_action(
            vehicle_id,
            lambda: self.vehicle_manager.start_hazard_lights(vehicle_id),
            "start hazard lights",
            command="start_hazard_lights",
        )

    async def async_start_hazard_lights_and_horn(self, vehicle_id: str) -> None:
        await self._async_send_action(
            vehicle_id,
            lambda: self.vehicle_manager.start_hazard_lights_and_horn(vehicle_id),
            "start hazard lights and horn",
            command="start_hazard_lights_and_horn",
        )

    async def async_start_valet_mode(self, vehicle_id: str) -> None:
        await self._async_send_action(
            vehicle_id,
            lambda: self.vehicle_manager.start_valet_mode(vehicle_id),
            "start valet mode",
            command="valet_mode_action",
        )

    async def async_stop_valet_mode(self, vehicle_id: str) -> None:
        await self._async_send_action(
            vehicle_id,
            lambda: self.vehicle_manager.stop_valet_mode(vehicle_id),
            "stop valet mode",
            command="valet_mode_action",
        )

    async def async_set_v2l_limit(self, vehicle_id: str, limit: int) -> None:
        await self._async_send_action(
            vehicle_id,
            lambda: self.vehicle_manager.set_vehicle_to_load_discharge_limit(
                vehicle_id, limit
            ),
            "set V2L limit",
            command="set_vehicle_to_load_discharge_limit",
        )

    async def async_set_windows(
        self, vehicle_id: str, windowOptions: WindowRequestOptions
    ) -> None:
        await self._async_send_action(
            vehicle_id,
            lambda: self.vehicle_manager.set_windows_state(vehicle_id, windowOptions),
            "set windows",
            command="set_windows_state",
        )

    async def async_set_navigation(
        self, vehicle_id: str, poi_list: list[POIInfo]
    ) -> None:
        await self._async_send_action(
            vehicle_id,
            lambda: self.vehicle_manager.set_navigation(vehicle_id, poi_list),
            "set navigation",
            command="set_navigation",
        )

    def _window_options(
        self, vehicle_id: str, state: WINDOW_STATE
    ) -> WindowRequestOptions:
        vehicle = self.vehicle_manager.vehicles[vehicle_id]
        positions = getattr(vehicle, "supported_window_positions", None)
        if positions is None:
            positions = ("front_left", "front_right", "back_left", "back_right")
        return WindowRequestOptions(**{position: state for position in positions})

    async def async_open_all_windows(self, vehicle_id: str) -> None:
        options = self._window_options(vehicle_id, WINDOW_STATE.OPEN)
        await self._async_send_action(
            vehicle_id,
            lambda: self.vehicle_manager.set_windows_state(vehicle_id, options),
            "open all windows",
            command="set_windows_state",
        )

    async def async_close_all_windows(self, vehicle_id: str) -> None:
        options = self._window_options(vehicle_id, WINDOW_STATE.CLOSED)
        await self._async_send_action(
            vehicle_id,
            lambda: self.vehicle_manager.set_windows_state(vehicle_id, options),
            "close all windows",
            command="set_windows_state",
        )

    async def async_vent_all_windows(self, vehicle_id: str) -> None:
        options = self._window_options(vehicle_id, WINDOW_STATE.VENTILATION)
        await self._async_send_action(
            vehicle_id,
            lambda: self.vehicle_manager.set_windows_state(vehicle_id, options),
            "vent all windows",
            command="set_windows_state",
        )

    async def _async_save_token(self) -> None:
        """Persist the latest token into the config entry."""
        config_entry = self.config_entry
        assert config_entry is not None
        new_token = self.vehicle_manager.token.to_dict()
        # Only update if token actually changed
        if new_token and new_token != config_entry.data.get(CONF_TOKEN):
            updated_data = {**config_entry.data, CONF_TOKEN: new_token}
            self.hass.config_entries.async_update_entry(config_entry, data=updated_data)
