"""Climate entity for the Milesight WT101 LoRaWAN thermostat.

One config entry == one LoRaWAN application (hub). Each thermostat lives as a
ConfigSubentry under that hub, so all thermostats in the same TTN/ChirpStack
application share one set of credentials.
"""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.climate import (
    ClimateEntity,
    ClimateEntityFeature,
    HVACMode,
)
from homeassistant.config_entries import ConfigEntry, ConfigSubentry
from homeassistant.const import (
    ATTR_TEMPERATURE,
    CONF_NAME,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    UnitOfTemperature,
)
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_state_change_event

from .const import (
    CONF_CS_DEV_EUI,
    CONF_CURRENT_TEMP_SENSOR,
    CONF_MAX_TEMP,
    CONF_MIN_TEMP,
    CONF_PLATFORM_TYPE,
    CONF_TARGET_TEMP_SENSOR,
    CONF_TOLERANCE,
    CONF_TTN_DEVICE_ID,
    DEFAULT_MAX_TEMP,
    DEFAULT_MIN_TEMP,
    DEFAULT_TOLERANCE,
    DOMAIN,
    PLATFORM_CHIRPSTACK,
    SUBENTRY_TYPE_THERMOSTAT,
    cs_uplink_signal,
)
from .lorawan import async_send_downlink, build_target_temperature_payload

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create one climate entity per thermostat subentry of this hub."""
    seen_subentries: set[str] = set()

    @callback
    def _add_subentry(subentry: ConfigSubentry) -> None:
        if subentry.subentry_type != SUBENTRY_TYPE_THERMOSTAT:
            return
        if subentry.subentry_id in seen_subentries:
            return
        seen_subentries.add(subentry.subentry_id)
        async_add_entities(
            [Wt101ClimateEntity(hass, entry, subentry)],
            config_subentry_id=subentry.subentry_id,
        )

    for subentry in entry.subentries.values():
        _add_subentry(subentry)


class Wt101ClimateEntity(ClimateEntity):
    """One climate entity bound to one WT101 thermostat (one subentry)."""

    _attr_hvac_modes = [HVACMode.HEAT]
    _attr_hvac_mode = HVACMode.HEAT
    _attr_supported_features = ClimateEntityFeature.TARGET_TEMPERATURE
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_target_temperature_step = 0.5
    _attr_should_poll = False
    _attr_has_entity_name = False

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        subentry: ConfigSubentry,
    ) -> None:
        self.hass = hass
        self._entry = entry
        self._subentry = subentry

        sub_data = subentry.data
        self._attr_name = sub_data[CONF_NAME]
        self._attr_unique_id = subentry.subentry_id

        self._current_sensor: str | None = sub_data.get(CONF_CURRENT_TEMP_SENSOR)
        self._target_sensor: str | None = sub_data.get(CONF_TARGET_TEMP_SENSOR)

        self._attr_min_temp = float(sub_data.get(CONF_MIN_TEMP, DEFAULT_MIN_TEMP))
        self._attr_max_temp = float(sub_data.get(CONF_MAX_TEMP, DEFAULT_MAX_TEMP))
        self._tolerance = float(sub_data.get(CONF_TOLERANCE, DEFAULT_TOLERANCE))

        self._attr_current_temperature: float | None = None
        self._attr_target_temperature: float | None = None

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

    @property
    def _platform(self) -> str:
        return self._entry.data[CONF_PLATFORM_TYPE]

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()

        tracked: list[str] = []
        if self._current_sensor:
            self._sync_from_state(
                self._current_sensor, self.hass.states.get(self._current_sensor)
            )
            tracked.append(self._current_sensor)
        if self._target_sensor:
            self._sync_from_state(
                self._target_sensor, self.hass.states.get(self._target_sensor)
            )
            tracked.append(self._target_sensor)
        if tracked:
            self.async_on_remove(
                async_track_state_change_event(
                    self.hass, tracked, self._handle_source_change
                )
            )

        # ChirpStack uplinks routed via the per-hub webhook update us directly.
        if self._platform == PLATFORM_CHIRPSTACK:
            dev_eui = self._subentry.data.get(CONF_CS_DEV_EUI)
            if dev_eui:
                self.async_on_remove(
                    async_dispatcher_connect(
                        self.hass,
                        cs_uplink_signal(self._entry.entry_id, dev_eui),
                        self._handle_uplink,
                    )
                )

    @callback
    def _handle_uplink(self, fields: dict[str, float]) -> None:
        """Apply temperatures pushed in by the ChirpStack webhook."""
        changed = False
        current = fields.get("current_temperature")
        target = fields.get("target_temperature")
        if current is not None and self._attr_current_temperature != current:
            self._attr_current_temperature = current
            changed = True
        if target is not None and self._attr_target_temperature != target:
            self._attr_target_temperature = target
            changed = True
        if changed:
            self.async_write_ha_state()

    @callback
    def _handle_source_change(self, event: Event) -> None:
        entity_id = event.data.get("entity_id")
        new_state = event.data.get("new_state")
        if self._sync_from_state(entity_id, new_state):
            self.async_write_ha_state()

    def _sync_from_state(self, entity_id: str | None, state) -> bool:
        if entity_id is None or state is None:
            return False
        if state.state in (None, "", STATE_UNAVAILABLE, STATE_UNKNOWN):
            return False
        try:
            value = float(state.state)
        except (TypeError, ValueError):
            return False
        if entity_id == self._current_sensor:
            if self._attr_current_temperature == value:
                return False
            self._attr_current_temperature = value
            return True
        if entity_id == self._target_sensor:
            if self._attr_target_temperature == value:
                return False
            self._attr_target_temperature = value
            return True
        return False

    def _sensor_target_value(self) -> float | None:
        if not self._target_sensor:
            return None
        state = self.hass.states.get(self._target_sensor)
        if state is None or state.state in (
            None,
            "",
            STATE_UNAVAILABLE,
            STATE_UNKNOWN,
        ):
            return None
        try:
            return float(state.state)
        except (TypeError, ValueError):
            return None

    async def async_set_temperature(self, **kwargs: Any) -> None:
        new_temp = kwargs.get(ATTR_TEMPERATURE)
        if new_temp is None:
            return

        new_temp = round(float(new_temp) * 2) / 2  # snap to 0.5 °C
        new_temp = max(self._attr_min_temp, min(self._attr_max_temp, new_temp))

        sensor_value = self._sensor_target_value()
        if sensor_value is not None and abs(sensor_value - new_temp) < 0.05:
            _LOGGER.debug(
                "%s: skip downlink, requested %.1f matches sensor %.1f",
                self.entity_id,
                new_temp,
                sensor_value,
            )
            self._attr_target_temperature = new_temp
            self.async_write_ha_state()
            return

        payload = build_target_temperature_payload(new_temp, self._tolerance)
        ok = await async_send_downlink(
            self.hass,
            self._entry,
            self._subentry.data,
            payload,
            label=self.entity_id,
        )
        if ok:
            self._attr_target_temperature = new_temp
            self.async_write_ha_state()
