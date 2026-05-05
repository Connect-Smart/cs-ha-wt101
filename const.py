"""Constants for the Milesight WT101 climate integration."""
from __future__ import annotations

DOMAIN = "wt101_climate"
PLATFORMS: list[str] = ["climate"]

# Subentry type — one thermostat per subentry under a hub config entry.
SUBENTRY_TYPE_THERMOSTAT = "thermostat"

# Hub-level (config entry) keys: platform + creds + which application to use.
CONF_PLATFORM_TYPE = "platform_type"

CONF_TTN_BASE_URL = "ttn_base_url"
CONF_TTN_APPLICATION_ID = "ttn_application_id"
CONF_TTN_API_KEY = "ttn_api_key"

CONF_CS_BASE_URL = "cs_base_url"
CONF_CS_API_TOKEN = "cs_api_token"
CONF_CS_APPLICATION_ID = "cs_application_id"
CONF_CS_APPLICATION_NAME = "cs_application_name"

# Subentry-level (per thermostat) keys.
CONF_CURRENT_TEMP_SENSOR = "current_temp_sensor"
CONF_TARGET_TEMP_SENSOR = "target_temp_sensor"
CONF_FPORT = "fport"
CONF_MIN_TEMP = "min_temp"
CONF_MAX_TEMP = "max_temp"
CONF_TOLERANCE = "tolerance"
CONF_TTN_DEVICE_ID = "ttn_device_id"
CONF_CS_DEV_EUI = "cs_dev_eui"

# Platform identifiers
PLATFORM_TTN = "ttn"
PLATFORM_CHIRPSTACK = "chirpstack"

# Defaults (PDF section 5.2 — default port 85, target temp range 5–35°C)
DEFAULT_FPORT = 85
DEFAULT_MIN_TEMP = 5.0
DEFAULT_MAX_TEMP = 30.0
DEFAULT_TOLERANCE = 0.5
DEFAULT_TTN_BASE_URL = "https://eu1.cloud.thethings.network"
DEFAULT_CS_BASE_URL = "http://localhost:8080"

# Substring matches used to highlight WT101 entries in discovery dropdowns.
WT101_BRAND_KEYS: tuple[str, ...] = ("milesight",)
WT101_MODEL_KEYS: tuple[str, ...] = ("wt101",)
