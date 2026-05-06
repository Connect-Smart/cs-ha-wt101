"""Config flow for the Milesight WT101 climate integration.

Architecture: one **hub** config entry per LoRaWAN application (TTN application or
ChirpStack application). Each thermostat is added as a **subentry** under that hub
so all knoppen share one set of credentials.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

import aiohttp
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.components import webhook
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlowResult,
    ConfigSubentryFlow,
    OptionsFlow,
    SubentryFlowResult,
)
from homeassistant.const import CONF_NAME
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    EntitySelector,
    EntitySelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .const import (
    CONF_CS_API_TOKEN,
    CONF_CS_APPLICATION_ID,
    CONF_CS_APPLICATION_NAME,
    CONF_CS_BASE_URL,
    CONF_CS_DEV_EUI,
    CONF_CS_WEBHOOK_ID,
    CONF_CURRENT_TEMP_SENSOR,
    CONF_FPORT,
    CONF_MAX_TEMP,
    CONF_MIN_TEMP,
    CONF_MOTOR_POSITION_SENSOR,
    CONF_MOTOR_STROKE_SENSOR,
    CONF_PLATFORM_TYPE,
    CONF_TARGET_TEMP_SENSOR,
    CONF_TOLERANCE,
    CONF_TTN_API_KEY,
    CONF_TTN_APPLICATION_ID,
    CONF_TTN_BASE_URL,
    CONF_TTN_DEVICE_ID,
    DEFAULT_CS_BASE_URL,
    DEFAULT_FPORT,
    DEFAULT_MAX_TEMP,
    DEFAULT_MIN_TEMP,
    DEFAULT_TOLERANCE,
    DEFAULT_TTN_BASE_URL,
    DOMAIN,
    PLATFORM_CHIRPSTACK,
    PLATFORM_TTN,
    SUBENTRY_TYPE_THERMOSTAT,
    WT101_BRAND_KEYS,
    WT101_MODEL_KEYS,
)

_LOGGER = logging.getLogger(__name__)
_HTTP_TIMEOUT = aiohttp.ClientTimeout(total=10)
_MANUAL_ENTRY = "__manual__"


class _ApiError(Exception):
    """Raised when the upstream LoRaWAN API rejects a request or is unreachable."""


# =====================================================================
# Hub flow — one entry per TTN/ChirpStack application
# =====================================================================
class Wt101ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Add a LoRaWAN application as a hub. Thermostats are added as subentries."""

    VERSION = 1

    def __init__(self) -> None:
        self._platform: str | None = None
        self._creds: dict[str, Any] = {}
        self._app_options: list[SelectOptionDict] = []
        self._pending_cs_entry: dict[str, Any] | None = None
        self._pending_cs_title: str | None = None

    @staticmethod
    @callback
    def async_get_options_flow(entry: ConfigEntry) -> OptionsFlow:
        return Wt101OptionsFlow()

    @classmethod
    @callback
    def async_get_supported_subentry_types(
        cls, config_entry: ConfigEntry
    ) -> dict[str, type[ConfigSubentryFlow]]:
        """Allow adding thermostats as subentries under the hub."""
        return {SUBENTRY_TYPE_THERMOSTAT: ThermostatSubentryFlow}

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Choose TTN or ChirpStack."""
        if user_input is not None:
            self._platform = user_input[CONF_PLATFORM_TYPE]
            if self._platform == PLATFORM_TTN:
                return await self.async_step_ttn()
            return await self.async_step_chirpstack()

        schema = vol.Schema(
            {
                vol.Required(
                    CONF_PLATFORM_TYPE, default=PLATFORM_TTN
                ): SelectSelector(
                    SelectSelectorConfig(
                        options=[
                            SelectOptionDict(
                                value=PLATFORM_TTN,
                                label="The Things Stack (TTN)",
                            ),
                            SelectOptionDict(
                                value=PLATFORM_CHIRPSTACK, label="ChirpStack"
                            ),
                        ],
                        mode=SelectSelectorMode.DROPDOWN,
                    )
                ),
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema)

    # --- TTN hub: server URL + application id + API key ---------------
    async def async_step_ttn(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                # Validate creds by listing devices.
                await _ttn_list_devices(
                    self.hass,
                    user_input[CONF_TTN_BASE_URL],
                    user_input[CONF_TTN_APPLICATION_ID],
                    user_input[CONF_TTN_API_KEY],
                )
            except _ApiError as err:
                _LOGGER.warning("TTN validation failed: %s", err)
                errors["base"] = "cannot_connect"
            else:
                unique = (
                    f"ttn:{user_input[CONF_TTN_BASE_URL].rstrip('/')}"
                    f":{user_input[CONF_TTN_APPLICATION_ID]}"
                )
                await self.async_set_unique_id(unique)
                self._abort_if_unique_id_configured()
                title = f"TTN: {user_input[CONF_TTN_APPLICATION_ID]}"
                return self.async_create_entry(
                    title=title,
                    data={CONF_PLATFORM_TYPE: PLATFORM_TTN, **user_input},
                )

        defaults = self._creds
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_TTN_BASE_URL,
                    default=defaults.get(CONF_TTN_BASE_URL, DEFAULT_TTN_BASE_URL),
                ): str,
                vol.Required(
                    CONF_TTN_APPLICATION_ID,
                    default=defaults.get(CONF_TTN_APPLICATION_ID, ""),
                ): str,
                vol.Required(
                    CONF_TTN_API_KEY,
                    default=defaults.get(CONF_TTN_API_KEY, ""),
                ): TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD)),
            }
        )
        return self.async_show_form(
            step_id="ttn", data_schema=schema, errors=errors
        )

    # --- ChirpStack hub: creds + pick one application -----------------
    async def async_step_chirpstack(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            self._creds = dict(user_input)
            try:
                apps = await _cs_list_applications(
                    self.hass,
                    user_input[CONF_CS_BASE_URL],
                    user_input[CONF_CS_API_TOKEN],
                )
            except _ApiError as err:
                _LOGGER.warning(
                    "ChirpStack discovery failed (%s); allowing manual app entry",
                    err,
                )
                self._app_options = []
            else:
                self._app_options = [
                    SelectOptionDict(value=a["id"], label=a["label"]) for a in apps
                ]
            return await self.async_step_chirpstack_app()

        defaults = self._creds
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_CS_BASE_URL,
                    default=defaults.get(CONF_CS_BASE_URL, DEFAULT_CS_BASE_URL),
                ): str,
                vol.Required(
                    CONF_CS_API_TOKEN,
                    default=defaults.get(CONF_CS_API_TOKEN, ""),
                ): TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD)),
            }
        )
        return self.async_show_form(
            step_id="chirpstack", data_schema=schema, errors=errors
        )

    async def async_step_chirpstack_app(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            app_id = user_input[CONF_CS_APPLICATION_ID]
            if app_id == _MANUAL_ENTRY:
                return await self.async_step_chirpstack_app_manual()
            label = next(
                (o["label"] for o in self._app_options if o["value"] == app_id),
                app_id,
            )
            return await self._finish_chirpstack(app_id, label)

        options = list(self._app_options) + [
            SelectOptionDict(value=_MANUAL_ENTRY, label="Manual entry…")
        ]
        schema = vol.Schema(
            {
                vol.Required(CONF_CS_APPLICATION_ID): SelectSelector(
                    SelectSelectorConfig(
                        options=options, mode=SelectSelectorMode.DROPDOWN
                    )
                ),
            }
        )
        return self.async_show_form(step_id="chirpstack_app", data_schema=schema)

    async def async_step_chirpstack_app_manual(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            app_id = user_input[CONF_CS_APPLICATION_ID].strip()
            return await self._finish_chirpstack(app_id, app_id)
        schema = vol.Schema({vol.Required(CONF_CS_APPLICATION_ID): str})
        return self.async_show_form(
            step_id="chirpstack_app_manual", data_schema=schema
        )

    async def _finish_chirpstack(
        self, application_id: str, application_label: str
    ) -> ConfigFlowResult:
        unique = (
            f"chirpstack:{self._creds[CONF_CS_BASE_URL].rstrip('/')}"
            f":{application_id}"
        )
        await self.async_set_unique_id(unique)
        self._abort_if_unique_id_configured()

        # Stash the entry payload and show the webhook URL before creating it,
        # so the user can paste it into ChirpStack's HTTP integration.
        self._pending_cs_entry = {
            CONF_PLATFORM_TYPE: PLATFORM_CHIRPSTACK,
            CONF_CS_BASE_URL: self._creds[CONF_CS_BASE_URL],
            CONF_CS_API_TOKEN: self._creds[CONF_CS_API_TOKEN],
            CONF_CS_APPLICATION_ID: application_id,
            CONF_CS_APPLICATION_NAME: application_label,
            CONF_CS_WEBHOOK_ID: webhook.async_generate_id(),
        }
        self._pending_cs_title = f"ChirpStack: {application_label}"
        return await self.async_step_chirpstack_webhook()

    async def async_step_chirpstack_webhook(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show the webhook URL to paste into ChirpStack's HTTP integration."""
        assert self._pending_cs_entry is not None
        assert self._pending_cs_title is not None
        webhook_id = self._pending_cs_entry[CONF_CS_WEBHOOK_ID]

        if user_input is not None:
            data = self._pending_cs_entry
            title = self._pending_cs_title
            self._pending_cs_entry = None
            self._pending_cs_title = None
            return self.async_create_entry(title=title, data=data)

        url = webhook.async_generate_url(self.hass, webhook_id)
        return self.async_show_form(
            step_id="chirpstack_webhook",
            data_schema=vol.Schema({}),
            description_placeholders={"webhook_url": url},
        )


