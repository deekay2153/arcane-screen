"""Constants for Arcane Screen."""
from __future__ import annotations

DOMAIN = "arcane_screen"
VERSION = "0.1.1"

# The PiPup build we install and test against. Downloaded from its own GitHub release at setup time (never bundled).
# mhoogenbosch/PiPup is MIT for its own changes; see README "Credits".
PIPUP_PACKAGE = "nl.rogro82.pipup"
PIPUP_VERSION = "0.31.0"
PIPUP_APK_URL = "https://github.com/mhoogenbosch/PiPup/releases/download/v0.31.0/PiPup.apk"
PIPUP_APK_SHA256 = "43f89413fe49435796ac288f25d45f895b149ad2b5c426b0b797bc2f906f7e3e"
PIPUP_PORT = 7979
POPUP_ID = "arcane_screen"

# Config entry data
CONF_HOST = "host"
CONF_NAME = "name"
CONF_PLATFORM = "platform"          # "hikvision" or "android"
CONF_HIK_USER = "hik_username"
CONF_HIK_PASSWORD = "hik_password"
CONF_ADB_PORT = "adb_port"
CONF_HA_URL = "ha_url"              # the address the SCREEN uses to reach Home Assistant
CONF_KEY = "feed_key"               # generated, never shown

PLATFORM_HIKVISION = "hikvision"
PLATFORM_ANDROID = "android"

# Options
OPT_WEATHER = "weather"
OPT_WEATHER_HOURLY = "weather_hourly"  # optional: some providers (BOM) keep hourly on a separate entity
OPT_CALENDARS = "calendars"
OPT_CHIPS = "chips"
OPT_BUTTONS = "buttons"
OPT_BUTTONS_WHEN = "buttons_when"   # optional on/off entity: buttons only while it is on (e.g. babysitter mode)
OPT_CALL = "call_entity"            # optional entity that is busy while the intercom is ringing / in a call
OPT_RESTART_TIME = "restart_time"
OPT_RETURN_MIN = "return_minutes"
OPT_CLOCK_24 = "clock_24h"
OPT_OPEN_ENTRANCES = "open_entrances"  # let chips open gates / garage doors / doors (off by default)
ENTRANCE_CLASSES = ("gate", "garage", "door")
OPT_WIDTH = "width"
OPT_HEIGHT = "height"

DEFAULT_ADB_PORT = 5555
DEFAULT_RESTART_TIME = "03:30:00"
DEFAULT_RETURN_MIN = 5
DEFAULT_WIDTH = 1024
DEFAULT_HEIGHT = 600

MAX_CHIPS = 8
MAX_BUTTONS = 6

# States of the call entity that mean "no call".
CALL_IDLE_STATES = {"idle", "off", "closed", "unavailable", "unknown", "none", "", "0", "false"}

# Domains a tap on the screen may act on. Locks and alarm panels are view-only on purpose.
TOGGLE_DOMAINS = {"light", "switch", "fan", "input_boolean", "climate", "humidifier", "media_player"}
PRESS_DOMAINS = {"script", "scene", "button", "input_button"}
COVER_DOMAIN = "cover"

STATIC_URL = "/arcane_screen_static"
FEED_URL = "/api/arcane_screen/feed/{key}/{screen}"
ACT_URL = "/api/arcane_screen/act/{key}/{screen}"
CAM_URL = "/api/arcane_screen/cam/{key}/{screen}/{i}"

EVENT_NOTICE_CLEARED = "arcane_screen_notice_cleared"
EVENT_WELCOME_CLOSED = "arcane_screen_welcome_closed"
WELCOME_MAX_CAMERAS = 3
SIGNAL_STATUS = "arcane_screen_status_{}"
