"""Shared LoRaWAN downlink helpers for the WT101 integration.

All Milesight WT101 downlink command bytes are defined here together with a
single ``async_send_downlink`` helper that climate / button / number / switch
entities call. Keeping the HTTP plumbing in one place avoids duplicating the
TTN and ChirpStack POST logic across every platform.

Command byte values come from the official Milesight encoder:
https://github.com/Milesight-IoT/SensorDecoders/blob/main/wt-series/wt101/wt101-encoder.js
"""
from __future__ import annotations

import asyncio
import base64
import logging
import struct
from typing import Any

import aiohttp

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    CONF_CS_API_TOKEN,
    CONF_CS_BASE_URL,
    CONF_CS_DEV_EUI,
    CONF_FPORT,
    CONF_PLATFORM_TYPE,
    CONF_TTN_API_KEY,
    CONF_TTN_APPLICATION_ID,
    CONF_TTN_BASE_URL,
    CONF_TTN_DEVICE_ID,
    DEFAULT_FPORT,
    PLATFORM_CHIRPSTACK,
    PLATFORM_TTN,
)

_LOGGER = logging.getLogger(__name__)
_HTTP_TIMEOUT = aiohttp.ClientTimeout(total=10)


# =====================================================================
# Payload builders
# =====================================================================
def build_target_temperature_payload(target_c: float, tolerance_c: float) -> bytes:
    """ff b1 + INT8 °C + UINT16LE tolerance×10."""
    temp_byte = struct.pack("b", int(round(target_c)))
    tolerance_units = max(0, min(0xFFFF, int(round(tolerance_c * 10))))
    return b"\xff\xb1" + temp_byte + struct.pack("<H", tolerance_units)


def build_temperature_calibration_payload(
    enable: bool, calibration_c: float
) -> bytes:
    """ff ab + enable(1B) + INT16LE value×10. enable=0 disables the offset."""
    enable_byte = struct.pack("B", 1 if enable else 0)
    value_units = max(-32768, min(32767, int(round(calibration_c * 10))))
    return b"\xff\xab" + enable_byte + struct.pack("<h", value_units)


def build_temperature_control_enable_payload(enable: bool) -> bytes:
    """ff b3 + enable(1B)."""
    return b"\xff\xb3" + struct.pack("B", 1 if enable else 0)


def build_valve_calibration_payload() -> bytes:
    """ff ad ff — single-shot mechanical valve recalibration."""
    return b"\xff\xad\xff"


# =====================================================================
# Downlink dispatch
# =====================================================================
async def async_send_downlink(
    hass: HomeAssistant,
    entry: ConfigEntry,
    sub_data: dict[str, Any],
    payload: bytes,
    *,
    label: str = "WT101",
) -> bool:
    """POST a downlink to whichever LNS the hub points at."""
    fport = int(sub_data.get(CONF_FPORT, DEFAULT_FPORT))
    b64 = base64.b64encode(payload).decode("ascii")
    _LOGGER.debug(
        "%s: queueing downlink hex=%s base64=%s fport=%d",
        label,
        payload.hex(),
        b64,
        fport,
    )

    platform = entry.data[CONF_PLATFORM_TYPE]
    if platform == PLATFORM_TTN:
        return await _send_ttn(hass, entry, sub_data, b64, fport, label)
    if platform == PLATFORM_CHIRPSTACK:
        return await _send_chirpstack(hass, entry, sub_data, b64, fport, label)
    _LOGGER.error("%s: unknown platform %s", label, platform)
    return False


async def _send_ttn(
    hass: HomeAssistant,
    entry: ConfigEntry,
    sub_data: dict[str, Any],
    frm_payload_b64: str,
    fport: int,
    label: str,
) -> bool:
    hub = entry.data
    base_url = str(hub[CONF_TTN_BASE_URL]).rstrip("/")
    app_id = hub[CONF_TTN_APPLICATION_ID]
    device_id = sub_data[CONF_TTN_DEVICE_ID]
    api_key = hub[CONF_TTN_API_KEY]

    url = (
        f"{base_url}/api/v3/as/applications/{app_id}"
        f"/devices/{device_id}/down/push"
    )
    body = {
        "downlinks": [
            {
                "f_port": fport,
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
    return await _post(hass, url, body, headers, f"{label}/TTN")


async def _send_chirpstack(
    hass: HomeAssistant,
    entry: ConfigEntry,
    sub_data: dict[str, Any],
    data_b64: str,
    fport: int,
    label: str,
) -> bool:
    hub = entry.data
    base_url = str(hub[CONF_CS_BASE_URL]).rstrip("/")
    dev_eui = sub_data[CONF_CS_DEV_EUI]
    token = hub[CONF_CS_API_TOKEN]

    url = f"{base_url}/api/devices/{dev_eui}/queue"
    body = {
        "queueItem": {
            "fPort": fport,
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
    return await _post(hass, url, body, headers, f"{label}/ChirpStack")


async def _post(
    hass: HomeAssistant,
    url: str,
    body: dict,
    headers: dict,
    label: str,
) -> bool:
    session = async_get_clientsession(hass)
    try:
        async with session.post(
            url, json=body, headers=headers, timeout=_HTTP_TIMEOUT
        ) as resp:
            if resp.status >= 400:
                text = await resp.text()
                _LOGGER.error(
                    "%s downlink failed: HTTP %s — %s",
                    label,
                    resp.status,
                    text[:500],
                )
                return False
            _LOGGER.debug("%s downlink queued (HTTP %s)", label, resp.status)
            return True
    except asyncio.TimeoutError:
        _LOGGER.error("%s downlink timed out", label)
    except aiohttp.ClientError as err:
        _LOGGER.error("%s downlink HTTP error: %s", label, err)
    except Exception as err:  # noqa: BLE001
        _LOGGER.exception("%s downlink unexpected error: %s", label, err)
    return False
