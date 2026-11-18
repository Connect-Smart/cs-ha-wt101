"""Switch entities for Milesight WT101 thermostats."""
from __future__ import annotations

from datetime import datetime, timedelta
import logging
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry, ConfigSubentry
from homeassistant.const import (
    ATTR_UNIT_OF_MEASUREMENT,
    CONF_NAME,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    UnitOfTemperature,
)
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_conversion import TemperatureConverter

from .calibration import (
    STATE_DONE,
    STATE_STARTED,
    calibration_signal,
    device_key,
    is_calibrating,
    raise_if_calibrating,
)
from .const import (
    CONF_CS_DEV_EUI,
    CONF_EXTERNAL_OFFLINE_MODE,
    CONF_EXTERNAL_SEND_INTERVAL,
    CONF_EXTERNAL_TEMP_SENSOR,
    CONF_EXTERNAL_TIMEOUT,
    CONF_FREEZE_THRESHOLD,
    CONF_PLATFORM_TYPE,
    CONF_TTN_DEVICE_ID,
    CONF_WINDOW_COOLING_RATE,
    CONF_WINDOW_STOP_TIME,
    CONF_WINDOW_VALVE_STRATEGY,
    DEFAULT_EXTERNAL_OFFLINE_MODE,
    DEFAULT_EXTERNAL_SEND_INTERVAL,
    DEFAULT_EXTERNAL_TIMEOUT,
    DEFAULT_FREEZE_THRESHOLD,
    DEFAULT_WINDOW_COOLING_RATE,
    DEFAULT_WINDOW_STOP_TIME,
    DEFAULT_WINDOW_VALVE_STRATEGY,
    DOMAIN,
    EXTERNAL_OFFLINE_MODES,
    PLATFORM_CHIRPSTACK,
    SUBENTRY_TYPE_THERMOSTAT,
    WINDOW_VALVE_STRATEGIES,
    cs_uplink_signal,
)
from .lorawan import (
    async_send_calibration_trigger,
    async_send_downlink,
    build_child_lock_payload,
    build_external_offline_mode_payload,
    build_external_sensor_mode_payload,
    build_external_temperature_payload,
    build_freeze_protection_payload,
    build_open_window_payload,
    build_temperature_control_enable_payload,
)

_LOGGER = logging.getLogger(__name__)

# Skip an uplink-triggered push if we already queued one this recently.
_UPLINK_DEDUPE = timedelta(seconds=60)
# A setting queued this recently before a calibration may still have been
# waiting in the queue the calibration flushed; it is sent again afterwards.
_RESEND_WINDOW = timedelta(minutes=30)


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
                Wt101TemperatureControlSwitch(entry, subentry),
                Wt101ChildLockSwitch(entry, subentry),
                Wt101OpenWindowSwitch(entry, subentry),
                Wt101FreezeProtectionSwitch(entry, subentry),
                Wt101ExternalSensorSwitch(entry, subentry),
            ],
            config_subentry_id=subentry.subentry_id,
        )

    for subentry in entry.subentries.values():
        _add(subentry)


