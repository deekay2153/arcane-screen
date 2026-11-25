"""Set-up wizard: the screen's address -> (Hikvision login) -> install PiPup -> done. Then options pick what to show."""
from __future__ import annotations

import asyncio
import secrets
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers import selector
from homeassistant.helpers.network import NoURLAvailableError, get_url

from .const import (
    CONF_ADB_PORT,
    CONF_HA_URL,
    CONF_HIK_PASSWORD,
    CONF_HIK_USER,
    CONF_HOST,
    CONF_KEY,
    CONF_NAME,
    CONF_PLATFORM,
    DEFAULT_ADB_PORT,
    DEFAULT_HEIGHT,
    DEFAULT_RESTART_TIME,
    DEFAULT_RETURN_MIN,
    DEFAULT_WIDTH,
    DOMAIN,
    MAX_BUTTONS,
    MAX_CHIPS,
    OPT_BUTTONS,
    OPT_BUTTONS_WHEN,
    OPT_CALENDARS,
    OPT_CALL,
    OPT_CHIPS,
    OPT_CLOCK_24,
    OPT_OPEN_ENTRANCES,
    OPT_HEIGHT,
    OPT_RESTART_TIME,
    OPT_RETURN_MIN,
    OPT_WEATHER,
    OPT_WEATHER_HOURLY,
    OPT_WIDTH,
    PLATFORM_ANDROID,
    PLATFORM_HIKVISION,
)
from .device import Screen, ScreenError


def _default_url(hass) -> str:
    try:
        return get_url(hass, allow_internal=True, allow_external=False, allow_ip=True, prefer_external=False)
    except NoURLAvailableError:
        return "http://homeassistant.local:8123"


class ArcaneScreenFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self.data: dict[str, Any] = {}
        self._task: asyncio.Task | None = None
        self._error: str | None = None

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            await self.async_set_unique_id(user_input[CONF_HOST].strip())
            self._abort_if_unique_id_configured()
            self.data = {**user_input, CONF_HOST: user_input[CONF_HOST].strip()}
            if user_input[CONF_PLATFORM] == PLATFORM_HIKVISION:
                return await self.async_step_hikvision()
            return await self.async_step_install()
        schema = vol.Schema(
            {
                vol.Required(CONF_NAME, default="Intercom screen"): str,
                vol.Required(CONF_HOST): str,
                vol.Required(CONF_PLATFORM, default=PLATFORM_HIKVISION): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=[PLATFORM_HIKVISION, PLATFORM_ANDROID], translation_key="platform"
                    )
                ),
                vol.Required(CONF_ADB_PORT, default=DEFAULT_ADB_PORT): vol.Coerce(int),
                vol.Required(CONF_HA_URL, default=_default_url(self.hass)): str,
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema)

    async def async_step_hikvision(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            screen = Screen(self.hass, self.data[CONF_HOST], self.data[CONF_ADB_PORT], user_input[CONF_HIK_USER], user_input[CONF_HIK_PASSWORD])
            Screen._refused.pop(self.data[CONF_HOST], None)    # a person typing a login again: one fresh attempt
            try:
                ok = await screen.hik_adb_on()
            except ScreenError:
                errors["base"] = "hik_auth"
            else:
                if ok:
                    self.data.update(user_input)
                    return await self.async_step_install()
                errors["base"] = "hik_unreachable"
        schema = vol.Schema(
            {
                vol.Required(CONF_HIK_USER, default="admin"): str,
                vol.Required(CONF_HIK_PASSWORD): selector.TextSelector(
                    selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
                ),
            }
        )
        return self.async_show_form(step_id="hikvision", data_schema=schema, errors=errors)

    async def async_step_install(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if self._task is None:
            screen = Screen(
                self.hass, self.data[CONF_HOST], self.data[CONF_ADB_PORT], self.data.get(CONF_HIK_USER), self.data.get(CONF_HIK_PASSWORD)
            )
            self._task = self.hass.async_create_task(screen.install_pipup())
        if not self._task.done():
            return self.async_show_progress(step_id="install", progress_action="installing", progress_task=self._task)
        try:
            self._task.result()
        except ScreenError as err:
            self._error = str(err)
            self._task = None
            return self.async_show_progress_done(next_step_id="failed")
        except Exception as err:  # noqa: BLE001 - show anything unexpected rather than a spinner forever
            self._error = f"Unexpected error: {err}"
            self._task = None
            return self.async_show_progress_done(next_step_id="failed")
        self._task = None
        return self.async_show_progress_done(next_step_id="finish")

    async def async_step_failed(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return await self.async_step_install()
        return self.async_show_form(
            step_id="failed", data_schema=vol.Schema({}), description_placeholders={"error": self._error or ""}
        )

    async def async_step_finish(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        data = {k: v for k, v in self.data.items() if k != CONF_NAME}
        data[CONF_KEY] = secrets.token_urlsafe(24)
        return self.async_create_entry(
            title=self.data[CONF_NAME],
            data=data,
            options={OPT_RESTART_TIME: DEFAULT_RESTART_TIME, OPT_RETURN_MIN: DEFAULT_RETURN_MIN},
        )

    @staticmethod
    @callback
    def async_get_options_flow(entry: ConfigEntry) -> OptionsFlow:
        return ArcaneScreenOptions()


def _entities(domains: list[str] | None = None, multiple: bool = False) -> selector.EntitySelector:
    cfg: dict[str, Any] = {"multiple": multiple}
    if domains:
        cfg["domain"] = domains
    return selector.EntitySelector(selector.EntitySelectorConfig(**cfg))


class ArcaneScreenOptions(OptionsFlow):
    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            if len(user_input.get(OPT_CHIPS, [])) > MAX_CHIPS:
                errors[OPT_CHIPS] = "too_many_chips"
            elif len(user_input.get(OPT_BUTTONS, [])) > MAX_BUTTONS:
                errors[OPT_BUTTONS] = "too_many_buttons"
            else:
                return self.async_create_entry(data=user_input)
        o = self.config_entry.options

        def opt(key: str, default: Any = None) -> Any:
            v = o.get(key, default)
            return {"suggested_value": v} if v not in (None, "", []) else {}

        schema = vol.Schema(
            {
                vol.Optional(OPT_WEATHER, description=opt(OPT_WEATHER)): _entities(["weather"]),
                vol.Optional(OPT_WEATHER_HOURLY, description=opt(OPT_WEATHER_HOURLY)): _entities(["weather"]),
                vol.Optional(OPT_CALENDARS, description=opt(OPT_CALENDARS)): _entities(["calendar"], True),
                vol.Optional(OPT_CHIPS, description=opt(OPT_CHIPS)): _entities(None, True),
                vol.Optional(OPT_BUTTONS, description=opt(OPT_BUTTONS)): _entities(
                    ["light", "switch", "fan", "input_boolean", "script", "scene"], True
                ),
                vol.Optional(OPT_BUTTONS_WHEN, description=opt(OPT_BUTTONS_WHEN)): _entities(["input_boolean", "binary_sensor", "switch"]),
                vol.Optional(OPT_CALL, description=opt(OPT_CALL)): _entities(),
                vol.Required(OPT_CLOCK_24, default=o.get(OPT_CLOCK_24, False)): bool,
                vol.Required(OPT_OPEN_ENTRANCES, default=o.get(OPT_OPEN_ENTRANCES, False)): bool,
                vol.Required(OPT_RETURN_MIN, default=o.get(OPT_RETURN_MIN, DEFAULT_RETURN_MIN)): vol.All(vol.Coerce(int), vol.Range(min=1, max=240)),
                vol.Required(OPT_RESTART_TIME, default=o.get(OPT_RESTART_TIME, DEFAULT_RESTART_TIME)): selector.TimeSelector(),
                vol.Required(OPT_WIDTH, default=o.get(OPT_WIDTH, DEFAULT_WIDTH)): vol.Coerce(int),
                vol.Required(OPT_HEIGHT, default=o.get(OPT_HEIGHT, DEFAULT_HEIGHT)): vol.Coerce(int),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema, errors=errors)
