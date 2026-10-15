"""Climate entity for the Milesight WT101 LoRaWAN thermostat.

One config entry == one LoRaWAN application (hub). Each thermostat lives as a
ConfigSubentry under that hub, so all thermostats in the same TTN/ChirpStack
application share one set of credentials.

The target temperature is arbitrated by ``SetpointArbiter`` so HA, an optional
setpoint source entity and the knob on the device do not ping-pong over the
slow LoRaWAN round-trip.
"""
from __future__ import annotations

from datetime import datetime, timedelta
import logging
from typing import Any

from homeassistant.components.climate import (
    ClimateEntity,
    ClimateEntityFeature,
    HVACMode,
)
from homeassistant.config_entries import ConfigEntry, ConfigSubentry
from homeassistant.const import (
    ATTR_ENTITY_ID,
    ATTR_TEMPERATURE,
    CONF_NAME,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    UnitOfTemperature,
)
from homeassistant.core import Event, HomeAssistant, callback, split_entity_id
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_time_interval,
)
from homeassistant.util import dt as dt_util

from .const import (
    CONF_CS_DEV_EUI,
    CONF_CURRENT_TEMP_SENSOR,
    CONF_MAX_TEMP,
    CONF_MIN_TEMP,
    CONF_PLATFORM_TYPE,
    CONF_SETPOINT_GRACE,
    CONF_SETPOINT_PRIORITY,
    CONF_SETPOINT_SOURCE,
    CONF_TARGET_TEMP_SENSOR,
    CONF_TOLERANCE,
    CONF_TTN_DEVICE_ID,
    DEFAULT_MAX_TEMP,
    DEFAULT_MIN_TEMP,
    DEFAULT_SETPOINT_GRACE,
    DEFAULT_SETPOINT_PRIORITY,
    DEFAULT_TOLERANCE,
    DOMAIN,
    PLATFORM_CHIRPSTACK,
    PRIORITY_SOURCE,
    SUBENTRY_TYPE_THERMOSTAT,
    WRITABLE_SETPOINT_DOMAINS,
    cs_uplink_signal,
)
from .lorawan import async_send_downlink, build_target_temperature_payload
from .setpoint import NO_ACTION, Action, SetpointArbiter

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


def _numeric_state(state) -> float | None:
    if state is None or state.state in (None, "", STATE_UNAVAILABLE, STATE_UNKNOWN):
        return None
    try:
        return float(state.state)
    except (TypeError, ValueError):
        return None


def _setpoint_reading(state) -> float | None:
    """Value of a setpoint source: a climate's target, else the numeric state."""
    if state is None or state.domain != "climate":
        return _numeric_state(state)
    if state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
        return None
    try:
        return float(state.attributes[ATTR_TEMPERATURE])
    except (KeyError, TypeError, ValueError):
        # e.g. hvac_mode off or a heat_cool thermostat without one target
        return None


