"""One screen: what to show, when to show it, and what the page is allowed to do."""
from __future__ import annotations

import asyncio
import hashlib
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import Event, HomeAssistant, State, callback
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.storage import Store
from homeassistant.helpers.event import (
    async_call_later,
    async_track_state_change_event,
    async_track_time_change,
    async_track_time_interval,
)
from homeassistant.util import dt as dt_util

from .const import (
    ACT_URL,
    CALL_IDLE_STATES,
    CONF_ADB_PORT,
    CONF_HA_URL,
    CONF_HIK_PASSWORD,
    CONF_HIK_USER,
    CONF_HOST,
    CONF_KEY,
    COVER_DOMAIN,
    DOMAIN,
    EVENT_NOTICE_CLEARED,
    EVENT_WELCOME_CLOSED,
    WELCOME_MAX_CAMERAS,
    DEFAULT_HEIGHT,
    DEFAULT_RESTART_TIME,
    DEFAULT_RETURN_MIN,
    DEFAULT_WIDTH,
    FEED_URL,
    MAX_BUTTONS,
    MAX_CHIPS,
    OPT_BUTTONS,
    OPT_BUTTONS_WHEN,
    OPT_CALENDARS,
    OPT_CALL,
    OPT_CHIPS,
    OPT_CLOCK_24,
    OPT_OPEN_ENTRANCES,
    ENTRANCE_CLASSES,
    OPT_HEIGHT,
    OPT_RESTART_TIME,
    OPT_RETURN_MIN,
    OPT_WEATHER,
    OPT_WEATHER_HOURLY,
    OPT_WIDTH,
    POPUP_ID,
    PRESS_DOMAINS,
    SIGNAL_STATUS,
    STATIC_URL,
    TOGGLE_DOMAINS,
)
from .device import Screen, ScreenError

# Fingerprint of the page, read once when the module loads (Home Assistant imports it off the event loop).
# The page is served with long cache headers, so its address must change whenever its contents do,
# or a screen keeps showing the old copy after an update.
PAGE_STAMP = hashlib.sha256((Path(__file__).parent / "www" / "screen.html").read_bytes()).hexdigest()[:12]
from .feed import Line

_LOGGER = logging.getLogger(__name__)

PUSH_DELAY = 0.5
WATCHDOG = timedelta(seconds=30)
CALL_END_HOLD = 5
NO_LISTENER_GRACE = timedelta(seconds=25)
SHOW_MIN_GAP = timedelta(seconds=60)
BANNER_MAX = 120


