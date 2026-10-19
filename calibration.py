"""Keep a WT101's downlink queue quiet while its valve motor calibrates.

A calibration started over LoRaWAN failed or produced a far too short stroke
when other downlinks (external temperature, target) reached the device while
the motor was measuring: the WT101 fetches queued downlinks right away when
the network signals more are pending, so they land mid-calibration.

While a calibration is in progress for a device:

- every downlink except the calibration itself is held back
  (``async_send_downlink`` refuses it unless ``force=True``),
- user changes on the other entities are rejected with a clear error,
- automatic senders (setpoint arbiter, external temperature pushes) skip
  and resync once the calibration is done.

It ends when the device reports a calibration result (ChirpStack webhook) or
after ``CALIBRATION_TIMEOUT`` (TTN has no result path here).
"""
from __future__ import annotations

from datetime import datetime, timedelta
import logging
from typing import Any

from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_call_later
from homeassistant.util import dt as dt_util

from .const import CONF_CS_DEV_EUI, CONF_TTN_DEVICE_ID, DOMAIN

_LOGGER = logging.getLogger(__name__)

# Delivery waits for the next uplink (~10 min by default) and the motor run
# itself takes under a minute; leave room for one missed uplink.
CALIBRATION_TIMEOUT = timedelta(minutes=30)

STATE_STARTED = "started"
STATE_DONE = "done"

_DATA_KEY = "calibrating"


def device_key(sub_data: dict[str, Any]) -> str:
    """Stable per-device key shared by the webhook and the entities."""
    return str(
        sub_data.get(CONF_CS_DEV_EUI) or sub_data.get(CONF_TTN_DEVICE_ID) or ""
    ).lower()


def calibration_signal(key: str) -> str:
    """Dispatcher signal carrying {"state": started|done, "result": ...}."""
    return f"{DOMAIN}_calibration_{key.lower()}"


def _active(hass: HomeAssistant) -> dict[str, tuple[datetime, CALLBACK_TYPE]]:
    return hass.data.setdefault(DOMAIN, {}).setdefault(_DATA_KEY, {})


def is_calibrating(hass: HomeAssistant, sub_data: dict[str, Any]) -> bool:
    key = device_key(sub_data)
    return bool(key) and key in _active(hass)


def raise_if_calibrating(hass: HomeAssistant, sub_data: dict[str, Any]) -> None:
    """Reject a user action instead of silently dropping its downlink."""
    if is_calibrating(hass, sub_data):
        raise HomeAssistantError(
            "Valve calibration in progress on this WT101; other commands are "
            "held back until it reports a result (max "
            f"{int(CALIBRATION_TIMEOUT.total_seconds() // 60)} min)"
        )


@callback
def async_start(hass: HomeAssistant, sub_data: dict[str, Any]) -> None:
    """Mark the device as calibrating; ends on result or timeout."""
    key = device_key(sub_data)
    if not key:
        return
    active = _active(hass)
    if key in active:
        active.pop(key)[1]()  # restart: cancel the previous timeout

    @callback
    def _timeout(_now: datetime) -> None:
        _LOGGER.warning(
            "WT101 %s: no calibration result within %s; releasing held "
            "downlinks",
            key,
            CALIBRATION_TIMEOUT,
        )
        async_finish(hass, key, None)

    cancel = async_call_later(hass, CALIBRATION_TIMEOUT, _timeout)
    active[key] = (dt_util.utcnow(), cancel)
    async_dispatcher_send(
        hass, calibration_signal(key), {"state": STATE_STARTED, "result": None}
    )


@callback
def async_finish(hass: HomeAssistant, key: str, result: str | None) -> None:
    """Calibration over (result reported, timed out or start failed)."""
    key = key.lower()
    entry = _active(hass).pop(key, None)
    if entry is None:
        return
    entry[1]()
    async_dispatcher_send(
        hass, calibration_signal(key), {"state": STATE_DONE, "result": result}
    )
