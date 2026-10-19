"""Buttons for Milesight WT101 thermostats."""
from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry, ConfigSubentry
from homeassistant.const import CONF_NAME
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    CONF_CS_DEV_EUI,
    CONF_TTN_DEVICE_ID,
    DOMAIN,
    SUBENTRY_TYPE_THERMOSTAT,
)
from .calibration import (
    async_finish as async_finish_calibration,
    async_start as async_start_calibration,
    device_key,
)
from .lorawan import async_send_downlink, build_valve_calibration_payload


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
            [Wt101CalibrateValveButton(entry, subentry)],
            config_subentry_id=subentry.subentry_id,
        )

    for subentry in entry.subentries.values():
        _add(subentry)


class Wt101CalibrateValveButton(ButtonEntity):
    """Trigger a one-shot mechanical valve recalibration on the WT101."""

    _attr_should_poll = False
    _attr_has_entity_name = False
    _attr_icon = "mdi:valve"

    def __init__(self, entry: ConfigEntry, subentry: ConfigSubentry) -> None:
        self._entry = entry
        self._subentry = subentry
        sub_data = subentry.data

        self._attr_name = f"{sub_data[CONF_NAME]} calibrate valve"
        self._attr_unique_id = f"{subentry.subentry_id}_calibrate_valve"

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

    async def async_press(self) -> None:
        sub_data = self._subentry.data
        # Hold every other downlink first so nothing sneaks in between the
        # queue flush and the calibration, or reaches the motor mid-run.
        async_start_calibration(self.hass, sub_data)
        ok = await async_send_downlink(
            self.hass,
            self._entry,
            sub_data,
            build_valve_calibration_payload(),
            label=self.entity_id,
            force=True,
            replace_queue=True,
        )
        if not ok:
            async_finish_calibration(self.hass, device_key(sub_data), None)
