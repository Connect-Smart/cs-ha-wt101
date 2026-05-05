"""Climate entity for the Milesight WT101 LoRaWAN thermostat.

One config entry == one LoRaWAN application (hub). Each thermostat lives as a
ConfigSubentry under that hub, so all thermostats in the same TTN/ChirpStack
application share one set of credentials.
"""
from __future__ import annotations

import asyncio
import base64
import logging
import struct
from typing import Any

import aiohttp

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
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_state_change_event

from .const import (
    CONF_CS_API_TOKEN,
    CONF_CS_BASE_URL,
    CONF_CS_DEV_EUI,
    CONF_CURRENT_TEMP_SENSOR,
    CONF_FPORT,
    CONF_MAX_TEMP,
    CONF_MIN_TEMP,
    CONF_PLATFORM_TYPE,
    CONF_TARGET_TEMP_SENSOR,
    CONF_TOLERANCE,
    CONF_TTN_API_KEY,
    CONF_TTN_APPLICATION_ID,
    CONF_TTN_BASE_URL,
    CONF_TTN_DEVICE_ID,
    DEFAULT_FPORT,
    DEFAULT_MAX_TEMP,
    DEFAULT_MIN_TEMP,
    DEFAULT_TOLERANCE,
    DOMAIN,
    PLATFORM_CHIRPSTACK,
    PLATFORM_TTN,
    SUBENTRY_TYPE_THERMOSTAT,
)

_LOGGER = logging.getLogger(__name__)
_HTTP_TIMEOUT = aiohttp.ClientTimeout(total=10)

# PDF section 6.3.1: target temperature command header.
_CMD_TARGET_TEMPERATURE = b"\xff\xb1"


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


def build_target_temperature_payload(
    target_c: float, tolerance_c: float
) -> bytes:
    """Encode the ff b1 target-temperature command.

    PDF 6.3.1: byte 1 is INT8 °C, bytes 2–3 are UINT16/10 °C tolerance, little-endian.
    """
    temp_byte = struct.pack("b", int(round(target_c)))
    tolerance_units = max(0, min(0xFFFF, int(round(tolerance_c * 10))))
    tolerance_bytes = struct.pack("<H", tolerance_units)
    return _CMD_TARGET_TEMPERATURE + temp_byte + tolerance_bytes


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

        self._current_sensor: str = sub_data[CONF_CURRENT_TEMP_SENSOR]
        self._target_sensor: str = sub_data[CONF_TARGET_TEMP_SENSOR]

        self._attr_min_temp = float(sub_data.get(CONF_MIN_TEMP, DEFAULT_MIN_TEMP))
        self._attr_max_temp = float(sub_data.get(CONF_MAX_TEMP, DEFAULT_MAX_TEMP))
        self._tolerance = float(sub_data.get(CONF_TOLERANCE, DEFAULT_TOLERANCE))
        self._fport = int(sub_data.get(CONF_FPORT, DEFAULT_FPORT))

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
        self._sync_from_state(
            self._current_sensor, self.hass.states.get(self._current_sensor)
        )
        self._sync_from_state(
            self._target_sensor, self.hass.states.get(self._target_sensor)
        )
        self.async_on_remove(
            async_track_state_change_event(
                self.hass,
                [self._current_sensor, self._target_sensor],
                self._handle_source_change,
            )
        )

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
        if await self._send_downlink(payload):
            self._attr_target_temperature = new_temp
            self.async_write_ha_state()

    # ----------------------------------------------------------- downlinks
    async def _send_downlink(self, payload: bytes) -> bool:
        b64 = base64.b64encode(payload).decode("ascii")
        _LOGGER.debug(
            "%s: sending downlink hex=%s base64=%s fport=%d",
            self.entity_id,
            payload.hex(),
            b64,
            self._fport,
        )
        if self._platform == PLATFORM_TTN:
            return await self._send_ttn(b64)
        if self._platform == PLATFORM_CHIRPSTACK:
            return await self._send_chirpstack(b64)
        _LOGGER.error("%s: unknown platform %s", self.entity_id, self._platform)
        return False

    async def _send_ttn(self, frm_payload_b64: str) -> bool:
        hub = self._entry.data
        sub = self._subentry.data
        base_url = str(hub[CONF_TTN_BASE_URL]).rstrip("/")
        app_id = hub[CONF_TTN_APPLICATION_ID]
        device_id = sub[CONF_TTN_DEVICE_ID]
        api_key = hub[CONF_TTN_API_KEY]

        url = (
            f"{base_url}/api/v3/as/applications/{app_id}"
            f"/devices/{device_id}/down/push"
        )
        body = {
            "downlinks": [
                {
                    "f_port": self._fport,
                    "frm_payload": frm_payload_b64,
                    "priority": "NORMAL",
                }
            ]
        }
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        return await self._post(url, body, headers, "TTN")

    async def _send_chirpstack(self, data_b64: str) -> bool:
        hub = self._entry.data
        sub = self._subentry.data
        base_url = str(hub[CONF_CS_BASE_URL]).rstrip("/")
        dev_eui = sub[CONF_CS_DEV_EUI]
        token = hub[CONF_CS_API_TOKEN]

        url = f"{base_url}/api/devices/{dev_eui}/queue"
        body = {
            "queueItem": {
                "fPort": self._fport,
                "data": data_b64,
                "confirmed": False,
                "devEui": dev_eui,
            }
        }
        headers = {
            "Authorization": f"Bearer {token}",
            "Grpc-Metadata-Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        return await self._post(url, body, headers, "ChirpStack")

    async def _post(
        self, url: str, body: dict, headers: dict, label: str
    ) -> bool:
        session = async_get_clientsession(self.hass)
        try:
            async with session.post(
                url, json=body, headers=headers, timeout=_HTTP_TIMEOUT
            ) as resp:
                if resp.status >= 400:
                    text = await resp.text()
                    _LOGGER.error(
                        "%s downlink failed for %s: HTTP %s — %s",
                        label,
                        self.entity_id,
                        resp.status,
                        text[:500],
                    )
                    return False
                _LOGGER.debug(
                    "%s downlink queued for %s (HTTP %s)",
                    label,
                    self.entity_id,
                    resp.status,
                )
                return True
        except asyncio.TimeoutError:
            _LOGGER.error("%s downlink timed out for %s", label, self.entity_id)
        except aiohttp.ClientError as err:
            _LOGGER.error(
                "%s downlink HTTP error for %s: %s", label, self.entity_id, err
            )
        except Exception as err:  # noqa: BLE001 — last-resort safety net
            _LOGGER.exception(
                "%s downlink unexpected error for %s: %s",
                label,
                self.entity_id,
                err,
            )
        return False
