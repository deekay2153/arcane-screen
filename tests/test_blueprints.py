"""Every blueprint must load as a working automation; two are run end to end."""
from __future__ import annotations

import asyncio

import shutil
from datetime import timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util

from custom_components.arcane_screen.const import DOMAIN

SRC = Path(__file__).parent.parent / "blueprints" / "automation" / "arcane_screen"
INPUTS = {
    "bin_night.yaml": {"collection_weekdays": ["0"]},
    "washing_rain.yaml": {"washing_out": "input_boolean.washing", "weather": "weather.home"},
    "arrived_home.yaml": {"people": ["person.alex"], "away_minutes": 0},
    "left_open.yaml": {"target": "binary_sensor.gate"},
    "started_finished.yaml": {"thing": "switch.back_lawn"},
    "parcel_delivered.yaml": {"source": "sensor.parcels"},
    "welcome_home.yaml": {"people": ["person.alex"], "cameras": ["camera.front_gate"], "close_entity": "cover.garage", "away_minutes": 0},
}


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


@pytest.fixture(autouse=True)
def no_screen():
    """No real screen: every network call to it is stood in for (the nightly restart fires when the clock jumps ahead)."""
    dev = "custom_components.arcane_screen.device.Screen."
    with patch(dev + "show", AsyncMock(return_value=True)), patch(dev + "dismiss", AsyncMock(return_value=True)), patch(
        dev + "pipup_state", AsyncMock(return_value={})
    ), patch(dev + "adb", AsyncMock(return_value=[])), patch(dev + "restart_pipup", AsyncMock()):
        yield


async def _setup(hass: HomeAssistant):
    dst = Path(hass.config.path("blueprints", "automation", "arcane_screen"))
    dst.mkdir(parents=True, exist_ok=True)
    for f in SRC.glob("*.yaml"):
        shutil.copy(f, dst / f.name)
    await async_setup_component(hass, "http", {})
    if True:
        entry = MockConfigEntry(domain=DOMAIN, title="Hall", data={"host": "10.0.0.9", "platform": "android", "adb_port": 5555,
                                                                   "ha_url": "http://10.0.0.2:8123", "feed_key": "k" * 32}, options={})
        entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
    autos = [{"id": n, "alias": n, "use_blueprint": {"path": f"arcane_screen/{n}", "input": i}} for n, i in INPUTS.items()]
    assert await async_setup_component(hass, "automation", {"automation": autos})
    await hass.async_block_till_done()
    return hass.data[DOMAIN][entry.entry_id]


@pytest.mark.parametrize("expected_lingering_timers", [True])  # the washing blueprint's 10-minute check
async def test_blueprints_load_and_run(hass: HomeAssistant) -> None:
    hass.states.async_set("person.alex", "not_home", {"friendly_name": "Alex"})
    hass.states.async_set("switch.back_lawn", "off", {"friendly_name": "Back lawn"})
    mgr = await _setup(hass)
    states = hass.states.async_all("automation")
    assert len(states) == len(INPUTS)
    assert all(s.state == "on" for s in states), [(s.entity_id, s.state) for s in states]

    async def settle():
        for _ in range(200):
            await asyncio.sleep(0)

    banners = []
    with patch.object(type(mgr), "banner", lambda self, *a: banners.append(a)):
        hass.states.async_set("person.alex", "home", {"friendly_name": "Alex"})
        await settle()
        hass.states.async_set("switch.back_lawn", "on", {"friendly_name": "Back lawn"})
        await settle()
        hass.states.async_set("switch.back_lawn", "off", {"friendly_name": "Back lawn"})
        await settle()
    titles = [b[1] for b in banners]
    assert titles == ["Alex is home", "Back lawn started", "Back lawn finished"], titles
    await hass.services.async_call("automation", "turn_off", {"entity_id": "all"}, blocking=True)
    await hass.config_entries.async_unload(mgr.entry.entry_id)
    await hass.async_block_till_done()