class _Wt101SettingSwitch(SwitchEntity, RestoreEntity):
    """One on/off device setting, written with a single downlink.

    The WT101 does not report these settings back, so the state is the last
    value HA queued successfully (restored across restarts). Settings that
    come with parameters from the thermostat form are shown as attributes
    and sent again when they are changed while the switch is on.
    """

    _attr_should_poll = False
    _attr_has_entity_name = False

    # Subclass overrides:
    _suffix: str = ""
    _unique_suffix: str = ""
    _default_on: bool = False
    # Re-send after a calibration flushed the queue. Off where re-sending
    # has side effects (enabling temperature control starts a calibration).
    _resend_after_calibration: bool = True

    def __init__(self, entry: ConfigEntry, subentry: ConfigSubentry) -> None:
        self._entry = entry
        self._subentry = subentry
        sub_data = subentry.data

        self._attr_name = f"{sub_data[CONF_NAME]} {self._suffix}"
        self._attr_unique_id = f"{subentry.subentry_id}_{self._unique_suffix}"
        self._attr_is_on = self._default_on
        self._sent_settings: dict[str, Any] = {}
        self._sent_at: datetime | None = None
        self._resend_pending = False

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

    def _settings(self) -> dict[str, Any]:
        """Parameters sent along with ``on`` (from the thermostat form)."""
        return {}

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        return self._sent_settings or None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is not None and last.state in ("on", "off"):
            self._attr_is_on = last.state == "on"
            self._sent_settings = {
                key: last.attributes[key]
                for key in self._settings()
                if key in last.attributes
            }
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                calibration_signal(device_key(self._subentry.data)),
                self._handle_calibration,
            )
        )
        if self._attr_is_on and self._sent_settings != self._settings():
            # Reconfigured while on: push the new parameters.
            self.hass.async_create_task(self._async_resend())

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._set(False)

    async def _set(self, enable: bool) -> None:
        raise_if_calibrating(self.hass, self._subentry.data)
        if await self._async_send(enable):
            self._record_sent(enable)

    async def _async_resend(self) -> None:
        """Send the current state again without user interaction."""
        if is_calibrating(self.hass, self._subentry.data):
            self._resend_pending = True  # sent once the calibration ends
            return
        if await self._async_send(self._attr_is_on):
            self._record_sent(self._attr_is_on)

    @callback
    def _record_sent(self, enable: bool) -> None:
        self._attr_is_on = enable
        self._sent_settings = self._settings()
        self._sent_at = dt_util.utcnow()
        self.async_write_ha_state()

    @callback
    def _handle_calibration(self, event: dict[str, Any]) -> None:
        if event["state"] == STATE_STARTED:
            if (
                self._resend_after_calibration
                and self._sent_at is not None
                and dt_util.utcnow() - self._sent_at < _RESEND_WINDOW
            ):
                self._resend_pending = True
        elif event["state"] == STATE_DONE and self._resend_pending:
            self._resend_pending = False
            self.hass.async_create_task(self._async_resend())

    async def _async_send(self, enable: bool) -> bool:
        raise NotImplementedError

    async def _async_downlink(self, payload: bytes) -> bool:
        return await async_send_downlink(
            self.hass,
            self._entry,
            self._subentry.data,
            payload,
            label=self.entity_id,
        )


class Wt101TemperatureControlSwitch(_Wt101SettingSwitch):
    """Enable or disable the WT101's internal temperature-control loop."""

    _suffix = "temperature control"
    _unique_suffix = "temp_control"
    _default_on = True
    _resend_after_calibration = False
    _attr_icon = "mdi:thermostat-cog"

    async def _async_send(self, enable: bool) -> bool:
        payload = build_temperature_control_enable_payload(enable)
        if enable and not self._attr_is_on:
            # Enabling starts one motor calibration (user guide 5.3): hold
            # the other downlinks exactly like the calibrate button does.
            return await async_send_calibration_trigger(
                self.hass,
                self._entry,
                self._subentry.data,
                payload,
                label=self.entity_id,
            )
        return await self._async_downlink(payload)


class Wt101ChildLockSwitch(_Wt101SettingSwitch):
    """Lock the knob so the target can only be changed remotely.

    While locked, turning the knob on the device does nothing; HA, the
    setpoint source and the ToolBox app can still change the target.
    """

    _suffix = "child lock"
    _unique_suffix = "child_lock"
    _default_on = False  # factory default: unlocked
    _attr_icon = "mdi:lock"

    async def _async_send(self, enable: bool) -> bool:
        return await self._async_downlink(build_child_lock_payload(enable))


class Wt101OpenWindowSwitch(_Wt101SettingSwitch):
    """Open window detection (ff af + f9 37).

    When the temperature drops faster than the cooling rate the WT101 stops
    regulating and reports ``open`` (see the open window sensor). It resumes
    when the temperature rises for 10 minutes, reaches the target, or after
    the stop time.
    """

    _suffix = "open window detection"
    _unique_suffix = "open_window_detection"
    _attr_icon = "mdi:window-open-variant"

    def _settings(self) -> dict[str, Any]:
        sub_data = self._subentry.data
        return {
            CONF_WINDOW_COOLING_RATE: float(
                sub_data.get(CONF_WINDOW_COOLING_RATE, DEFAULT_WINDOW_COOLING_RATE)
            ),
            CONF_WINDOW_STOP_TIME: int(
                sub_data.get(CONF_WINDOW_STOP_TIME, DEFAULT_WINDOW_STOP_TIME)
            ),
            CONF_WINDOW_VALVE_STRATEGY: sub_data.get(
                CONF_WINDOW_VALVE_STRATEGY, DEFAULT_WINDOW_VALVE_STRATEGY
            ),
        }

    async def _async_send(self, enable: bool) -> bool:
        settings = self._settings()
        return await self._async_downlink(
            build_open_window_payload(
                enable,
                settings[CONF_WINDOW_COOLING_RATE],
                settings[CONF_WINDOW_STOP_TIME],
                WINDOW_VALVE_STRATEGIES.get(settings[CONF_WINDOW_VALVE_STRATEGY], 0),
            )
        )


