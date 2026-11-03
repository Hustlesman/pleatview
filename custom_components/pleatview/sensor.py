"""Battery sensor for PleatView devices that have one (tablets, phones)."""

from __future__ import annotations

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.const import PERCENTAGE, EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .coordinator import PleatViewConfigEntry, PleatViewDevice
from .entity import PleatViewEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: PleatViewConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the battery sensor; TVs report no battery and get none."""
    device = entry.runtime_data
    if device.state.get("battery"):
        async_add_entities([PleatViewBatterySensor(device)])


class PleatViewBatterySensor(PleatViewEntity, SensorEntity):
    """The battery level, for example to switch a wall tablet's charger on and off."""

    _attr_device_class = SensorDeviceClass.BATTERY
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, device: PleatViewDevice) -> None:
        """Initialize the sensor."""
        super().__init__(device)
        self._attr_unique_id = f"{device.device_id}_battery"

    @property
    def available(self) -> bool:
        """Return whether the device is connected and reports a battery."""
        return super().available and bool(self._device.state.get("battery"))

    @property
    def native_value(self) -> int | None:
        """Return the battery level in percent."""
        return (self._device.state.get("battery") or {}).get("level")
