"""Number entities for Milesight WT101 thermostats."""
from __future__ import annotations

from homeassistant.components.number import (
    NumberEntity,
    NumberMode,
)
from homeassistant.config_entries import ConfigEntry, ConfigSubentry
from homeassistant.const import CONF_NAME, UnitOfTemperature
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .const import (
    CONF_CS_DEV_EUI,
    CONF_TTN_DEVICE_ID,
    DOMAIN,
    SUBENTRY_TYPE_THERMOSTAT,
)
from .lorawan import async_send_downlink, build_temperature_calibration_payload


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    seen: set[str] = set()

    @callback
    def _add(subentry: ConfigSubentry) -> None:
        if subentry.subentry_type != SUBENTRY_TYPE_THERMOSTAT:
            return
        if subentry.subentry_id in seen:
            return
        seen.add(subentry.subentry_id)
        async_add_entities(
            [Wt101TemperatureOffsetNumber(entry, subentry)],
            config_subentry_id=subentry.subentry_id,
        )

    for subentry in entry.subentries.values():
        _add(subentry)


class Wt101TemperatureOffsetNumber(NumberEntity, RestoreEntity):
    """Set the WT101's internal temperature calibration offset."""

    _attr_should_poll = False
    _attr_has_entity_name = False
    _attr_native_min_value = -5.0
    _attr_native_max_value = 5.0
    _attr_native_step = 0.1
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_mode = NumberMode.BOX
    _attr_icon = "mdi:thermometer-plus"

    def __init__(self, entry: ConfigEntry, subentry: ConfigSubentry) -> None:
        self._entry = entry
        self._subentry = subentry
        sub_data = subentry.data

        self._attr_name = f"{sub_data[CONF_NAME]} temperature offset"
        self._attr_unique_id = f"{subentry.subentry_id}_temp_offset"
        self._attr_native_value = 0.0

        identifier = (
            sub_data.get(CONF_TTN_DEVICE_ID)
            or sub_data.get(CONF_CS_DEV_EUI)
            or subentry.subentry_id
        )
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, identifier)},
            name=sub_data[CONF_NAME],
            manufacturer="Milesight",
            model="WT101",
        )

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is not None and last.state not in (None, "unknown", "unavailable"):
            try:
                self._attr_native_value = float(last.state)
            except (TypeError, ValueError):
                pass

    async def async_set_native_value(self, value: float) -> None:
        snapped = round(float(value) * 10) / 10
        snapped = max(
            self._attr_native_min_value, min(self._attr_native_max_value, snapped)
        )
        # Always send enable=1; setting the offset to 0.0 produces no correction
        # but keeps the calibration feature active on the device.
        payload = build_temperature_calibration_payload(True, snapped)
        ok = await async_send_downlink(
            self.hass,
            self._entry,
            self._subentry.data,
            payload,
            label=self.entity_id,
        )
        if ok:
            self._attr_native_value = snapped
            self.async_write_ha_state()
