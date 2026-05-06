"""The Milesight WT101 LoRaWAN climate integration.

A config entry == one TTN/ChirpStack application (the "hub"). Each thermostat
is a ConfigSubentry under that hub, so credentials are shared across all
thermostats in the same application.
"""
from __future__ import annotations

import logging
from typing import Any

from aiohttp.web import Request, Response

from homeassistant.components import webhook
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_send

from .const import (
    CONF_CS_WEBHOOK_ID,
    CONF_PLATFORM_TYPE,
    DOMAIN,
    PLATFORMS,
    PLATFORM_CHIRPSTACK,
    cs_uplink_signal,
)

_LOGGER = logging.getLogger(__name__)

# ChirpStack codec output keys we accept for current and target temperature.
_CURRENT_KEYS: tuple[str, ...] = (
    "temperature",
    "indoor_temperature",
    "current_temperature",
    "temp",
)
_TARGET_KEYS: tuple[str, ...] = (
    "temperature_target",
    "target_temperature",
    "target_temp",
    "setpoint",
)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up a hub config entry."""
    hass.data.setdefault(DOMAIN, {})

    if entry.data.get(CONF_PLATFORM_TYPE) == PLATFORM_CHIRPSTACK:
        # Migrate hubs created before the webhook feature: mint an ID once.
        if not entry.data.get(CONF_CS_WEBHOOK_ID):
            new_data = {**entry.data, CONF_CS_WEBHOOK_ID: webhook.async_generate_id()}
            hass.config_entries.async_update_entry(entry, data=new_data)
        _register_chirpstack_webhook(hass, entry)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # When subentries are added or removed the hub entry is updated;
    # reload so the climate platform picks up the new entity set.
    entry.async_on_unload(entry.add_update_listener(_async_reload_on_change))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a hub config entry."""
    if entry.data.get(CONF_PLATFORM_TYPE) == PLATFORM_CHIRPSTACK:
        webhook_id = entry.data.get(CONF_CS_WEBHOOK_ID)
        if webhook_id:
            webhook.async_unregister(hass, webhook_id)
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def _async_reload_on_change(
    hass: HomeAssistant, entry: ConfigEntry
) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


# =====================================================================
# ChirpStack HTTP integration → webhook
# =====================================================================
def _register_chirpstack_webhook(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Register the per-hub webhook that ChirpStack POSTs uplinks to."""
    webhook_id = entry.data.get(CONF_CS_WEBHOOK_ID)
    if not webhook_id:
        _LOGGER.warning(
            "ChirpStack hub %s has no webhook id stored; skipping registration",
            entry.title,
        )
        return

    async def _handle(
        hass: HomeAssistant, webhook_id: str, request: Request
    ) -> Response | None:
        await _handle_chirpstack_uplink(hass, entry, request)
        return None

    try:
        webhook.async_register(
            hass,
            DOMAIN,
            f"WT101 ChirpStack ({entry.title})",
            webhook_id,
            _handle,
            allowed_methods=["POST"],
        )
    except ValueError:
        # Already registered from a previous setup of the same entry.
        _LOGGER.debug("Webhook %s already registered", webhook_id)


async def _handle_chirpstack_uplink(
    hass: HomeAssistant, entry: ConfigEntry, request: Request
) -> None:
    """Parse a ChirpStack HTTP integration POST and fan out to entities."""
    try:
        body: dict[str, Any] = await request.json()
    except ValueError:
        _LOGGER.debug("ChirpStack webhook: non-JSON body, ignoring")
        return

    info = body.get("deviceInfo") or {}
    dev_eui = info.get("devEui") or info.get("dev_eui") or ""
    if not dev_eui:
        # Heartbeat, join, status — anything without a devEui isn't an uplink.
        return

    obj = body.get("object")
    if not isinstance(obj, dict):
        _LOGGER.debug(
            "ChirpStack uplink for %s has no decoded 'object' — load the "
            "Milesight WT101 codec in ChirpStack so temperature fields are "
            "decoded",
            dev_eui,
        )
        return

    current = _pick_temperature(obj, _CURRENT_KEYS)
    target = _pick_temperature(obj, _TARGET_KEYS)

    if current is None and target is None:
        _LOGGER.debug(
            "ChirpStack uplink for %s carried no recognised temperature "
            "fields (object keys: %s)",
            dev_eui,
            list(obj.keys()),
        )
        return

    async_dispatcher_send(
        hass, cs_uplink_signal(entry.entry_id, dev_eui), current, target
    )


def _pick_temperature(obj: dict[str, Any], keys: tuple[str, ...]) -> float | None:
    """Return the first numeric value found under any of the candidate keys."""
    for key in keys:
        if key not in obj:
            continue
        value = obj[key]
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return None