class Wt101ClimateEntity(ClimateEntity):
    """One climate entity bound to one WT101 thermostat (one subentry)."""

    _attr_hvac_modes = [HVACMode.HEAT]
    _attr_hvac_mode = HVACMode.HEAT
    _attr_supported_features = ClimateEntityFeature.TARGET_TEMPERATURE
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    # The WT101 stores its target as whole degrees (INT8, ff b1).
    _attr_target_temperature_step = 1.0
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
        self._setpoint_source: str | None = sub_data.get(CONF_SETPOINT_SOURCE)

        self._attr_min_temp = float(sub_data.get(CONF_MIN_TEMP, DEFAULT_MIN_TEMP))
        self._attr_max_temp = float(sub_data.get(CONF_MAX_TEMP, DEFAULT_MAX_TEMP))
        self._tolerance = float(sub_data.get(CONF_TOLERANCE, DEFAULT_TOLERANCE))

        self._attr_current_temperature: float | None = None
        self._arbiter = SetpointArbiter(
            sub_data.get(CONF_SETPOINT_PRIORITY, DEFAULT_SETPOINT_PRIORITY),
            timedelta(
                minutes=int(sub_data.get(CONF_SETPOINT_GRACE, DEFAULT_SETPOINT_GRACE))
            ),
        )

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

    @property
    def target_temperature(self) -> float | None:
        return self._arbiter.target

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "setpoint_source": self._setpoint_source,
            "setpoint_priority": self._arbiter.priority,
            "device_target_temperature": self._arbiter.reported,
            "pending_target_temperature": self._arbiter.pending,
        }

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

        if self._setpoint_source == self.entity_id:
            _LOGGER.warning(
                "%s: setpoint source points at itself; ignoring it", self.entity_id
            )
            self._setpoint_source = None
        if self._setpoint_source:
            self.async_on_remove(
                async_track_state_change_event(
                    self.hass,
                    [self._setpoint_source],
                    self._handle_setpoint_source_change,
                )
            )

        # Resolves pending targets when uplinks don't (lost downlink, TTN).
        self.async_on_remove(
            async_track_time_interval(
                self.hass, self._async_tick, timedelta(minutes=1)
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

    # ------------------------------------------------------------------
    # Inputs
    # ------------------------------------------------------------------
    @callback
    def _handle_uplink(self, fields: dict[str, float]) -> None:
        """Apply temperatures pushed in by the ChirpStack webhook."""
        self._arbiter.on_uplink()
        current = fields.get("current_temperature")
        target = fields.get("target_temperature")
        if current is not None:
            self._attr_current_temperature = current
        if target is not None:
            self._report_target(target)
        self.async_write_ha_state()

    @callback
    def _handle_source_change(self, event: Event) -> None:
        entity_id = event.data.get("entity_id")
        new_state = event.data.get("new_state")
        if self._sync_from_state(entity_id, new_state):
            self.async_write_ha_state()

    def _sync_from_state(self, entity_id: str | None, state) -> bool:
        value = _numeric_state(state)
        if entity_id is None or value is None:
            return False
        if entity_id == self._current_sensor:
            if self._attr_current_temperature == value:
                return False
            self._attr_current_temperature = value
            return True
        if entity_id == self._target_sensor:
            self._report_target(value)
            return True
        return False

    @callback
    def _report_target(self, value: float) -> None:
        action = self._arbiter.on_report(value, dt_util.utcnow(), self._source_value())
        self._schedule(action)

    @callback
    def _handle_setpoint_source_change(self, event: Event) -> None:
        value = _setpoint_reading(event.data.get("new_state"))
        if value is None:
            return
        self._schedule(self._arbiter.request(self._clamp(value)))
        self.async_write_ha_state()

    async def _async_tick(self, now: datetime) -> None:
        action = self._arbiter.on_tick(dt_util.utcnow(), self._source_value())
        if action != NO_ACTION:
            await self._async_apply(action)

    async def async_set_temperature(self, **kwargs: Any) -> None:
        new_temp = kwargs.get(ATTR_TEMPERATURE)
        if new_temp is None:
            return
        new_temp = self._clamp(round(float(new_temp)))

        if self._setpoint_source:
            domain = split_entity_id(self._setpoint_source)[0]
            if domain in WRITABLE_SETPOINT_DOMAINS:
                # The source listener turns this into a downlink, so the
                # source and the device can never drift apart via the UI.
                await self._async_write_source(new_temp)
                return
            if self._arbiter.priority == PRIORITY_SOURCE:
                raise HomeAssistantError(
                    f"The target of {self.name} is controlled by "
                    f"{self._setpoint_source}; change that entity instead"
                )

        await self._async_apply(self._arbiter.request(new_temp))

    # ------------------------------------------------------------------
    # Outputs
    # ------------------------------------------------------------------
    @callback
    def _schedule(self, action: Action) -> None:
        if action != NO_ACTION:
            self.hass.async_create_task(self._async_apply(action))

    async def _async_apply(self, action: Action) -> None:
        if action.gave_up is not None:
            _LOGGER.warning(
                "%s: WT101 did not confirm target %.0f °C after retries; "
                "keeping the device value %s",
                self.entity_id,
                action.gave_up,
                self._arbiter.reported,
            )
        if action.send is not None:
            payload = build_target_temperature_payload(action.send, self._tolerance)
            ok = await async_send_downlink(
                self.hass,
                self._entry,
                self._subentry.data,
                payload,
                label=self.entity_id,
            )
            if ok:
                self._arbiter.mark_sent(
                    action.send, dt_util.utcnow(), retry=action.retry
                )
        if action.write_source is not None:
            await self._async_write_source(action.write_source)
        self.async_write_ha_state()

    async def _async_write_source(self, value: float) -> None:
        if not self._setpoint_source:
            return
        domain = split_entity_id(self._setpoint_source)[0]
        if domain not in WRITABLE_SETPOINT_DOMAINS:
            return
        if domain == "climate":
            service, data = "set_temperature", {ATTR_TEMPERATURE: value}
        else:
            service, data = "set_value", {"value": value}
        await self.hass.services.async_call(
            domain,
            service,
            {ATTR_ENTITY_ID: self._setpoint_source, **data},
            blocking=True,
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _clamp(self, value: float) -> float:
        return max(self._attr_min_temp, min(self._attr_max_temp, value))

    def _source_value(self) -> float | None:
        if not self._setpoint_source:
            return None
        value = _setpoint_reading(self.hass.states.get(self._setpoint_source))
        return None if value is None else self._clamp(value)
