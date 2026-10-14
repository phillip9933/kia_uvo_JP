"""Climate sliders use per-vehicle bounds while preserving legacy unit defaults."""

from unittest.mock import MagicMock

import pytest
from homeassistant.const import UnitOfTemperature
from hyundai_kia_connect_api import Vehicle

from custom_components.kia_uvo.climate import HyundaiKiaCarClimateControlSwitch


def entity(bounds=None, unit=UnitOfTemperature.CELSIUS):
    vehicle = Vehicle(id="synthetic", name="synthetic", model="synthetic")
    vehicle.climate_temperature_range = bounds
    vehicle.air_temperature = (20, unit)
    vehicle.air_control_is_on = False
    return HyundaiKiaCarClimateControlSwitch(MagicMock(), vehicle)


@pytest.mark.parametrize("bounds", [(17.0, 27.0), (15.5, 29.5), (15.0, 30.0)])
def test_japan_profile_bounds_limit_the_climate_slider(bounds):
    climate = entity(bounds)
    assert climate.min_temp == bounds[0]
    assert climate.max_temp == bounds[1]
    assert climate.target_temperature_step == 0.5


@pytest.mark.parametrize(
    "unit,expected",
    [(UnitOfTemperature.CELSIUS, (14, 30)), (UnitOfTemperature.FAHRENHEIT, (62, 82))],
)
def test_unspecified_legacy_range_retains_existing_unit_bounds(unit, expected):
    climate = entity(unit=unit)
    assert (climate.min_temp, climate.max_temp) == expected
