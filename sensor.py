"""Read-only sensor entities (motor position, motor stroke) for WT101.

Each sensor mirrors data from one of two sources:
- TTN flow: the user picked an existing HA sensor entity in the subentry; this
  entity tracks state changes on that source and re-publishes the value under
  the WT101 device.
- ChirpStack flow: the per-hub webhook decodes the uplink and dispatches a
  field dict; this entity pulls its own field out of that dict.

Both sources are optional; the entity simply stays unknown until data arrives
on either path.
"""
from __future__ import annotations

from homeassistant.components.sensor import (
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry, ConfigSubentry
from homeassistant.const import (
    CONF_NAME,
    PERCENTAGE,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_state_change_event

from .const import (
    CONF_CS_DEV_EUI,
    CONF_MOTOR_POSITION_SENSOR,
    CONF_MOTOR_STROKE_SENSOR,
    CONF_PLATFORM_TYPE,
    CONF_TTN_DEVICE_ID,
    DOMAIN,
    PLATFORM_CHIRPSTACK,
    SUBENTRY_TYPE_THERMOSTAT,
    cs_uplink_signal,
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
            [
                Wt101MotorPositionSensor(entry, subentry),
                Wt101MotorStrokeSensor(entry, subentry),
            ],
            config_subentry_id=subentry.subentry_id,
        )

    for subentry in entry.subentries.values():
        _add(subentry)


class _Wt101MirrorSensor(SensorEntity):
    """Mirror a value from either a picked HA sensor or the webhook dict."""

    _attr_should_poll = False
    _attr_has_entity_name = False
    _attr_state_class = SensorStateClass.MEASUREMENT

    # Subclass overrides:
    _suffix: str = ""
    _unique_suffix: str = ""
    _source_conf_key: str = ""
    _uplink_field: str = ""

    def __init__(self, entry: ConfigEntry, subentry: ConfigSubentry) -> None:
        self._entry = entry
        self._subentry = subentry
        sub_data = subentry.data

        self._attr_name = f"{sub_data[CONF_NAME]} {self._suffix}"
        self._attr_unique_id = f"{subentry.subentry_id}_{self._unique_suffix}"
        self._attr_native_value = None

        self._source_entity: str | None = sub_data.get(self._source_conf_key)

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

        if self._source_entity:
            self._sync_from_state(self.hass.states.get(self._source_entity))
            self.async_on_remove(
                async_track_state_change_event(
                    self.hass,
                    [self._source_entity],
                    self._handle_source_change,
                )
            )

        if self._entry.data.get(CONF_PLATFORM_TYPE) == PLATFORM_CHIRPSTACK:
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
    def _handle_source_change(self, event: Event) -> None:
        if self._sync_from_state(event.data.get("new_state")):
            self.async_write_ha_state()

    def _sync_from_state(self, state) -> bool:
        if state is None:
            return False
        if state.state in (None, "", STATE_UNAVAILABLE, STATE_UNKNOWN):
            return False
        try:
            value = float(state.state)
        except (TypeError, ValueError):
            return False
        if self._attr_native_value == value:
            return False
        self._attr_native_value = value
        return True

    @callback
    def _handle_uplink(self, fields: dict[str, float]) -> None:
        value = fields.get(self._uplink_field)
        if value is None:
            return
        if self._attr_native_value == value:
            return
        self._attr_native_value = value
        self.async_write_ha_state()


class Wt101MotorPositionSensor(_Wt101MirrorSensor):
    _suffix = "motor position"
    _unique_suffix = "motor_position"
    _source_conf_key = CONF_MOTOR_POSITION_SENSOR
    _uplink_field = "motor_position"

    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_icon = "mdi:valve"


class Wt101MotorStrokeSensor(_Wt101MirrorSensor):
    _suffix = "motor stroke"
    _unique_suffix = "motor_stroke"
    _source_conf_key = CONF_MOTOR_STROKE_SENSOR
    _uplink_field = "motor_stroke"

    _attr_icon = "mdi:arrow-expand-vertical"
