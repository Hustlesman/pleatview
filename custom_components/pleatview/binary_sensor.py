"""Charging sensor for PleatView devices that have a battery (tablets, phones)."""

from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .coordinator import PleatViewConfigEntry, PleatViewDevice
from .entity import PleatViewEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: PleatViewConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the charging sensor; TVs report no battery and get none."""
    device = entry.runtime_data
    if device.state.get("battery"):
        async_add_entities([PleatViewChargingSensor(device)])


class PleatViewChargingSensor(PleatViewEntity, BinarySensorEntity):
    """On while the device is plugged in, also when its battery is full."""

    _attr_device_class = BinarySensorDeviceClass.BATTERY_CHARGING
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, device: PleatViewDevice) -> None:
        """Initialize the sensor."""
        super().__init__(device)
        self._attr_unique_id = f"{device.device_id}_charging"

    @property
    def available(self) -> bool:
        """Return whether the device is connected and reports a battery."""
        return super().available and bool(self._device.state.get("battery"))

    @property
    def is_on(self) -> bool | None:
        """Return whether the device is plugged in."""
        return (self._device.state.get("battery") or {}).get("charging")
