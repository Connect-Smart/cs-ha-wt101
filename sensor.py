"""Read-only sensor entities (valve, battery and status reports).

Each sensor mirrors data from one of two sources:
- TTN flow: the user picked an existing HA sensor entity in the subentry; this
  entity tracks state changes on that source and re-publishes the value under
  the WT101 device.
- ChirpStack flow: the per-hub webhook decodes the uplink and dispatches a
  field dict; this entity pulls its own field out of that dict.

Both sources are optional; the entity simply stays unknown until data arrives
on either path.

Battery, calibration result, tamper, open window and freeze protection only
exist on the ChirpStack path; the codec emits the statuses as strings, which
are normalised to ENUM options here.
"""
from __future__ import annotations

import logging
import re

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry, ConfigSubentry
from homeassistant.const import (
    CONF_NAME,
    PERCENTAGE,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    EntityCategory,
)
from homeassistant.core import Event, HomeAssistant, State, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.util import dt as dt_util

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

_LOGGER = logging.getLogger(__name__)


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
        entities: list[SensorEntity] = [
            Wt101ValveOpeningSensor(entry, subentry),
            Wt101MotorPositionSensor(entry, subentry),
            Wt101MotorStrokeSensor(entry, subentry),
        ]
        if entry.data.get(CONF_PLATFORM_TYPE) == PLATFORM_CHIRPSTACK:
            # Only the webhook delivers these; on TTN they would stay unknown.
            entities += [
                Wt101BatterySensor(entry, subentry),
                Wt101WindowSensor(entry, subentry),
                Wt101FreezeProtectionSensor(entry, subentry),
                Wt101CalibrationResultSensor(entry, subentry),
                Wt101TamperStatusSensor(entry, subentry),
            ]
        async_add_entities(entities, config_subentry_id=subentry.subentry_id)

    for subentry in entry.subentries.values():
        _add(subentry)


def _device_info(subentry: ConfigSubentry) -> DeviceInfo:
    sub_data = subentry.data
    identifier = (
        sub_data.get(CONF_TTN_DEVICE_ID)
        or sub_data.get(CONF_CS_DEV_EUI)
        or subentry.subentry_id
    )
    return DeviceInfo(
        identifiers={(DOMAIN, identifier)},
        name=sub_data[CONF_NAME],
        manufacturer="Milesight",
        model="WT101",
    )


def _numeric_state(state: State | None) -> float | None:
    if state is None or state.state in (None, "", STATE_UNAVAILABLE, STATE_UNKNOWN):
        return None
    try:
        return float(state.state)
    except (TypeError, ValueError):
        return None


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
        self._attr_device_info = _device_info(subentry)

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
        value = _numeric_state(state)
        if value is None or self._attr_native_value == value:
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
    """Raw motor steps (0b 90): 0 = valve fully open, = stroke when closed."""

    _suffix = "motor position"
    _unique_suffix = "motor_position"
    _source_conf_key = CONF_MOTOR_POSITION_SENSOR
    _uplink_field = "motor_position"

    _attr_icon = "mdi:ray-vertex"
    _attr_entity_category = EntityCategory.DIAGNOSTIC


class Wt101MotorStrokeSensor(_Wt101MirrorSensor):
    _suffix = "motor stroke"
    _unique_suffix = "motor_stroke"
    _source_conf_key = CONF_MOTOR_STROKE_SENSOR
    _uplink_field = "motor_stroke"

    _attr_icon = "mdi:arrow-expand-vertical"
    _attr_entity_category = EntityCategory.DIAGNOSTIC


class Wt101BatterySensor(_Wt101MirrorSensor):
    _suffix = "battery"
    _unique_suffix = "battery"
    _uplink_field = "battery"

    _attr_device_class = SensorDeviceClass.BATTERY
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_entity_category = EntityCategory.DIAGNOSTIC