class Wt101FreezeProtectionSwitch(_Wt101SettingSwitch):
    """Freeze protection (ff b0).

    Below the threshold the WT101 opens the valve to heat back up to the
    threshold (± tolerance), checked every 10 minutes.
    """

    _suffix = "freeze protection"
    _unique_suffix = "freeze_protection"
    _attr_icon = "mdi:snowflake-thermometer"

    def _settings(self) -> dict[str, Any]:
        return {
            CONF_FREEZE_THRESHOLD: float(
                self._subentry.data.get(
                    CONF_FREEZE_THRESHOLD, DEFAULT_FREEZE_THRESHOLD
                )
            )
        }

    async def _async_send(self, enable: bool) -> bool:
        return await self._async_downlink(
            build_freeze_protection_payload(
                enable, self._settings()[CONF_FREEZE_THRESHOLD]
            )
        )


class Wt101ExternalSensorSwitch(SwitchEntity, RestoreEntity):
    """Make the WT101 regulate on a temperature from another HA sensor.

    Turning this on sends ``ff f8`` (offline fallback) + ``ff c4`` (enable
    external mode) and from then on pushes the picked sensor's value as a
    ``03`` downlink. The WT101 is Class A, so each queued downlink is only
    delivered on its next uplink; pushes are therefore paced to roughly one
    per uplink to keep the LNS queue from growing:

    - ChirpStack with the webhook: one push per received uplink.
    - Otherwise (TTN, or no uplinks seen): one push per send interval.

    Turning it off sends ``ff c4 00`` so the built-in sensor takes over again.
    """

    _attr_should_poll = False
    _attr_has_entity_name = False
    _attr_icon = "mdi:thermometer-lines"

    def __init__(self, entry: ConfigEntry, subentry: ConfigSubentry) -> None:
        self._entry = entry
        self._subentry = subentry
        sub_data = subentry.data

        self._attr_name = f"{sub_data[CONF_NAME]} external sensor mode"
        self._attr_unique_id = f"{subentry.subentry_id}_external_sensor_mode"
        self._attr_is_on = False

        self._source: str | None = sub_data.get(CONF_EXTERNAL_TEMP_SENSOR)
        self._timeout = int(
            sub_data.get(CONF_EXTERNAL_TIMEOUT, DEFAULT_EXTERNAL_TIMEOUT)
        )
        self._interval = timedelta(
            minutes=int(
                sub_data.get(
                    CONF_EXTERNAL_SEND_INTERVAL, DEFAULT_EXTERNAL_SEND_INTERVAL
                )
            )
        )
        self._offline_mode = EXTERNAL_OFFLINE_MODES.get(
            sub_data.get(CONF_EXTERNAL_OFFLINE_MODE, DEFAULT_EXTERNAL_OFFLINE_MODE),
            EXTERNAL_OFFLINE_MODES[DEFAULT_EXTERNAL_OFFLINE_MODE],
        )

        self._last_sent_at: datetime | None = None
        self._last_sent_value: float | None = None
        self._sending = False
        self._unsubs: list[CALLBACK_TYPE] = []

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
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "external_sensor": self._source,
            "last_sent_temperature": self._last_sent_value,
            # After a restart _last_sent_at only paces the timer; show it
            # once a reading has actually been sent.
            "last_sent_at": (
                self._last_sent_at.isoformat()
                if self._last_sent_at and self._last_sent_value is not None
                else None
            ),
            "calibrating": is_calibrating(self.hass, self._subentry.data),
        }

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is not None and last.state == "on" and self._source:
            # The device keeps the mode across HA restarts; just resume pushing.
            # Count the restart as a send so the interval timer does not
            # queue an extra downlink on top of the uplink-driven one.
            self._attr_is_on = True
            self._last_sent_at = dt_util.utcnow()
            self._start_pushing()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                calibration_signal(device_key(self._subentry.data)),
                self._handle_calibration,
            )
        )

    async def async_will_remove_from_hass(self) -> None:
        self._stop_pushing()
        await super().async_will_remove_from_hass()

    async def async_turn_on(self, **kwargs: Any) -> None:
        raise_if_calibrating(self.hass, self._subentry.data)
        await self._async_enable()

    async def _async_enable(self) -> None:
        """Send fallback + enable + first reading in one downlink."""
        if not self._source:
            raise HomeAssistantError(
                "No external temperature sensor configured for this thermostat; "
                "pick one via the thermostat's Reconfigure option first"
            )
        payload = build_external_offline_mode_payload(
            self._offline_mode
        ) + build_external_sensor_mode_payload(True, self._timeout)
        # Piggyback the first reading so the device has a value right away.
        value = self._read_source()
        if value is not None:
            payload += build_external_temperature_payload(value)

        ok = await async_send_downlink(
            self.hass,
            self._entry,
            self._subentry.data,
            payload,
            label=self.entity_id,
        )
        if not ok:
            return
        if value is not None:
            self._record_sent(value)
        self._attr_is_on = True
        self._start_pushing()
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        raise_if_calibrating(self.hass, self._subentry.data)
        ok = await async_send_downlink(
            self.hass,
            self._entry,
            self._subentry.data,
            build_external_sensor_mode_payload(False, self._timeout),
            label=self.entity_id,
        )
        if not ok:
            return
        self._attr_is_on = False
        self._stop_pushing()
        self.async_write_ha_state()

    @callback
    def _handle_calibration(self, event: dict[str, Any]) -> None:
        """Re-send the full mode after a calibration flushed the queue."""
        if event["state"] == STATE_DONE and self._attr_is_on:
            self.hass.async_create_task(self._async_enable())
        self.async_write_ha_state()

    @callback
    def _start_pushing(self) -> None:
        self._stop_pushing()
        # Fine-grained tick; _async_tick decides whether the interval elapsed.
        self._unsubs.append(
            async_track_time_interval(
                self.hass, self._async_tick, timedelta(minutes=1)
            )
        )
        if self._entry.data.get(CONF_PLATFORM_TYPE) == PLATFORM_CHIRPSTACK:
            dev_eui = self._subentry.data.get(CONF_CS_DEV_EUI)
            if dev_eui:
                self._unsubs.append(
                    async_dispatcher_connect(
                        self.hass,
                        cs_uplink_signal(self._entry.entry_id, dev_eui),
                        self._handle_uplink,
                    )
                )

    @callback
    def _stop_pushing(self) -> None:
        while self._unsubs:
            self._unsubs.pop()()

    @callback
    def _handle_uplink(self, fields: dict[str, float]) -> None:
        """Queue a fresh reading; ChirpStack delivers it on the next uplink."""
        if self._sent_within(_UPLINK_DEDUPE):
            return
        self.hass.async_create_task(self._async_push())

    async def _async_tick(self, now: datetime) -> None:
        if self._sent_within(self._interval):
            return
        await self._async_push()

    def _sent_within(self, window: timedelta) -> bool:
        return (
            self._last_sent_at is not None
            and dt_util.utcnow() - self._last_sent_at < window
        )

    async def _async_push(self) -> None:
        if not self._attr_is_on or self._sending:
            return
        if is_calibrating(self.hass, self._subentry.data):
            return
        value = self._read_source()
        if value is None:
            _LOGGER.warning(
                "%s: external sensor %s has no usable value; the WT101 falls "
                "back to its offline mode after %d min without readings",
                self.entity_id,
                self._source,
                self._timeout,
            )
            return
        self._sending = True
        try:
            ok = await async_send_downlink(
                self.hass,
                self._entry,
                self._subentry.data,
                build_external_temperature_payload(value),
                label=self.entity_id,
            )
        finally:
            self._sending = False
        if ok:
            self._record_sent(value)
            self.async_write_ha_state()

    @callback
    def _record_sent(self, value: float) -> None:
        self._last_sent_at = dt_util.utcnow()
        self._last_sent_value = value

    def _read_source(self) -> float | None:
        """Current external reading in °C, or None when unavailable."""
        if not self._source:
            return None
        state = self.hass.states.get(self._source)
        if state is None or state.state in (
            None,
            "",
            STATE_UNAVAILABLE,
            STATE_UNKNOWN,
        ):
            return None
        try:
            value = float(state.state)
        except (TypeError, ValueError):
            return None
        unit = state.attributes.get(ATTR_UNIT_OF_MEASUREMENT)
        if (
            unit
            and unit != UnitOfTemperature.CELSIUS
            and unit in TemperatureConverter.VALID_UNITS
        ):
            value = TemperatureConverter.convert(
                value, unit, UnitOfTemperature.CELSIUS
            )
        return round(value, 1)