def _num(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


class ScreenManager:
    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        d = entry.data
        self.key: str = d[CONF_KEY]
        self.screen = Screen(hass, d[CONF_HOST], d.get(CONF_ADB_PORT, 5555), d.get(CONF_HIK_USER), d.get(CONF_HIK_PASSWORD))
        self.line = Line()
        self.status = "starting"
        self.last_restart: datetime | None = None
        self.last_error: str | None = None
        self.in_call = False
        self.hidden_until: datetime | None = None
        self._unsubs: list = []
        self._push_handle = None
        self._call_handle = None
        self._zero_since: datetime | None = dt_util.utcnow()
        # Notices stay on the screen until cleared, and are kept across restarts.
        self.notices: dict[str, dict[str, Any]] = {}
        self._store: Store = Store(hass, 1, f"{DOMAIN}.notices.{entry.entry_id}")
        # Welcome home: a full-screen greeting with up to 3 cameras, shown on request and closed by time, tap or action.
        self.welcome: dict[str, Any] | None = None
        self._welcome_handle = None
        self._last_show: datetime | None = None
        self._forecast: dict[str, list] = {"hourly": [], "daily": []}
        self._events: list[dict] = []
        self._events_more = 0

    # ------------------------------------------------------------------ options
    @property
    def opts(self) -> dict[str, Any]:
        return self.entry.options

    @property
    def chips(self) -> list[str]:
        return list(self.opts.get(OPT_CHIPS, []))[:MAX_CHIPS]

    @property
    def buttons(self) -> list[str]:
        return list(self.opts.get(OPT_BUTTONS, []))[:MAX_BUTTONS]

    def page_url(self) -> str:
        base = self.entry.data[CONF_HA_URL].rstrip("/")
        eid = self.entry.entry_id
        feed = FEED_URL.format(key=self.key, screen=eid)
        act = ACT_URL.format(key=self.key, screen=eid)
        return (
            f"{base}{STATIC_URL}/screen.html?v={PAGE_STAMP}&id={POPUP_ID}"
            f"#feed={quote(feed, safe='')}&act={quote(act, safe='')}"
        )

    # ------------------------------------------------------------------ life cycle
    async def async_start(self) -> None:
        saved = await self._store.async_load()
        if isinstance(saved, dict):
            self.notices = {k: v for k, v in saved.items() if isinstance(v, dict)}
        watched = set(self.chips) | set(self.buttons) | {"sun.sun"}
        for k in (OPT_WEATHER, OPT_BUTTONS_WHEN, OPT_CALL):
            if self.opts.get(k):
                watched.add(self.opts[k])
        watched |= set(self.opts.get(OPT_CALENDARS, []))
        self._unsubs.append(async_track_state_change_event(self.hass, list(watched), self._on_state))
        self._unsubs.append(async_track_time_interval(self.hass, self._watchdog, WATCHDOG))
        self._unsubs.append(async_track_time_interval(self.hass, self._refresh_forecast, timedelta(minutes=30)))
        self._unsubs.append(async_track_time_interval(self.hass, self._refresh_events, timedelta(minutes=15)))
        hh, mm, ss = (int(x) for x in str(self.opts.get(OPT_RESTART_TIME, DEFAULT_RESTART_TIME)).split(":"))
        self._unsubs.append(async_track_time_change(self.hass, self._nightly, hour=hh, minute=mm, second=ss))
        call = self.opts.get(OPT_CALL)
        if call and (st := self.hass.states.get(call)):
            self.in_call = self._is_call(st)
        await self._refresh_forecast()
        await self._refresh_events()
        self.push()
        if self._wanted():
            # same popup id + same address: PiPup only reschedules a page that is already up, it doesn't rebuild it
            self.hass.async_create_task(self.async_show(force=True))

    async def async_stop(self) -> None:
        for u in self._unsubs:
            u()
        self._unsubs.clear()
        for h in (self._push_handle, self._call_handle, self._welcome_handle):
            if h:
                h()
        self.line.send(None)

    def _set_status(self, status: str) -> None:
        if status != self.status:
            self.status = status
        async_dispatcher_send(self.hass, SIGNAL_STATUS.format(self.entry.entry_id))

    @callback
    def listeners_changed(self) -> None:
        n = len(self.line.queues)
        self._zero_since = None if n else dt_util.utcnow()
        if n:
            self._set_status("showing")
        elif not self.in_call and not self._hidden():
            self._set_status("reconnecting")
        else:
            async_dispatcher_send(self.hass, SIGNAL_STATUS.format(self.entry.entry_id))

    # ------------------------------------------------------------------ showing / hiding
    def _hidden(self) -> bool:
        return self.hidden_until is not None and dt_util.utcnow() < self.hidden_until

    def _wanted(self) -> bool:
        return not self.in_call and not self._hidden()

    async def async_show(self, force: bool = False) -> bool:
        now = dt_util.utcnow()
        if not force and self._last_show and now - self._last_show < SHOW_MIN_GAP:
            return False
        self._last_show = now
        ok = await self.screen.show(
            self.page_url(), int(self.opts.get(OPT_WIDTH, DEFAULT_WIDTH)), int(self.opts.get(OPT_HEIGHT, DEFAULT_HEIGHT))
        )
        if not ok:
            self.last_error = "PiPup didn't answer"
            self._set_status("offline")
        return ok

    async def async_dismiss(self, minutes: float | None = None) -> None:
        if minutes:
            self.hidden_until = dt_util.utcnow() + timedelta(minutes=minutes)
        await self.screen.dismiss()
        self._set_status("in_call" if self.in_call else "hidden")

    async def _watchdog(self, _now=None) -> None:
        if self.in_call:
            return
        if self.hidden_until and not self._hidden():
            self.hidden_until = None
            await self.async_show(force=True)
            return
        if self._hidden():
            return
        if not self.line.queues and self._zero_since and dt_util.utcnow() - self._zero_since > NO_LISTENER_GRACE:
            await self.async_show()

    # ------------------------------------------------------------------ calls
    def _is_call(self, st: State | None) -> bool:
        return st is not None and str(st.state).lower() not in CALL_IDLE_STATES

    @callback
    def _call_changed(self, st: State | None) -> None:
        busy = self._is_call(st)
        if self._call_handle:
            self._call_handle()
            self._call_handle = None
        if busy and not self.in_call:
            self.in_call = True
            self.hass.async_create_task(self.async_dismiss())
        elif not busy and self.in_call:
            # back a few seconds after the call screen has gone, so it never flickers over a call that resumes
            @callback
            def done(_now):
                self._call_handle = None
                self.in_call = False
                if not self._hidden():
                    self.hass.async_create_task(self.async_show(force=True))

            self._call_handle = async_call_later(self.hass, CALL_END_HOLD, done)

    # ------------------------------------------------------------------ nightly PiPup restart
    async def _nightly(self, _now=None) -> None:
        if self.in_call:
            _LOGGER.info("%s: nightly PiPup restart skipped (call in progress)", self.entry.title)
            return
        await self.async_restart_pipup()

    async def async_restart_pipup(self) -> None:
        try:
            await self.screen.restart_pipup()
        except ScreenError as err:
            self.last_error = str(err)
            _LOGGER.warning("%s: PiPup restart failed: %s", self.entry.title, err)
            self._set_status("offline")
            return
        self.last_restart = dt_util.utcnow()
        for _ in range(20):
            await asyncio.sleep(1)
            if await self.screen.pipup_state() is not None:
                break
        if self._wanted():
            await self.async_show(force=True)

    # ------------------------------------------------------------------ data
    @callback
    def _on_state(self, event: Event) -> None:
        eid = event.data["entity_id"]
        new = event.data.get("new_state")
        if eid == self.opts.get(OPT_CALL):
            self._call_changed(new)
            return
        if eid == self.opts.get(OPT_WEATHER):
            self.hass.async_create_task(self._refresh_forecast())
        if eid in self.opts.get(OPT_CALENDARS, []):
            self.hass.async_create_task(self._refresh_events())
        self.push()

    @callback
    def push(self) -> None:
        if self._push_handle:
            return

        @callback
        def go(_now):
            self._push_handle = None
            self.line.publish(self.build())

        self._push_handle = async_call_later(self.hass, PUSH_DELAY, go)

    async def _refresh_forecast(self, _now=None) -> None:
        w = self.opts.get(OPT_WEATHER)
        if not w or not self.hass.services.has_service("weather", "get_forecasts"):
            return
        for kind in ("hourly", "daily"):
            src = (self.opts.get(OPT_WEATHER_HOURLY) or w) if kind == "hourly" else w
            try:
                res = await self.hass.services.async_call(
                    "weather", "get_forecasts", {"entity_id": src, "type": kind}, blocking=True, return_response=True
                )
                self._forecast[kind] = (res or {}).get(src, {}).get("forecast", []) or []
            except Exception as err:  # noqa: BLE001 - some weather entities don't support every type
                _LOGGER.debug("forecast %s for %s: %s", kind, src, err)
                self._forecast[kind] = []
        self.push()

    async def _refresh_events(self, _now=None) -> None:
        cals = self.opts.get(OPT_CALENDARS, [])
        if not cals:
            return
        start = dt_util.now()
        end = start + timedelta(days=7)
        out: list[dict] = []
        try:
            res = await self.hass.services.async_call(
                "calendar",
                "get_events",
                {"entity_id": cals, "start_date_time": start.isoformat(), "end_date_time": end.isoformat()},
                blocking=True,
                return_response=True,
            )
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("calendar events: %s", err)
            return
        for cal in (res or {}).values():
            for e in cal.get("events", []):
                out.append({"start": str(e.get("start")), "title": e.get("summary") or ""})
        out.sort(key=lambda e: e["start"])
        self._events, self._events_more = out[:12], max(0, len(out) - 12)
        self.push()

    def _phase(self) -> str:
        sun = self.hass.states.get("sun.sun")
        if not sun:
            return "night"
        elev = _num(sun.attributes.get("elevation")) or 0
        rising = bool(sun.attributes.get("rising"))
        if elev < -2:
            return "night"
        if elev < 10:
            return "dawn" if rising else "dusk"
        return "day"

    def _weather(self) -> dict[str, Any] | None:
        w = self.opts.get(OPT_WEATHER)
        st = self.hass.states.get(w) if w else None
        if not st or st.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            return None
        a = st.attributes
        daily = [
            {
                "d": str(f.get("datetime", ""))[:10],
                "low": _num(f.get("templow")),
                "high": _num(f.get("temperature")),
                "cond": f.get("condition"),
                "chance": _num(f.get("precipitation_probability")),
                "mm": _num(f.get("precipitation")),
            }
            for f in self._forecast["daily"][:7]
        ]
        hourly = [
            {
                "t": dt_util.as_local(dt_util.parse_datetime(str(f.get("datetime")))).isoformat()
                if dt_util.parse_datetime(str(f.get("datetime")))
                else str(f.get("datetime")),
                "temp": _num(f.get("temperature")),
                "cond": f.get("condition"),
                "chance": _num(f.get("precipitation_probability")),
            }
            for f in self._forecast["hourly"][:12]
        ]
        today: dict[str, Any] = {}
        if daily and daily[0]["d"] == dt_util.now().date().isoformat():
            today, daily = daily[0], daily[1:]
        return {
            "now": {
                "temp": _num(a.get("temperature")),
                "cond": st.state,
                "hum": _num(a.get("humidity")),
                "wind": _num(a.get("wind_speed")),
                "wind_unit": a.get("wind_speed_unit") or "",
            },
            "today": {"low": today.get("low"), "high": today.get("high"), "chance": today.get("chance")},
            "hourly": hourly,
            "daily": daily,
            "unit": a.get("temperature_unit") or "°",
        }

    def build(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "v": 1,
            "phase": self._phase(),
            "clock24": bool(self.opts.get(OPT_CLOCK_24, False)),
            "events": self._events,
            "events_more": self._events_more,
            "chips": [chip_for(self.hass.states.get(e), e, i, bool(self.opts.get(OPT_OPEN_ENTRANCES, False))) for i, e in enumerate(self.chips)],
        }
        if wx := self._weather():
            data.update(wx)
        when = self.opts.get(OPT_BUTTONS_WHEN)
        show_buttons = not when or (self.hass.states.get(when) and self.hass.states.get(when).state == "on")
        if show_buttons and self.buttons:
            data["buttons"] = [button_for(self.hass.states.get(e), e, i) for i, e in enumerate(self.buttons)]
        if self.notices:
            data["notices"] = list(self.notices.values())
        if self.welcome:
            w = self.welcome
            data["welcome"] = {
                "id": w["id"], "name": w["name"], "message": w["message"], "after": w["after"],
                "pulse_until": w["pulse_until"].isoformat() if w["pulse_until"] else None,
                "note": w["note"],
                "cams": [{"i": i, "name": _short(self._cam_name(e))} for i, e in enumerate(w["cameras"])],
            }
        return data

    def _cam_name(self, eid: str) -> str:
        st = self.hass.states.get(eid)
        return (st.name if st else eid.split(".", 1)[-1]).upper()

    # ------------------------------------------------------------------ taps from the page
    async def async_page_action(self, body: dict[str, Any]) -> bool:
        kind = body.get("kind")
        if kind == "note":
            n = self.notices.get(str(body.get("id")))
            if not n or not n.get("tap_clears"):
                return False
            self.clear_notice(n["id"], by="tap")
            return True
        if kind == "welcome":
            if not self.welcome:
                return False
            self.close_welcome(by="tap")
            return True
        if kind == "exit":
            minutes = float(self.opts.get(OPT_RETURN_MIN, DEFAULT_RETURN_MIN))
            await self.async_dismiss(minutes)
            return True
        try:
            i = int(body.get("i"))
        except (TypeError, ValueError):
            return False
        if kind == "chip" and 0 <= i < len(self.chips):
            eid = self.chips[i]
            allowed = {a["a"] for a in actions_for(self.hass.states.get(eid), eid, bool(self.opts.get(OPT_OPEN_ENTRANCES, False)))}
            act = body.get("a")
            if act not in allowed:
                return False
        elif kind == "button" and 0 <= i < len(self.buttons):
            eid = self.buttons[i]
            if eid.split(".")[0] not in BUTTON_DOMAINS:
                return False
            act = "press" if eid.split(".")[0] in PRESS_DOMAINS else "toggle"
            when = self.opts.get(OPT_BUTTONS_WHEN)
            if when and not (self.hass.states.get(when) and self.hass.states.get(when).state == "on"):
                return False
        else:
            return False
        await call_action(self.hass, eid, act)
        return True

    # ------------------------------------------------------------------ notices
    @callback
    def set_notice(
        self, nid: str, text: str, icon: str, level: str, tint: bool, tap_clears: bool,
        banner_title: str | None = None, banner_message: str | None = None,
    ) -> None:
        if nid not in self.notices and (banner_title or banner_message):
            # The banner only comes with a NEW notice, so an automation can re-send the notice as often as it likes.
            self.banner(banner_message or banner_title or "", banner_title if banner_message else None, icon, 30, level)
        self.notices[nid] = {"id": nid, "text": text, "icon": icon, "level": level, "tint": tint, "tap_clears": tap_clears}
        self._notices_changed()

    @callback
    def clear_notice(self, nid: str | None, by: str = "action") -> None:
        if nid is None:
            gone = list(self.notices)
            self.notices.clear()
        elif self.notices.pop(nid, None) is None:
            return
        else:
            gone = [nid]
        for g in gone:
            # Blueprints listen for this, e.g. so a tapped-away "bins out" notice doesn't come back red later.
            self.hass.bus.async_fire(EVENT_NOTICE_CLEARED, {"id": g, "screen": self.entry.title, "entry_id": self.entry.entry_id, "by": by})
        self._notices_changed()

    @callback
    def _notices_changed(self) -> None:
        self._store.async_delay_save(lambda: dict(self.notices), 2)
        self.push()
        async_dispatcher_send(self.hass, SIGNAL_STATUS.format(self.entry.entry_id))   # the status sensor lists them

    # ------------------------------------------------------------------ welcome home
    async def async_welcome(
        self, name: str, message: str, after: str, pulse_seconds: int, cameras: list[str], seconds: int, note: str
    ) -> None:
        now = dt_util.utcnow()
        if self._welcome_handle:
            self._welcome_handle()
        self.welcome = {
            "id": int(now.timestamp() * 1000), "name": name, "message": message, "after": after, "note": note,
            "pulse_until": now + timedelta(seconds=pulse_seconds) if pulse_seconds else None,
            "cameras": list(cameras)[:WELCOME_MAX_CAMERAS],
        }

        @callback
        def _timeout(_now) -> None:
            self._welcome_handle = None
            self.close_welcome(by="timeout")

        self._welcome_handle = async_call_later(self.hass, seconds, _timeout)
        self.push()
        # Someone is arriving: bring the dashboard back if it was put away with the exit button.
        if self._hidden() and not self.in_call:
            self.hidden_until = None
            await self.async_show(force=True)

    @callback
    def close_welcome(self, by: str = "action") -> None:
        if self._welcome_handle:
            self._welcome_handle()
            self._welcome_handle = None
        if not self.welcome:
            return
        self.welcome = None
        self.hass.bus.async_fire(EVENT_WELCOME_CLOSED, {"screen": self.entry.title, "entry_id": self.entry.entry_id, "by": by})
        self.push()

    def welcome_camera(self, i: int) -> str | None:
        """Cameras can only be watched through the screen's address while the welcome screen is up."""
        if not self.welcome or not 0 <= i < len(self.welcome["cameras"]):
            return None
        return self.welcome["cameras"][i]

    # ------------------------------------------------------------------ banners
    @callback
    def banner(self, message: str, title: str | None, icon: str | None, seconds: int, tone: str) -> None:
        self.line.event(
            "banner",
            {"title": title or "", "message": message, "icon": icon or "bell", "seconds": min(int(seconds), BANNER_MAX), "tone": tone},
        )


# ---------------------------------------------------------------------- chips / buttons
BUTTON_DOMAINS = ("light", "switch", "fan", "input_boolean", "script", "scene", "button", "input_button")
def _short(name: str) -> str:
    return name if len(name) <= 22 else name[:21] + "…"


def actions_for(st: State | None, eid: str, entrances: bool = False) -> list[dict[str, str]]:
    if st is None or st.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
        return []
    if eid.split(".")[0] == COVER_DOMAIN and st.attributes.get("device_class") in ENTRANCE_CLASSES and not entrances:
        return []   # view-only unless "Let chips open gates, garage doors and doors" is on
    domain = eid.split(".")[0]
    s = st.state
    if domain == "media_player":
        return [{"a": "playpause", "label": "Pause" if s == "playing" else "Play"}]
    if domain in TOGGLE_DOMAINS:
        return [{"a": "off", "label": "Turn off"}] if s not in ("off",) else [{"a": "on", "label": "Turn on"}]
    if domain == COVER_DOMAIN:
        acts = [{"a": "open", "label": "Open"}, {"a": "close", "label": "Close"}]
        if s in ("opening", "closing"):
            acts.insert(1, {"a": "stop", "label": "Stop"})
        return acts
    if domain in PRESS_DOMAINS:
        return [{"a": "press", "label": "Run" if domain == "script" else "Activate" if domain == "scene" else "Press"}]
    return []


SERVICES = {
    "on": "turn_on",
    "off": "turn_off",
    "open": "open_cover",
    "close": "close_cover",
    "stop": "stop_cover",
    "playpause": "media_play_pause",
}


async def call_action(hass: HomeAssistant, eid: str, act: str) -> None:
    domain = eid.split(".")[0]
    if act == "toggle":
        await hass.services.async_call("homeassistant", "toggle", {"entity_id": eid}, blocking=False)
    elif act == "press":
        svc = {"script": "turn_on", "scene": "turn_on", "button": "press", "input_button": "press"}[domain]
        await hass.services.async_call(domain, svc, {"entity_id": eid}, blocking=False)
    else:
        await hass.services.async_call(domain, SERVICES[act], {"entity_id": eid}, blocking=False)


def chip_for(st: State | None, eid: str, i: int, entrances: bool = False) -> dict[str, Any]:
    domain = eid.split(".")[0]
    if st is None:
        return {"i": i, "name": eid, "icon": "dot", "text": "–", "tone": "off", "state": "missing"}
    a = st.attributes
    dc = a.get("device_class")
    name = _short(st.name)
    s = st.state
    chip: dict[str, Any] = {
        "i": i,
        "name": name,
        "icon": "dot",
        "text": name,
        "tone": "",
        "state": s.replace("_", " "),
        "changed": st.last_changed.isoformat(),
        "actions": actions_for(st, eid, entrances),
    }
    off = s in (STATE_UNAVAILABLE, STATE_UNKNOWN)
    if domain == "sensor":
        unit = a.get("unit_of_measurement") or ""
        v = _num(s)
        chip["icon"] = {"temperature": "thermo", "humidity": "drop", "power": "bolt", "energy": "bolt", "battery": "battery",
                        "illuminance": "sun", "carbon_dioxide": "leaf", "pm25": "leaf"}.get(dc, "dot")
        if v is not None:
            if unit == "W" and abs(v) >= 1000:
                v, unit = v / 1000, "kW"
            txt = f"{v:.1f}" if (unit in ("°C", "°F", "kW", "kWh") or dc == "temperature") else f"{v:.0f}" if abs(v) >= 10 else f"{v:.1f}".rstrip("0").rstrip(".")
            chip["text"] = f"{txt}{'' if unit in ('°C', '°F') else ' '}{'°' if unit in ('°C', '°F') else unit}".strip()
        else:
            chip["text"] = "–" if off else s
    elif domain == "binary_sensor":
        chip["icon"] = {"door": "door", "garage_door": "garage", "window": "window", "opening": "door", "motion": "person",
                        "occupancy": "person", "presence": "person", "moisture": "drop", "smoke": "alert", "gas": "alert"}.get(dc, "dot")
        if s == "on":
            chip["tone"] = "warn" if dc in ("door", "garage_door", "window", "opening", "moisture", "smoke", "gas", "problem", "safety") else "on"
    elif domain == "alarm_control_panel":
        chip["icon"] = "shieldon" if s.startswith("armed") or s == "triggered" else "shield"
        chip["tone"] = "alert" if s == "triggered" else "on" if s.startswith("armed") else "warn" if s in ("arming", "pending") else ""
        # Panel names are often long model strings: the state says more.
        chip["text"] = "–" if off else {"armed_home": "Armed home", "armed_away": "Armed away", "armed_night": "Armed night",
                                        "armed_vacation": "Armed away", "armed_custom_bypass": "Armed"}.get(s, s.replace("_", " ").capitalize())
    elif domain == "cover":
        moving = s in ("opening", "closing")
        if dc == "gate":
            chip["icon"] = "gate"
            chip["gate"] = {"open": "open", "opening": "opening", "closing": "closing"}.get(s, "closed")
        elif dc == "garage":
            chip["icon"] = "garageopen" if s == "open" else "garage"
        else:
            chip["icon"] = "blinds"
        chip["tone"] = "move" if moving else "warn" if (s == "open" and dc in ("garage", "gate", "door")) else ""
    elif domain == "lock":
        chip["icon"] = "lock" if s == "locked" else "unlock"
        chip["tone"] = "warn" if s in ("unlocked", "open", "jammed") else ""
    elif domain in ("light", "switch", "fan", "input_boolean", "humidifier"):
        chip["icon"] = {"light": "bulb", "fan": "fan", "humidifier": "drop"}.get(domain, "power")
        chip["tone"] = "on" if s == "on" else ""
    elif domain == "climate":
        chip["icon"] = "flame" if s == "heat" else "snow"
        chip["tone"] = "on" if s not in ("off",) and not off else ""
    elif domain in ("person", "device_tracker"):
        chip["icon"] = "person"
        chip["tone"] = "on" if s == "home" else ""
    elif domain == "media_player":
        chip["icon"] = "note"
        chip["tone"] = "on" if s == "playing" else ""
    elif domain in PRESS_DOMAINS:
        chip["icon"] = "play"
    else:
        chip["text"] = f"{name}"
    if off:
        chip["tone"] = "off"
    return chip


def button_for(st: State | None, eid: str, i: int) -> dict[str, Any]:
    domain = eid.split(".")[0]
    return {
        "i": i,
        "name": _short(st.name) if st else eid,
        "on": bool(st and st.state == "on"),
        "icon": "bulb" if domain == "light" else "fan" if domain == "fan" else "play" if domain in PRESS_DOMAINS else "power",
    }
