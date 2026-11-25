"""Arcane Screen: a glass-style home dashboard and notification centre for Android intercom screens, shown through
PiPup and driven by Home Assistant."""
from __future__ import annotations

import logging
from pathlib import Path

import voluptuous as vol

from homeassistant.components.http import StaticPathConfig
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.storage import Store
import homeassistant.helpers.config_validation as cv

from .const import CONF_ADB_PORT, CONF_HOST, DOMAIN, STATIC_URL
from .device import Screen, ScreenError
from .feed import ActView, CamView, FeedView
from .manager import ScreenManager

_LOGGER = logging.getLogger(__name__)
PLATFORMS = [Platform.SENSOR]
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

SCREEN = vol.Optional("screen")
BANNER_SCHEMA = vol.Schema(
    {
        SCREEN: cv.string,
        vol.Required("message"): cv.string,
        vol.Optional("title"): cv.string,
        vol.Optional("icon", default="bell"): cv.string,
        vol.Optional("seconds", default=15): vol.All(vol.Coerce(int), vol.Range(min=3, max=120)),
        vol.Optional("tone", default="info"): vol.In(["info", "good", "warn", "alert"]),
    }
)
NOTICE_SCHEMA = vol.Schema(
    {
        SCREEN: cv.string,
        vol.Required("id"): cv.slug,
        vol.Required("text"): vol.All(cv.string, vol.Length(max=40)),
        vol.Optional("icon", default="bell"): cv.string,
        vol.Optional("level", default="info"): vol.In(["info", "good", "warn", "alert"]),
        vol.Optional("tint", default=False): cv.boolean,
        vol.Optional("tap_clears", default=False): cv.boolean,
        vol.Optional("banner_title"): cv.string,
        vol.Optional("banner_message"): cv.string,
    }
)
WELCOME_SCHEMA = vol.Schema(
    {
        SCREEN: cv.string,
        vol.Required("name"): cv.string,
        vol.Optional("message", default=""): cv.string,
        vol.Optional("message_after", default=""): cv.string,
        vol.Optional("pulse_seconds", default=0): vol.All(vol.Coerce(int), vol.Range(min=0, max=600)),
        vol.Optional("cameras", default=[]): vol.All(cv.ensure_list, [cv.entity_domain("camera")], vol.Length(max=3)),
        vol.Optional("seconds", default=180): vol.All(vol.Coerce(int), vol.Range(min=10, max=1800)),
        vol.Optional("note", default=""): cv.string,
    }
)
CLEAR_NOTICE_SCHEMA = vol.Schema({SCREEN: cv.string, vol.Optional("id"): cv.slug})
TARGET_SCHEMA = vol.Schema({SCREEN: cv.string})
DISMISS_SCHEMA = vol.Schema({SCREEN: cv.string, vol.Optional("minutes"): vol.All(vol.Coerce(float), vol.Range(min=0, max=1440))})


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    hass.data.setdefault(DOMAIN, {})
    hass.http.register_view(FeedView())
    hass.http.register_view(ActView())
    hass.http.register_view(CamView())
    await hass.http.async_register_static_paths(
        [StaticPathConfig(STATIC_URL, str(Path(__file__).parent / "www"), True)]
    )

    def targets(call: ServiceCall) -> list[ScreenManager]:
        mgrs: dict[str, ScreenManager] = hass.data[DOMAIN]
        want = call.data.get("screen")
        if not want:
            return list(mgrs.values())
        if want in mgrs:
            return [mgrs[want]]
        found = [m for m in mgrs.values() if m.entry.title.lower() == str(want).lower()]
        if not found:
            raise HomeAssistantError(f"No Arcane Screen called {want}")
        return found

    async def banner(call: ServiceCall) -> None:
        for m in targets(call):
            m.banner(call.data["message"], call.data.get("title"), call.data["icon"], call.data["seconds"], call.data["tone"])

    async def notice(call: ServiceCall) -> None:
        d = call.data
        for m in targets(call):
            m.set_notice(d["id"], d["text"], d["icon"], d["level"], d["tint"], d["tap_clears"], d.get("banner_title"), d.get("banner_message"))

    async def clear_notice(call: ServiceCall) -> None:
        for m in targets(call):
            m.clear_notice(call.data.get("id"))

    async def welcome(call: ServiceCall) -> None:
        d = call.data
        for m in targets(call):
            await m.async_welcome(d["name"], d["message"], d["message_after"], d["pulse_seconds"], d["cameras"], d["seconds"], d["note"])

    async def close_welcome(call: ServiceCall) -> None:
        for m in targets(call):
            m.close_welcome()

    async def show(call: ServiceCall) -> None:
        for m in targets(call):
            m.hidden_until = None
            await m.async_show(force=True)

    async def dismiss(call: ServiceCall) -> None:
        for m in targets(call):
            await m.async_dismiss(call.data.get("minutes"))

    async def restart(call: ServiceCall) -> None:
        for m in targets(call):
            await m.async_restart_pipup()

    async def install(call: ServiceCall) -> None:
        for m in targets(call):
            try:
                await m.screen.install_pipup()
            except ScreenError as err:
                raise HomeAssistantError(str(err)) from err
            await m.async_show(force=True)

    hass.services.async_register(DOMAIN, "banner", banner, schema=BANNER_SCHEMA)
    hass.services.async_register(DOMAIN, "notice", notice, schema=NOTICE_SCHEMA)
    hass.services.async_register(DOMAIN, "clear_notice", clear_notice, schema=CLEAR_NOTICE_SCHEMA)
    hass.services.async_register(DOMAIN, "welcome", welcome, schema=WELCOME_SCHEMA)
    hass.services.async_register(DOMAIN, "close_welcome", close_welcome, schema=TARGET_SCHEMA)
    hass.services.async_register(DOMAIN, "show", show, schema=TARGET_SCHEMA)
    hass.services.async_register(DOMAIN, "dismiss", dismiss, schema=DISMISS_SCHEMA)
    hass.services.async_register(DOMAIN, "restart_pipup", restart, schema=TARGET_SCHEMA)
    hass.services.async_register(DOMAIN, "install_pipup", install, schema=TARGET_SCHEMA)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    mgr = ScreenManager(hass, entry)
    hass.data[DOMAIN][entry.entry_id] = mgr
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    await mgr.async_start()
    entry.async_on_unload(entry.add_update_listener(_reload))
    return True


async def _reload(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    mgr: ScreenManager | None = hass.data[DOMAIN].pop(entry.entry_id, None)
    if mgr:
        await mgr.async_stop()
    return ok


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Deleting the entry takes the dashboard off the screen (a reload or options change keeps it up)."""
    await Screen(hass, entry.data[CONF_HOST], entry.data.get(CONF_ADB_PORT, 5555)).dismiss()
    await Store(hass, 1, f"{DOMAIN}.notices.{entry.entry_id}").async_remove()
