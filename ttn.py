"""Read WT101 uplinks for TTN hubs from the TTN Storage Integration.

ChirpStack pushes uplinks to an HA webhook. TTN could do the same, but only
when HA is reachable from the internet; many installs are not. Instead HA
pulls the stored uplinks with the hub's API key once a minute, decodes the
raw payload itself and dispatches it exactly like the ChirpStack webhook.

Requires the Storage Integration to be enabled on the TTN application and
an API key with "Read application traffic" (or all application rights).
"""
from __future__ import annotations

import asyncio
import base64
from datetime import datetime, timedelta
import json
import logging
from typing import Any

import aiohttp

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_call_later, async_track_time_interval
from homeassistant.util import dt as dt_util

from .const import (
    CONF_TTN_API_KEY,
    CONF_TTN_APPLICATION_ID,
    CONF_TTN_BASE_URL,
    CONF_TTN_DEVICE_ID,
    SUBENTRY_TYPE_THERMOSTAT,
)
from .decoder import decode_uplink

_LOGGER = logging.getLogger(__name__)

POLL_INTERVAL = timedelta(seconds=60)
# On (re)start, read this much history so sensors show the latest values
# (including a calibration result from earlier today) right away.
_HISTORY = "24h"
_LIMIT = 1000
_HTTP_TIMEOUT = aiohttp.ClientTimeout(total=30)
_FIELD_MASK = ",".join(
    (
        "up.uplink_message.frm_payload",
        "up.uplink_message.decoded_payload",
        "up.uplink_message.f_port",
    )
)


class TtnUplinkPoller:
    """Polls one TTN application's stored uplinks for its WT101 devices."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, dispatch) -> None:
        self._hass = hass
        self._entry = entry
        # dispatch(device_id, decoded_object, received_at, merged_times)
        self._dispatch = dispatch
        self._after: str | None = None  # received_at of the newest uplink seen
        self._seen: set[str] = set()  # ids of uplinks at that boundary
        self._busy = False
        self._warned = False
        self._unsubs: list[CALLBACK_TYPE] = []

    @callback
    def async_start(self) -> None:
        # Give the platforms a moment to add their entities first.
        self._unsubs.append(async_call_later(self._hass, 5, self._async_poll))
        self._unsubs.append(
            async_track_time_interval(self._hass, self._async_poll, POLL_INTERVAL)
        )

    @callback
    def async_stop(self) -> None:
        while self._unsubs:
            self._unsubs.pop()()

    def _device_ids(self) -> set[str]:
        return {
            str(sub.data[CONF_TTN_DEVICE_ID])
            for sub in self._entry.subentries.values()
            if sub.subentry_type == SUBENTRY_TYPE_THERMOSTAT
            and sub.data.get(CONF_TTN_DEVICE_ID)
        }

    async def _async_poll(self, _now: datetime | None = None) -> None:
        if self._busy:
            return
        self._busy = True
        try:
            first = self._after is None
            results = await self._async_fetch(first)
            if results is None:
                return
            self._handle(results, first)
        finally:
            self._busy = False

    async def _async_fetch(self, first: bool) -> list[dict[str, Any]] | None:
        hub = self._entry.data
        url = (
            f"{str(hub[CONF_TTN_BASE_URL]).rstrip('/')}/api/v3/as/applications/"
            f"{hub[CONF_TTN_APPLICATION_ID]}/packages/storage/uplink_message"
        )
        params = {"field_mask": _FIELD_MASK, "limit": str(_LIMIT)}
        if first:
            # Newest first, so the limit drops the oldest history.
            params.update(last=_HISTORY, order="-received_at")
        else:
            params.update(after=self._after, order="received_at")
        headers = {
            "Authorization": f"Bearer {hub[CONF_TTN_API_KEY]}",
            "Accept": "application/json",
        }
        session = async_get_clientsession(self._hass)
        try:
            async with session.get(
                url, params=params, headers=headers, timeout=_HTTP_TIMEOUT
            ) as resp:
                text = await resp.text()
                if resp.status >= 400:
                    self._warn(
                        "TTN storage request failed (HTTP %s): %s. Enable the "
                        "Storage Integration on the TTN application and give the "
                        "API key the right to read application traffic",
                        resp.status,
                        text[:300],
                    )
                    return None
        except (asyncio.TimeoutError, aiohttp.ClientError) as err:
            _LOGGER.debug("TTN storage request failed: %s", err)
            return None

        self._warned = False
        results = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                result = json.loads(line).get("result")
            except ValueError:
                continue
            if isinstance(result, dict):
                results.append(result)
        if first:
            results.reverse()  # back to chronological order
        return results

    def _warn(self, msg: str, *args: Any) -> None:
        if not self._warned:
            _LOGGER.warning(msg, *args)
            self._warned = True
        else:
            _LOGGER.debug(msg, *args)

    @callback
    def _handle(self, results: list[dict[str, Any]], first: bool) -> None:
        wanted = self._device_ids()
        boundary: set[str] = set()
        merged: dict[str, tuple[dict[str, Any], dict[str, str]]] = {}

        for result in results:
            received_at = str(result.get("received_at") or "")
            device_id = str((result.get("end_device_ids") or {}).get("device_id") or "")
            uid = f"{device_id}/{received_at}"
            if uid in self._seen:
                continue  # already handled in the previous poll
            if received_at and received_at != self._after:
                self._after = received_at
                boundary = set()
            boundary.add(uid)

            if device_id not in wanted:
                continue
            obj = _decode(result.get("uplink_message") or {})
            if not obj:
                continue
            if first:
                # Replaying history: fold it into one update per device so
                # the entities see the latest value of every field once.
                fields, times = merged.setdefault(device_id, ({}, {}))
                fields.update(obj)
                times.update({key: received_at for key in obj})
            else:
                self._dispatch(device_id, obj, {key: received_at for key in obj})

        for device_id, (fields, times) in merged.items():
            self._dispatch(device_id, fields, times)

        if boundary:
            self._seen = boundary
        if self._after is None:
            # No history at all: start from now.
            self._after = dt_util.utcnow().strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _decode(uplink: dict[str, Any]) -> dict[str, Any]:
    """Decode the raw payload; fall back to TTN's payload formatter output."""
    raw = uplink.get("frm_payload")
    obj: dict[str, Any] = {}
    if raw:
        try:
            obj = decode_uplink(base64.b64decode(raw))
        except (ValueError, TypeError):
            obj = {}
    if not obj and isinstance(uplink.get("decoded_payload"), dict):
        obj = dict(uplink["decoded_payload"])
    return obj
