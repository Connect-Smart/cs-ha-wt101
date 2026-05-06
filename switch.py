"""Switch entities for Milesight WT101 thermostats."""
from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry, ConfigSubentry
from homeassistant.const import CONF_NAME
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
from .lorawan import (
    async_send_downlink,
    build_temperature_control_enable_payload,
)


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
            [Wt101TemperatureControlSwitch(entry, subentry)],
            config_subentry_id=subentry.subentry_id,
        )

    for subentry in entry.subentries.values():
        _add(subentry)


class Wt101TemperatureControlSwitch(SwitchEntity, RestoreEntity):
    """Enable or disable the WT101's internal temperature-control loop."""

    _attr_should_poll = False
    _attr_has_entity_name = False
    _attr_icon = "mdi:thermostat-cog"

    def __init__(self, entry: ConfigEntry, subentry: ConfigSubentry) -> None:
        self._entry = entry
        self._subentry = subentry
        sub_data = subentry.data

        self._attr_name = f"{sub_data[CONF_NAME]} temperature control"
        self._attr_unique_id = f"{subentry.subentry_id}_temp_control"
        self._attr_is_on = True

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
        if last is not None and last.state in ("on", "off"):
            self._attr_is_on = last.state == "on"

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._set(False)

    async def _set(self, enable: bool) -> None:
        payload = build_temperature_control_enable_payload(enable)
        ok = await async_send_downlink(
            self.hass,
            self._entry,
            self._subentry.data,
            payload,
            label=self.entity_id,
        )
        if ok:
            self._attr_is_on = enable
            self.async_write_ha_state()