class Wt101ValveOpeningSensor(SensorEntity):
    """How far the valve is open, in %.

    The WT101 only reports this itself (05 92) in valve opening control
    mode. In automatic temperature control it is derived from the motor:
    position 0 is fully open and position == stroke is fully closed (user
    guide 6.2), so opening = (stroke - position) / stroke.
    """

    _attr_should_poll = False
    _attr_has_entity_name = False
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_icon = "mdi:valve"

    def __init__(self, entry: ConfigEntry, subentry: ConfigSubentry) -> None:
        self._entry = entry
        self._subentry = subentry
        sub_data = subentry.data
        self._attr_name = f"{sub_data[CONF_NAME]} valve opening"
        self._attr_unique_id = f"{subentry.subentry_id}_valve_opening"
        self._attr_native_value = None
        self._attr_device_info = _device_info(subentry)
        self._position_entity: str | None = sub_data.get(CONF_MOTOR_POSITION_SENSOR)
        self._stroke_entity: str | None = sub_data.get(CONF_MOTOR_STROKE_SENSOR)
        self._position: float | None = None
        self._stroke: float | None = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()

        tracked = [e for e in (self._position_entity, self._stroke_entity) if e]
        if tracked:
            if self._position_entity:
                self._position = _numeric_state(
                    self.hass.states.get(self._position_entity)
                )
            if self._stroke_entity:
                self._stroke = _numeric_state(
                    self.hass.states.get(self._stroke_entity)
                )
            self._attr_native_value = self._derive()
            self.async_on_remove(
                async_track_state_change_event(
                    self.hass, tracked, self._handle_source_change
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

    def _derive(self) -> float | None:
        if self._position is None or not self._stroke:
            return None  # stroke 0: motor not calibrated yet
        opening = (self._stroke - self._position) / self._stroke * 100
        return round(max(0.0, min(100.0, opening)))

    @callback
    def _handle_source_change(self, event: Event) -> None:
        value = _numeric_state(event.data.get("new_state"))
        if value is None:
            return
        if event.data.get("entity_id") == self._position_entity:
            self._position = value
        else:
            self._stroke = value
        self._update(self._derive())

    @callback
    def _handle_uplink(self, fields: dict[str, float | str]) -> None:
        position = fields.get("motor_position")
        stroke = fields.get("motor_stroke")
        reported = fields.get("valve_opening")
        if isinstance(position, float):
            self._position = position
        if isinstance(stroke, float):
            self._stroke = stroke
        self._update(reported if isinstance(reported, float) else self._derive())

    @callback
    def _update(self, value: float | None) -> None:
        if value is None or value == self._attr_native_value:
            return
        self._attr_native_value = value
        self.async_write_ha_state()


# User guide: 08 e5 XX → 00 success, 01 fail (out of range), 02 fail
# (uninstalled), 03 calibration cleared, 04 temperature control disabled.
CALIBRATION_SUCCESS = "success"
CALIBRATION_FAIL_OUT_OF_RANGE = "fail_out_of_range"
CALIBRATION_FAIL_UNINSTALLED = "fail_uninstalled"
CALIBRATION_CLEARED = "cleared"
CALIBRATION_TEMP_CONTROL_DISABLED = "temperature_control_disabled"
_CALIBRATION_CODES: dict[int, str] = {
    0: CALIBRATION_SUCCESS,
    1: CALIBRATION_FAIL_OUT_OF_RANGE,
    2: CALIBRATION_FAIL_UNINSTALLED,
    3: CALIBRATION_CLEARED,
    4: CALIBRATION_TEMP_CONTROL_DISABLED,
}

# 06 00 XX → 00 installed, 01 uninstalled.
TAMPER_INSTALLED = "installed"
TAMPER_UNINSTALLED = "uninstalled"
_TAMPER_CODES: dict[int, str] = {0: TAMPER_INSTALLED, 1: TAMPER_UNINSTALLED}

# 07 00 XX → 00 normal, 01 open.  0a 00 XX → 00 back to normal, 01 triggered.
STATUS_NORMAL = "normal"
WINDOW_OPEN = "open"
FREEZE_TRIGGERED = "triggered"
_WINDOW_CODES: dict[int, str] = {0: STATUS_NORMAL, 1: WINDOW_OPEN}
_FREEZE_CODES: dict[int, str] = {0: STATUS_NORMAL, 1: FREEZE_TRIGGERED}


def _as_code(raw: str) -> int | None:
    try:
        number = float(raw)
    except ValueError:
        return None
    return int(number) if number.is_integer() else None


def _slug(raw: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", raw.lower()).strip("_")


def _normalize_calibration(raw: str) -> str | None:
    """Map codec output ("fail: out of range", "1", ...) to an ENUM option."""
    code = _as_code(raw)
    if code is not None:
        return _CALIBRATION_CODES.get(code)
    slug = _slug(raw)
    if "success" in slug:
        return CALIBRATION_SUCCESS
    if "range" in slug:
        return CALIBRATION_FAIL_OUT_OF_RANGE
    if "uninstall" in slug or "not_install" in slug:
        return CALIBRATION_FAIL_UNINSTALLED
    if "clear" in slug:
        return CALIBRATION_CLEARED
    if "disable" in slug or "temperature_control" in slug:
        return CALIBRATION_TEMP_CONTROL_DISABLED
    return None


def _normalize_tamper(raw: str) -> str | None:
    """Map codec output ("installed", "uninstall", "1", ...) to an option."""
    code = _as_code(raw)
    if code is not None:
        return _TAMPER_CODES.get(code)
    slug = _slug(raw)
    if "uninstall" in slug or "not_install" in slug or "removed" in slug:
        return TAMPER_UNINSTALLED
    if "install" in slug:
        return TAMPER_INSTALLED
    return None


def _normalize_flag(raw: str, codes: dict[int, str]) -> str | None:
    """Map a 0/1 status ("normal", "open", "1", ...) to one of ``codes``."""
    code = _as_code(raw)
    if code is not None:
        return codes.get(code)
    slug = _slug(raw)
    return slug if slug in codes.values() else None


class _Wt101UplinkEnumSensor(SensorEntity, RestoreEntity):
    """ENUM status pushed by the ChirpStack webhook, restored on restart.

    These packets are rare (calibration, tamper events), so the last known
    value is restored rather than dropping back to unknown after a restart.
    """

    _attr_should_poll = False
    _attr_has_entity_name = False
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    # Subclass overrides:
    _suffix: str = ""
    _unique_suffix: str = ""
    _uplink_field: str = ""

    def __init__(self, entry: ConfigEntry, subentry: ConfigSubentry) -> None:
        self._entry = entry
        self._subentry = subentry
        self._attr_name = f"{subentry.data[CONF_NAME]} {self._suffix}"
        self._attr_unique_id = f"{subentry.subentry_id}_{self._unique_suffix}"
        # Explicit name above wins; the key only translates the ENUM states.
        self._attr_translation_key = self._unique_suffix
        self._attr_native_value = None
        self._raw_value: str | None = None
        # Set on every report, so a repeated identical result (e.g. a second
        # "fail: out of range") is still visible as a new update.
        self._reported_at: str | None = None
        self._attr_device_info = _device_info(subentry)

    def _normalize(self, raw: str) -> str | None:
        raise NotImplementedError

    @property
    def extra_state_attributes(self) -> dict[str, str] | None:
        if self._raw_value is None:
            return None
        return {"raw_value": self._raw_value, "reported_at": self._reported_at}

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()

        last = await self.async_get_last_state()
        if last is not None and last.state in (self.options or []):
            self._attr_native_value = last.state
            self._raw_value = last.attributes.get("raw_value")
            self._reported_at = last.attributes.get("reported_at")

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
    def _handle_uplink(self, fields: dict[str, float | str]) -> None:
        raw = fields.get(self._uplink_field)
        if raw is None:
            return
        raw = str(raw)
        value = self._normalize(raw)
        if value is None:
            _LOGGER.warning(
                "%s: unrecognised %s value %r from the ChirpStack codec",
                self.entity_id,
                self._uplink_field,
                raw,
            )
            return
        self._attr_native_value = value
        self._raw_value = raw
        self._reported_at = dt_util.utcnow().isoformat()
        self.async_write_ha_state()


class Wt101CalibrationResultSensor(_Wt101UplinkEnumSensor):
    _suffix = "calibration result"
    _unique_suffix = "calibration_result"
    _uplink_field = "motor_calibration_result"

    _attr_options = list(_CALIBRATION_CODES.values())

    def _normalize(self, raw: str) -> str | None:
        return _normalize_calibration(raw)

    @property
    def icon(self) -> str:
        if self._attr_native_value == CALIBRATION_SUCCESS:
            return "mdi:check-circle-outline"
        if self._attr_native_value in (
            CALIBRATION_FAIL_OUT_OF_RANGE,
            CALIBRATION_FAIL_UNINSTALLED,
        ):
            return "mdi:alert-circle-outline"
        return "mdi:tune-vertical"


class Wt101TamperStatusSensor(_Wt101UplinkEnumSensor):
    _suffix = "tamper status"
    _unique_suffix = "tamper_status"
    _uplink_field = "tamper_status"

    _attr_options = list(_TAMPER_CODES.values())

    def _normalize(self, raw: str) -> str | None:
        return _normalize_tamper(raw)

    @property
    def icon(self) -> str:
        if self._attr_native_value == TAMPER_UNINSTALLED:
            return "mdi:lock-open-alert-outline"
        return "mdi:lock-outline"


class Wt101WindowSensor(_Wt101UplinkEnumSensor):
    """Open window detection: the WT101 pauses heating while ``open``."""

    _suffix = "open window"
    _unique_suffix = "window_detection"
    _uplink_field = "window_detection"

    _attr_options = list(_WINDOW_CODES.values())
    _attr_entity_category = None

    def _normalize(self, raw: str) -> str | None:
        return _normalize_flag(raw, _WINDOW_CODES)

    @property
    def icon(self) -> str:
        if self._attr_native_value == WINDOW_OPEN:
            return "mdi:window-open-variant"
        return "mdi:window-closed-variant"


class Wt101FreezeProtectionSensor(_Wt101UplinkEnumSensor):
    _suffix = "freeze protection"
    _unique_suffix = "freeze_protection"
    _uplink_field = "freeze_protection"

    _attr_options = list(_FREEZE_CODES.values())
    _attr_entity_category = None

    def _normalize(self, raw: str) -> str | None:
        return _normalize_flag(raw, _FREEZE_CODES)

    @property
    def icon(self) -> str:
        if self._attr_native_value == FREEZE_TRIGGERED:
            return "mdi:snowflake-alert"
        return "mdi:snowflake"