@pytest.mark.parametrize("expected_lingering_timers", [True])
async def test_bin_night(hass: HomeAssistant, freezer) -> None:
    from pytest_homeassistant_custom_component.common import async_fire_time_changed

    tz = dt_util.get_default_time_zone()
    sunday = dt_util.now().replace(hour=17, minute=59, second=0, microsecond=0)
    sunday += timedelta(days=(6 - sunday.weekday()) % 7)          # collection is Monday ("0")
    freezer.move_to(sunday)
    mgr = await _setup(hass)

    async def settle():
        # Not block_till_done: a bin night waiting for 8 pm never "finishes".
        for _ in range(200):
            await asyncio.sleep(0)

    async def at(h, m):
        t = sunday.replace(hour=h, minute=m, second=1)
        freezer.move_to(t)
        async_fire_time_changed(hass, t)
        await settle()

    await at(18, 0)
    assert mgr.notices["bin_night"]["level"] == "warn" and mgr.notices["bin_night"]["tint"]
    await at(20, 0)
    assert mgr.notices["bin_night"]["level"] == "alert"
    mgr.clear_notice("bin_night", by="tap")
    await settle()
    assert "bin_night" not in mgr.notices
    await hass.services.async_call("automation", "turn_off", {"entity_id": "all"}, blocking=True)
    await hass.config_entries.async_unload(mgr.entry.entry_id)
    await hass.async_block_till_done()


@pytest.mark.parametrize("expected_lingering_timers", [True])
async def test_bin_night_tapped_early(hass: HomeAssistant, freezer) -> None:
    from pytest_homeassistant_custom_component.common import async_fire_time_changed

    tz = dt_util.get_default_time_zone()
    sunday = dt_util.now().replace(hour=17, minute=59, second=0, microsecond=0)
    sunday += timedelta(days=(6 - sunday.weekday()) % 7)          # collection is Monday ("0")
    freezer.move_to(sunday)
    mgr = await _setup(hass)

    async def settle():
        # Not block_till_done: a bin night waiting for 8 pm never "finishes".
        for _ in range(200):
            await asyncio.sleep(0)

    async def at(h, m):
        t = sunday.replace(hour=h, minute=m, second=1)
        freezer.move_to(t)
        async_fire_time_changed(hass, t)
        await settle()

    await at(18, 0)
    assert mgr.notices["bin_night"]["level"] == "warn" and mgr.notices["bin_night"]["tint"]
    mgr.clear_notice("bin_night", by="tap")          # bins went out before 8 pm
    await settle()
    await at(20, 0)
    assert "bin_night" not in mgr.notices             # so no red
    await hass.services.async_call("automation", "turn_off", {"entity_id": "all"}, blocking=True)
    await hass.config_entries.async_unload(mgr.entry.entry_id)
    await hass.async_block_till_done()


@pytest.mark.parametrize("expected_lingering_timers", [True])
async def test_welcome_home_blueprint(hass: HomeAssistant) -> None:
    hass.states.async_set("person.alex", "not_home", {"friendly_name": "Alex"})
    hass.states.async_set("camera.front_gate", "idle", {"friendly_name": "Front gate"})
    hass.states.async_set("cover.garage", "closed", {"friendly_name": "Garage"})
    mgr = await _setup(hass)

    async def settle():
        for _ in range(200):
            await asyncio.sleep(0)

    hass.states.async_set("person.alex", "home", {"friendly_name": "Alex"})
    await settle()
    w = mgr.build()["welcome"]
    assert w["name"] == "Alex" and w["note"] == "Closes when Garage shuts" and w["cams"][0]["name"] == "FRONT GATE"
    hass.states.async_set("cover.garage", "open", {"friendly_name": "Garage"})
    await settle()
    assert "welcome" in mgr.build()
    hass.states.async_set("cover.garage", "closed", {"friendly_name": "Garage"})
    await settle()
    assert "welcome" not in mgr.build()
    await hass.services.async_call("automation", "turn_off", {"entity_id": "all"}, blocking=True)
    await hass.config_entries.async_unload(mgr.entry.entry_id)
    await hass.async_block_till_done()
