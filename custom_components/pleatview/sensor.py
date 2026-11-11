"""Battery sensor for PleatView devices that have one (tablets, phones), and how busy a
paired PleatView Renderer is."""

from __future__ import annotations

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.const import PERCENTAGE, EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import renderers
from .const import DOMAIN
from .coordinator import PleatViewConfigEntry, PleatViewDevice
from .entity import PleatViewEntity

# Screens in use, and the share of its machine's processor and memory in use.
RENDERER_SENSORS = ("screens", "cpu", "memory")


async def async_setup_entry(
    hass: HomeAssistant,
    entry: PleatViewConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the battery sensor; TVs report no battery and get none."""
    if renderers.is_renderer(entry):
        coordinator: renderers.RendererCoordinator = entry.runtime_data  # type: ignore[assignment]
        async_add_entities(RendererSensor(coordinator, entry, key) for key in RENDERER_SENSORS)
        return
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


class RendererSensor(CoordinatorEntity[renderers.RendererCoordinator], SensorEntity):
    """How busy a paired renderer is, so you can see when to add another."""

    _attr_has_entity_name = True
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: renderers.RendererCoordinator, entry: PleatViewConfigEntry, key: str) -> None:
        super().__init__(coordinator)
        self._key = key
        self._attr_translation_key = f"renderer_{key}"
        self._attr_unique_id = f"{entry.unique_id}_{key}"
        if key != "screens":
            self._attr_native_unit_of_measurement = PERCENTAGE
            self._attr_entity_category = EntityCategory.DIAGNOSTIC
        info = coordinator.data or {}
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.unique_id or entry.entry_id)},
            name=entry.title,
            manufacturer="PleatView",
            model=f"PleatView Renderer ({info.get('encoder', '?')})",
            sw_version=info.get("version"),
        )

    @property
    def native_value(self) -> float | None:
        data = self.coordinator.data or {}
        if self._key == "screens":
            return len(data.get("sessions") or {})
        value = (data.get("load") or {}).get(self._key)
        return round(float(value) * 100) if value is not None else None

    @property
    def extra_state_attributes(self) -> dict[str, int] | None:
        if self._key == "screens":
            return {"max_screens": int((self.coordinator.data or {}).get("max_screens") or 0)}
        return None