# =====================================================================
# Options flow — re-display the ChirpStack webhook URL after setup
# =====================================================================
class Wt101OptionsFlow(OptionsFlow):
    """Display the ChirpStack webhook URL so users can copy it again later."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        entry = self.config_entry
        if entry.data.get(CONF_PLATFORM_TYPE) != PLATFORM_CHIRPSTACK:
            return self.async_create_entry(title="", data={})

        webhook_id = entry.data.get(CONF_CS_WEBHOOK_ID)
        if not webhook_id:
            return self.async_abort(reason="no_webhook")

        if user_input is not None:
            return self.async_create_entry(title="", data={})

        url = webhook.async_generate_url(self.hass, webhook_id)
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema({}),
            description_placeholders={"webhook_url": url},
        )


# =====================================================================
# Subentry flow — one thermostat per subentry under a hub
# =====================================================================
class ThermostatSubentryFlow(ConfigSubentryFlow):
    """Add or reconfigure a single WT101 thermostat under a hub entry."""

    def __init__(self) -> None:
        self._common: dict[str, Any] = {}
        self._device_options: list[SelectOptionDict] = []

    def _hub_entry(self) -> ConfigEntry:
        return self._get_entry()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Step 1: name, sensors, fport, limits."""
        if user_input is not None:
            self._common = user_input
            return await self.async_step_device()

        schema = vol.Schema(
            {
                vol.Required(CONF_NAME): str,
                vol.Optional(CONF_CURRENT_TEMP_SENSOR): EntitySelector(
                    EntitySelectorConfig(domain="sensor")
                ),
                vol.Optional(CONF_TARGET_TEMP_SENSOR): EntitySelector(
                    EntitySelectorConfig(domain="sensor")
                ),
                vol.Optional(CONF_MOTOR_POSITION_SENSOR): EntitySelector(
                    EntitySelectorConfig(domain="sensor")
                ),
                vol.Optional(CONF_MOTOR_STROKE_SENSOR): EntitySelector(
                    EntitySelectorConfig(domain="sensor")
                ),
                vol.Optional(CONF_FPORT, default=DEFAULT_FPORT): NumberSelector(
                    NumberSelectorConfig(
                        min=1, max=255, step=1, mode=NumberSelectorMode.BOX
                    )
                ),
                vol.Optional(
                    CONF_MIN_TEMP, default=DEFAULT_MIN_TEMP
                ): NumberSelector(
                    NumberSelectorConfig(
                        min=5, max=35, step=0.5, mode=NumberSelectorMode.BOX
                    )
                ),
                vol.Optional(
                    CONF_MAX_TEMP, default=DEFAULT_MAX_TEMP
                ): NumberSelector(
                    NumberSelectorConfig(
                        min=5, max=35, step=0.5, mode=NumberSelectorMode.BOX
                    )
                ),
                vol.Optional(
                    CONF_TOLERANCE, default=DEFAULT_TOLERANCE
                ): NumberSelector(
                    NumberSelectorConfig(
                        min=0.1, max=5.0, step=0.1, mode=NumberSelectorMode.BOX
                    )
                ),
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema)

    async def async_step_device(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Step 2: pick the thermostat from the hub's application."""
        entry = self._hub_entry()
        platform = entry.data[CONF_PLATFORM_TYPE]

        # Discover devices once per visit so the dropdown is fresh.
        if not self._device_options:
            try:
                if platform == PLATFORM_TTN:
                    devices = await _ttn_list_devices(
                        self.hass,
                        entry.data[CONF_TTN_BASE_URL],
                        entry.data[CONF_TTN_APPLICATION_ID],
                        entry.data[CONF_TTN_API_KEY],
                    )
                    self._device_options = _ttn_build_options(devices)
                else:
                    devices = await _cs_list_devices(
                        self.hass,
                        entry.data[CONF_CS_BASE_URL],
                        entry.data[CONF_CS_API_TOKEN],
                        entry.data[CONF_CS_APPLICATION_ID],
                    )
                    self._device_options = _cs_build_options(devices)
            except _ApiError as err:
                _LOGGER.warning("Device discovery failed: %s", err)
                self._device_options = []

        device_key = (
            CONF_TTN_DEVICE_ID if platform == PLATFORM_TTN else CONF_CS_DEV_EUI
        )

        if user_input is not None:
            choice = user_input[device_key]
            if choice == _MANUAL_ENTRY:
                return await self.async_step_manual()
            return self._create(device_key, choice)

        options = list(self._device_options) + [
            SelectOptionDict(value=_MANUAL_ENTRY, label="Manual entry…")
        ]
        schema = vol.Schema(
            {
                vol.Required(device_key): SelectSelector(
                    SelectSelectorConfig(
                        options=options, mode=SelectSelectorMode.DROPDOWN
                    )
                ),
            }
        )
        return self.async_show_form(step_id="device", data_schema=schema)

    async def async_step_manual(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Manual fallback when discovery is unavailable."""
        platform = self._hub_entry().data[CONF_PLATFORM_TYPE]
        device_key = (
            CONF_TTN_DEVICE_ID if platform == PLATFORM_TTN else CONF_CS_DEV_EUI
        )
        if user_input is not None:
            return self._create(device_key, user_input[device_key].strip())
        schema = vol.Schema({vol.Required(device_key): str})
        return self.async_show_form(step_id="manual", data_schema=schema)

    def _create(self, device_key: str, device_value: str) -> SubentryFlowResult:
        data = {**self._common, device_key: device_value}
        return self.async_create_entry(title=self._common[CONF_NAME], data=data)


# =====================================================================
# TTN helpers
# =====================================================================
async def _ttn_list_devices(
    hass, base_url: str, application_id: str, api_key: str
) -> list[dict[str, Any]]:
    """Fetch end devices from a Things Stack v3 application."""
    url = f"{base_url.rstrip('/')}/api/v3/applications/{application_id}/devices"
    params = {"field_mask": "name,description,version_ids"}
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
    }
    session = async_get_clientsession(hass)
    try:
        async with session.get(
            url, headers=headers, params=params, timeout=_HTTP_TIMEOUT
        ) as resp:
            if resp.status >= 400:
                text = await resp.text()
                raise _ApiError(f"HTTP {resp.status}: {text[:200]}")
            payload = await resp.json()
    except (asyncio.TimeoutError, aiohttp.ClientError) as err:
        raise _ApiError(str(err)) from err
    return payload.get("end_devices", []) or []


def _ttn_build_options(devices: list[dict[str, Any]]) -> list[SelectOptionDict]:
    """Sort discovered devices with WT101 matches at the top."""
    wt101: list[SelectOptionDict] = []
    others: list[SelectOptionDict] = []
    for d in devices:
        ids = d.get("ids") or {}
        device_id = ids.get("device_id")
        if not device_id:
            continue
        version = d.get("version_ids") or {}
        brand = (version.get("brand_id") or "").lower()
        model = (version.get("model_id") or "").lower()
        name = d.get("name") or device_id
        if any(b in brand for b in WT101_BRAND_KEYS) and any(
            m in model for m in WT101_MODEL_KEYS
        ):
            wt101.append(
                SelectOptionDict(
                    value=device_id, label=f"{name} ({device_id}) — WT101"
                )
            )
            continue
        suffix = ""
        if version.get("model_id"):
            suffix = f" — {version.get('brand_id', '')} {version.get('model_id', '')}".rstrip()
        others.append(
            SelectOptionDict(value=device_id, label=f"{name} ({device_id}){suffix}")
        )
    return wt101 + others


# =====================================================================
# ChirpStack helpers
# =====================================================================
async def _cs_list_applications(
    hass, base_url: str, token: str
) -> list[dict[str, str]]:
    """Discover applications across all visible tenants."""
    session = async_get_clientsession(hass)
    base = base_url.rstrip("/")
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}

    tenants: list[dict[str, Any]] = []
    try:
        async with session.get(
            f"{base}/api/tenants",
            params={"limit": 100},
            headers=headers,
            timeout=_HTTP_TIMEOUT,
        ) as resp:
            if resp.status < 400:
                tenants = (await resp.json()).get("result", []) or []
    except (asyncio.TimeoutError, aiohttp.ClientError):
        tenants = []

    apps: list[dict[str, str]] = []
    if tenants:
        for tenant in tenants:
            tenant_id = tenant.get("id")
            tenant_name = tenant.get("name") or tenant_id
            if not tenant_id:
                continue
            try:
                async with session.get(
                    f"{base}/api/applications",
                    params={"limit": 100, "tenantId": tenant_id},
                    headers=headers,
                    timeout=_HTTP_TIMEOUT,
                ) as resp:
                    if resp.status >= 400:
                        continue
                    payload = await resp.json()
            except (asyncio.TimeoutError, aiohttp.ClientError):
                continue
            for a in payload.get("result", []) or []:
                if a.get("id"):
                    apps.append(
                        {
                            "id": a["id"],
                            "label": f"{tenant_name} / {a.get('name', a['id'])}",
                        }
                    )
    if apps:
        return apps

    # Fallback: list applications without tenant filter (older deploys).
    try:
        async with session.get(
            f"{base}/api/applications",
            params={"limit": 100},
            headers=headers,
            timeout=_HTTP_TIMEOUT,
        ) as resp:
            if resp.status >= 400:
                text = await resp.text()
                raise _ApiError(f"HTTP {resp.status}: {text[:200]}")
            payload = await resp.json()
    except (asyncio.TimeoutError, aiohttp.ClientError) as err:
        raise _ApiError(str(err)) from err
    return [
        {"id": a["id"], "label": a.get("name", a["id"])}
        for a in (payload.get("result") or [])
        if a.get("id")
    ]


async def _cs_list_devices(
    hass, base_url: str, token: str, application_id: str
) -> list[dict[str, Any]]:
    """Fetch devices in a ChirpStack application."""
    session = async_get_clientsession(hass)
    base = base_url.rstrip("/")
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    try:
        async with session.get(
            f"{base}/api/devices",
            params={"limit": 200, "applicationId": application_id},
            headers=headers,
            timeout=_HTTP_TIMEOUT,
        ) as resp:
            if resp.status >= 400:
                text = await resp.text()
                raise _ApiError(f"HTTP {resp.status}: {text[:200]}")
            payload = await resp.json()
    except (asyncio.TimeoutError, aiohttp.ClientError) as err:
        raise _ApiError(str(err)) from err
    return payload.get("result", []) or []


def _cs_build_options(devices: list[dict[str, Any]]) -> list[SelectOptionDict]:
    """Sort discovered ChirpStack devices with WT101 profile matches first."""
    wt101: list[SelectOptionDict] = []
    others: list[SelectOptionDict] = []
    for d in devices:
        dev_eui = d.get("devEui") or d.get("dev_eui")
        if not dev_eui:
            continue
        name = d.get("name") or dev_eui
        profile_name = d.get("deviceProfileName") or d.get("device_profile_name") or ""
        profile_lc = profile_name.lower()
        if any(m in profile_lc for m in WT101_MODEL_KEYS):
            wt101.append(
                SelectOptionDict(
                    value=dev_eui,
                    label=f"{name} ({dev_eui}) — {profile_name or 'WT101'}",
                )
            )
            continue
        suffix = f" — {profile_name}" if profile_name else ""
        others.append(
            SelectOptionDict(value=dev_eui, label=f"{name} ({dev_eui}){suffix}")
        )
    return wt101 + others
