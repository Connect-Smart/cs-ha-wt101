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

from .calibration import is_calibrating
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


def build_external_sensor_mode_payload(enable: bool, timeout_min: int) -> bytes:
    """ff c4 + enable(1B) + UINT8 timeout (min, 3–60).

    When enabled the WT101 ignores its built-in sensor and regulates on the
    temperature pushed via ``build_external_temperature_payload``. If no value
    arrives within the timeout it falls back per the offline control mode.
    """
    timeout = max(3, min(60, int(timeout_min)))
    return b"\xff\xc4" + struct.pack("BB", 1 if enable else 0, timeout)


def build_external_offline_mode_payload(mode: int) -> bytes:
    """ff f8 + mode: 0 keep valve, 1 internal sensor, 2 control off."""
    return b"\xff\xf8" + struct.pack("B", mode)


def build_external_temperature_payload(temperature_c: float) -> bytes:
    """03 + INT16LE value×10 + ff (trailing byte per the official encoder)."""
    value_units = max(-32768, min(32767, int(round(temperature_c * 10))))
    return b"\x03" + struct.pack("<h", value_units) + b"\xff"


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
    force: bool = False,
    replace_queue: bool = False,
) -> bool:
    """Queue a downlink on whichever LNS the hub points at.

    ``force`` bypasses the calibration hold (only the calibration itself
    uses it). ``replace_queue`` drops everything already queued for the
    device first, so this downlink is the next one it receives.
    """
    if not force and is_calibrating(hass, sub_data):
        _LOGGER.debug(
            "%s: holding downlink %s while the valve calibrates",
            label,
            payload.hex(),
        )
        return False

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
        return await _send_ttn(
            hass, entry, sub_data, b64, fport, label, replace_queue
        )
    if platform == PLATFORM_CHIRPSTACK:
        return await _send_chirpstack(
            hass, entry, sub_data, b64, fport, label, replace_queue
        )
    _LOGGER.error("%s: unknown platform %s", label, platform)
    return False


async def _send_ttn(
    hass: HomeAssistant,
    entry: ConfigEntry,
    sub_data: dict[str, Any],
    frm_payload_b64: str,
    fport: int,
    label: str,
    replace_queue: bool,
) -> bool:
    hub = entry.data
    base_url = str(hub[CONF_TTN_BASE_URL]).rstrip("/")
    app_id = hub[CONF_TTN_APPLICATION_ID]
    device_id = sub_data[CONF_TTN_DEVICE_ID]
    api_key = hub[CONF_TTN_API_KEY]

    url = (
        f"{base_url}/api/v3/as/applications/{app_id}"
        f"/devices/{device_id}/down/{'replace' if replace_queue else 'push'}"
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
    return await _request(hass, "POST", url, headers, f"{label}/TTN", body)


async def _send_chirpstack(
    hass: HomeAssistant,
    entry: ConfigEntry,
    sub_data: dict[str, Any],
    data_b64: str,
    fport: int,
    label: str,
    replace_queue: bool,
) -> bool:
    hub = entry.data
    base_url = str(hub[CONF_CS_BASE_URL]).rstrip("/")
    dev_eui = sub_data[CONF_CS_DEV_EUI]
    token = hub[CONF_CS_API_TOKEN]
    headers = {
        "Authorization": f"Bearer {token}",
        "Grpc-Metadata-Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    label = f"{label}/ChirpStack"

    url = f"{base_url}/api/devices/{dev_eui}/queue"
    if replace_queue and not await _flush_chirpstack_queue(hass, url, headers, label):
        return False
    body = {
        "queueItem": {
            "fPort": fport,
            "data": data_b64,
            "confirmed": False,
            "devEui": dev_eui,
        }
    }
    return await _request(hass, "POST", url, headers, label, body)


async def _flush_chirpstack_queue(
    hass: HomeAssistant, url: str, headers: dict, label: str
) -> bool:
    """Empty the device queue if anything is waiting in it."""
    listing = await _request(hass, "GET", url, headers, label, want_json=True)
    if not isinstance(listing, dict):
        return False
    queued = int(listing.get("totalCount") or len(listing.get("result") or []))
    if not queued:
        return True
    _LOGGER.info(
        "%s: flushing %d queued downlink(s) before calibrating: %s",
        label,
        queued,
        [
            base64.b64decode(item.get("data") or "").hex()
            for item in listing.get("result") or []
        ],
    )
    return bool(await _request(hass, "DELETE", url, headers, label))


async def _request(
    hass: HomeAssistant,
    method: str,
    url: str,
    headers: dict,
    label: str,
    body: dict | None = None,
    *,
    want_json: bool = False,
) -> Any:
    """HTTP call to the LNS; True/JSON on success, False on any failure."""
    session = async_get_clientsession(hass)
    try:
        async with session.request(
            method, url, json=body, headers=headers, timeout=_HTTP_TIMEOUT
        ) as resp:
            if resp.status >= 400:
                text = await resp.text()
                _LOGGER.error(
                    "%s %s failed: HTTP %s — %s",
                    label,
                    method,
                    resp.status,
                    text[:500],
                )
                return False
            _LOGGER.debug("%s %s ok (HTTP %s)", label, method, resp.status)
            if want_json:
                return await resp.json(content_type=None)
            return True
    except asyncio.TimeoutError:
        _LOGGER.error("%s %s timed out", label, method)
    except aiohttp.ClientError as err:
        _LOGGER.error("%s %s HTTP error: %s", label, method, err)
    except ValueError as err:
        _LOGGER.error("%s %s returned invalid JSON: %s", label, method, err)
    except Exception as err:  # noqa: BLE001
        _LOGGER.exception("%s %s unexpected error: %s", label, method, err)
    return False
