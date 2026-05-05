"""Config flow for the Milesight WT101 climate integration."""
from __future__ import annotations

import asyncio
import logging
from typing import Any

import aiohttp
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.const import CONF_NAME
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
    DEFAULT_CS_BASE_URL,
    DEFAULT_FPORT,
    DEFAULT_MAX_TEMP,
    DEFAULT_MIN_TEMP,
    DEFAULT_TOLERANCE,
    DEFAULT_TTN_BASE_URL,
    DOMAIN,
    PLATFORM_CHIRPSTACK,
    PLATFORM_TTN,
    WT101_BRAND_KEYS,
    WT101_MODEL_KEYS,
)

_LOGGER = logging.getLogger(__name__)
_HTTP_TIMEOUT = aiohttp.ClientTimeout(total=10)
_MANUAL_ENTRY = "__manual__"


class _ApiError(Exception):
    """Raised when the upstream LoRaWAN API rejects a request or is unreachable."""


class Wt101ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Multi-step UI flow for adding one WT101 thermostat per entry."""

    VERSION = 1

    def __init__(self) -> None:
        self._common: dict[str, Any] = {}
        self._creds: dict[str, Any] = {}
        self._device_options: list[SelectOptionDict] = []
        self._app_options: list[SelectOptionDict] = []

    # --------------------------------------------------------------- shared
    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Collect name, sensors, platform choice and limits."""
        if user_input is not None:
            self._common = user_input
            if user_input[CONF_PLATFORM_TYPE] == PLATFORM_TTN:
                return await self.async_step_ttn_creds()
            return await self.async_step_chirpstack_creds()

        schema = vol.Schema(
            {
                vol.Required(CONF_NAME): str,
                vol.Required(CONF_CURRENT_TEMP_SENSOR): EntitySelector(
                    EntitySelectorConfig(domain="sensor")
                ),
                vol.Required(CONF_TARGET_TEMP_SENSOR): EntitySelector(
                    EntitySelectorConfig(domain="sensor")
                ),
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

    # ------------------------------------------------------------------ TTN
    async def async_step_ttn_creds(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Collect TTN credentials and try to discover devices."""
        errors: dict[str, str] = {}
        if user_input is not None:
            self._creds = dict(user_input)
            try:
                devices = await _ttn_list_devices(
                    self.hass,
                    user_input[CONF_TTN_BASE_URL],
                    user_input[CONF_TTN_APPLICATION_ID],
                    user_input[CONF_TTN_API_KEY],
                )
            except _ApiError as err:
                _LOGGER.warning("TTN device listing failed: %s", err)
                errors["base"] = "cannot_connect"
            else:
                self._device_options = _ttn_build_options(devices)
                return await self.async_step_ttn_device()

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
            step_id="ttn_creds", data_schema=schema, errors=errors
        )

    async def async_step_ttn_device(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Pick a discovered TTN device or fall back to manual entry."""
        if user_input is not None:
            choice = user_input[CONF_TTN_DEVICE_ID]
            if choice == _MANUAL_ENTRY:
                return await self.async_step_ttn_manual()
            return self._finish_ttn(choice)

        options = list(self._device_options) + [
            SelectOptionDict(value=_MANUAL_ENTRY, label="Manual entry…")
        ]
        schema = vol.Schema(
            {
                vol.Required(CONF_TTN_DEVICE_ID): SelectSelector(
                    SelectSelectorConfig(
                        options=options, mode=SelectSelectorMode.DROPDOWN
                    )
                ),
            }
        )
        return self.async_show_form(step_id="ttn_device", data_schema=schema)

    async def async_step_ttn_manual(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Type a TTN device id by hand."""
        if user_input is not None:
            return self._finish_ttn(user_input[CONF_TTN_DEVICE_ID].strip())
        schema = vol.Schema({vol.Required(CONF_TTN_DEVICE_ID): str})
        return self.async_show_form(step_id="ttn_manual", data_schema=schema)

    def _finish_ttn(self, device_id: str) -> config_entries.ConfigFlowResult:
        data = {**self._common, **self._creds, CONF_TTN_DEVICE_ID: device_id}
        return self.async_create_entry(title=self._common[CONF_NAME], data=data)

    # ------------------------------------------------------------ ChirpStack
    async def async_step_chirpstack_creds(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Collect ChirpStack credentials and try to discover applications."""
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
                    "ChirpStack discovery failed (%s) — continuing with manual entry",
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
            step_id="chirpstack_creds", data_schema=schema, errors=errors
        )

    async def async_step_chirpstack_app(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Pick a ChirpStack application (or skip to manual EUI entry)."""
        errors: dict[str, str] = {}
        if user_input is not None:
            app_id = user_input[CONF_CS_APPLICATION_ID]
            if app_id == _MANUAL_ENTRY:
                return await self.async_step_chirpstack_manual()
            try:
                devices = await _cs_list_devices(
                    self.hass,
                    self._creds[CONF_CS_BASE_URL],
                    self._creds[CONF_CS_API_TOKEN],
                    app_id,
                )
            except _ApiError as err:
                _LOGGER.warning("ChirpStack device listing failed: %s", err)
                errors["base"] = "cannot_connect"
            else:
                self._device_options = _cs_build_options(devices)
                self._creds[CONF_CS_APPLICATION_ID] = app_id
                return await self.async_step_chirpstack_device()

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
        return self.async_show_form(
            step_id="chirpstack_app", data_schema=schema, errors=errors
        )

    async def async_step_chirpstack_device(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Pick a discovered ChirpStack device or fall back to manual entry."""
        if user_input is not None:
            choice = user_input[CONF_CS_DEV_EUI]
            if choice == _MANUAL_ENTRY:
                return await self.async_step_chirpstack_manual()
            return self._finish_chirpstack(choice)

        options = list(self._device_options) + [
            SelectOptionDict(value=_MANUAL_ENTRY, label="Manual entry…")
        ]
        schema = vol.Schema(
            {
                vol.Required(CONF_CS_DEV_EUI): SelectSelector(
                    SelectSelectorConfig(
                        options=options, mode=SelectSelectorMode.DROPDOWN
                    )
                ),
            }
        )
        return self.async_show_form(step_id="chirpstack_device", data_schema=schema)

    async def async_step_chirpstack_manual(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Type a ChirpStack devEui by hand."""
        if user_input is not None:
            return self._finish_chirpstack(user_input[CONF_CS_DEV_EUI].strip())
        schema = vol.Schema({vol.Required(CONF_CS_DEV_EUI): str})
        return self.async_show_form(step_id="chirpstack_manual", data_schema=schema)

    def _finish_chirpstack(self, dev_eui: str) -> config_entries.ConfigFlowResult:
        data = {**self._common, **self._creds, CONF_CS_DEV_EUI: dev_eui}
        return self.async_create_entry(title=self._common[CONF_NAME], data=data)


# ============================================================== TTN helpers
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
    return payload.get("end_devices", [])


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


# ====================================================== ChirpStack helpers
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
                        {"id": a["id"], "label": f"{tenant_name} / {a.get('name', a['id'])}"}
                    )
    if apps:
        return apps

    # Fallback: try to list applications without tenant filter (older deploys)
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
