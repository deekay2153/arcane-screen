"""Smoke tests: set-up, the data the page gets, the feed and tap addresses, the config flow (network patched out)."""
from __future__ import annotations

import asyncio
from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from homeassistant.util import dt as dt_util

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.setup import async_setup_component

from custom_components.arcane_screen.const import DOMAIN

KEY = "k" * 32


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


@pytest.fixture
def no_screen():
    with patch("custom_components.arcane_screen.device.Screen.show", AsyncMock(return_value=True)) as show, patch(
        "custom_components.arcane_screen.device.Screen.dismiss", AsyncMock(return_value=True)
    ), patch("custom_components.arcane_screen.device.Screen.pipup_state", AsyncMock(return_value={})):
        yield show


async def _setup(hass: HomeAssistant, options: dict) -> MockConfigEntry:
    await async_setup_component(hass, "http", {})
    await async_setup_component(hass, "input_boolean", {"input_boolean": {"lamp": {"name": "Lounge lamp"}, "sitter": {"name": "Sitter"}}})
    hass.states.async_set("sensor.power", "2400", {"unit_of_measurement": "W", "device_class": "power", "friendly_name": "Solar"})
    hass.states.async_set("lock.front", "locked", {"friendly_name": "Front door"})
    hass.states.async_set("sensor.call", "idle")
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Hall screen",
        data={"host": "10.0.0.9", "platform": "android", "adb_port": 5555, "ha_url": "http://10.0.0.2:8123", "feed_key": KEY},
        options=options,
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_build_and_show(hass: HomeAssistant, no_screen) -> None:
    entry = await _setup(
        hass,
        {"chips": ["sensor.power", "lock.front", "input_boolean.lamp"], "buttons": ["input_boolean.lamp"], "buttons_when": "input_boolean.sitter", "call_entity": "sensor.call"},
    )
    mgr = hass.data[DOMAIN][entry.entry_id]
    data = mgr.build()
    chips = data["chips"]
    assert chips[0]["text"] == "2.4 kW" and chips[0]["icon"] == "bolt"
    assert chips[1]["actions"] == []                      # locks are view-only
    assert chips[2]["actions"] == [{"a": "on", "label": "Turn on"}]
    assert "buttons" not in data                          # only while the sitter helper is on
    await hass.services.async_call("input_boolean", "turn_on", {"entity_id": "input_boolean.sitter"}, blocking=True)
    assert mgr.build()["buttons"][0]["name"] == "Lounge lamp"
    url = mgr.page_url()
    assert url.startswith("http://10.0.0.2:8123/arcane_screen_static/screen.html?v=") and "#feed=" in url
    assert no_screen.await_count >= 1                     # shown on start
    assert hass.states.get("sensor.hall_screen_dashboard") is not None


async def test_call_hides_and_returns(hass: HomeAssistant, no_screen) -> None:
    entry = await _setup(hass, {"call_entity": "sensor.call"})
    mgr = hass.data[DOMAIN][entry.entry_id]
    hass.states.async_set("sensor.call", "ringing")
    await hass.async_block_till_done()
    assert mgr.in_call and mgr.status == "in_call"
    shows = no_screen.await_count
    hass.states.async_set("sensor.call", "idle")
    await hass.async_block_till_done()
    assert mgr.in_call                                    # held for a few seconds after the call
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=6))
    await hass.async_block_till_done()
    assert not mgr.in_call
    assert no_screen.await_count == shows + 1


async def test_feed_and_act(hass: HomeAssistant, hass_client_no_auth, no_screen) -> None:
    entry = await _setup(hass, {"chips": ["input_boolean.lamp", "lock.front"]})
    client = await hass_client_no_auth()
    eid = entry.entry_id
    assert (await client.get(f"/api/arcane_screen/feed/wrong{KEY}/{eid}")).status == 404
    r = await client.post(f"/api/arcane_screen/act/{KEY}/{eid}", json={"kind": "chip", "i": 0, "a": "on"})
    assert r.status == 200
    await hass.async_block_till_done()
    assert hass.states.get("input_boolean.lamp").state == "on"
    r = await client.post(f"/api/arcane_screen/act/{KEY}/{eid}", json={"kind": "chip", "i": 1, "a": "open"})
    assert r.status == 409                                # not allowed on a lock
    r = await client.post(f"/api/arcane_screen/act/{KEY}/{eid}", json={"kind": "exit"})
    assert r.status == 200 and hass.data[DOMAIN][eid].hidden_until is not None
    resp = await client.get(f"/api/arcane_screen/feed/{KEY}/{eid}")
    assert resp.status == 200 and resp.headers["Content-Type"].startswith("text/event-stream")
    hass.data[DOMAIN][eid].banner("Hello", "Hi", "bell", 10, "info")
    chunk = b""
    for _ in range(5):
        chunk += await asyncio.wait_for(resp.content.readany(), 2)
        if b"event: banner" in chunk:
            break
    assert b"event: banner" in chunk
    resp.close()


async def test_static_page(hass: HomeAssistant, hass_client_no_auth, no_screen) -> None:
    await _setup(hass, {})
    client = await hass_client_no_auth()
    r = await client.get("/arcane_screen_static/screen.html")
    assert r.status == 200 and "EventSource" in await r.text()


async def test_config_flow_android(hass: HomeAssistant, no_screen) -> None:
    await async_setup_component(hass, "http", {})
    with patch("custom_components.arcane_screen.device.Screen.install_pipup", AsyncMock(return_value="0.31.0")):
        r = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
        assert r["type"] is FlowResultType.FORM
        r = await hass.config_entries.flow.async_configure(
            r["flow_id"], {"name": "Hall", "host": "10.0.0.9", "platform": "android", "adb_port": 5555, "ha_url": "http://10.0.0.2:8123"}
        )
        for _ in range(5):
            if r["type"] is FlowResultType.CREATE_ENTRY:
                break
            await hass.async_block_till_done()
            r = await hass.config_entries.flow.async_configure(r["flow_id"])
    assert r["type"] is FlowResultType.CREATE_ENTRY
    assert r["title"] == "Hall" and len(r["data"]["feed_key"]) >= 24


async def test_notices(hass: HomeAssistant, hass_client_no_auth, no_screen) -> None:
    entry = await _setup(hass, {})
    mgr = hass.data[DOMAIN][entry.entry_id]
    await hass.services.async_call(
        DOMAIN, "notice", {"id": "bin_night", "text": "Bins out tonight", "icon": "bin", "level": "warn", "tint": True, "tap_clears": True}, blocking=True
    )
    await hass.services.async_call(DOMAIN, "notice", {"id": "parcel", "text": "2 parcels today"}, blocking=True)
    notes = mgr.build()["notices"]
    assert [n["id"] for n in notes] == ["bin_night", "parcel"] and notes[0]["tint"] and notes[1]["level"] == "info"
    client = await hass_client_no_auth()
    act = f"/api/arcane_screen/act/{KEY}/{entry.entry_id}"
    assert (await client.post(act, json={"kind": "note", "id": "parcel"})).status == 409   # not tap-clearable
    assert (await client.post(act, json={"kind": "note", "id": "bin_night"})).status == 200
    assert [n["id"] for n in mgr.build()["notices"]] == ["parcel"]
    await hass.services.async_call(DOMAIN, "clear_notice", {}, blocking=True)
    assert "notices" not in mgr.build()


async def test_banner_reaches_a_late_screen(hass: HomeAssistant, hass_client_no_auth, no_screen) -> None:
    """A banner sent before the page connected is delivered when it does."""
    entry = await _setup(hass, {})
    hass.data[DOMAIN][entry.entry_id].banner("Back up", "Home Assistant", "bell", 30, "info")
    client = await hass_client_no_auth()
    resp = await client.get(f"/api/arcane_screen/feed/{KEY}/{entry.entry_id}")
    chunk = b""
    for _ in range(5):
        chunk += await asyncio.wait_for(resp.content.readany(), 2)
        if b"event: banner" in chunk:
            break
    assert b"event: banner" in chunk and b"Back up" in chunk
    resp.close()


async def test_welcome_home(hass: HomeAssistant, hass_client_no_auth, no_screen) -> None:
    """Welcome screen with cameras: shown, cameras reachable only while it is up, closed by a tap or by time."""
    from homeassistant.components.camera import Image

    entry = await _setup(hass, {})
    mgr = hass.data[DOMAIN][entry.entry_id]
    hass.states.async_set("camera.front_gate", "idle", {"friendly_name": "Front gate"})
    closed = []
    hass.bus.async_listen("arcane_screen_welcome_closed", lambda e: closed.append(e.data["by"]))
    await hass.services.async_call(
        DOMAIN, "welcome",
        {"name": "Alex", "message": "Front gate is opening", "message_after": "Front gate is open", "pulse_seconds": 30,
         "cameras": ["camera.front_gate"], "seconds": 60, "note": "Closes when the garage shuts"},
        blocking=True,
    )
    w = mgr.build()["welcome"]
    assert w["name"] == "Alex" and w["cams"] == [{"i": 0, "name": "FRONT GATE"}] and w["pulse_until"]
    client = await hass_client_no_auth()
    cam = f"/api/arcane_screen/cam/{KEY}/{entry.entry_id}"
    with patch("homeassistant.components.camera.async_get_image", AsyncMock(return_value=Image("image/jpeg", b"\xff\xd8jpeg"))):
        r = await client.get(f"{cam}/0?still=1&w=320")
        assert r.status == 200 and await r.read() == b"\xff\xd8jpeg"
        assert (await client.get(f"{cam}/1?still=1")).status == 404          # only the chosen cameras
        assert (await client.get(f"/api/arcane_screen/cam/wrong{KEY}/{entry.entry_id}/0?still=1")).status == 404
        r = await client.post(f"/api/arcane_screen/act/{KEY}/{entry.entry_id}", json={"kind": "welcome"})
        assert r.status == 200 and "welcome" not in mgr.build()
        await hass.async_block_till_done()
        assert (await client.get(f"{cam}/0?still=1")).status == 404          # closed: no more camera
    assert closed == ["tap"]
    await hass.services.async_call(DOMAIN, "welcome", {"name": "Sam", "seconds": 10}, blocking=True)
    assert mgr.build()["welcome"]["cams"] == []
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=11))
    await hass.async_block_till_done()
    assert "welcome" not in mgr.build() and closed == ["tap", "timeout"]


async def test_entrances_view_only_by_default(hass: HomeAssistant, hass_client_no_auth, no_screen) -> None:
    """Gate / garage / door covers can't be opened from a chip unless the owner allows it; blinds still can."""
    hass.states.async_set("cover.front_gate", "closed", {"device_class": "gate", "friendly_name": "Front gate"})
    hass.states.async_set("cover.blind", "closed", {"device_class": "blind", "friendly_name": "Blind"})
    entry = await _setup(hass, {"chips": ["cover.front_gate", "cover.blind"]})
    mgr = hass.data[DOMAIN][entry.entry_id]
    chips = mgr.build()["chips"]
    assert chips[0]["actions"] == [] and chips[1]["actions"]
    client = await hass_client_no_auth()
    r = await client.post(f"/api/arcane_screen/act/{KEY}/{entry.entry_id}", json={"kind": "chip", "i": 0, "a": "open"})
    assert r.status == 409
    hass.config_entries.async_update_entry(entry, options={"chips": ["cover.front_gate", "cover.blind"], "open_entrances": True})
    await hass.async_block_till_done()
    assert hass.data[DOMAIN][entry.entry_id].build()["chips"][0]["actions"]


async def test_config_flow_hikvision(hass: HomeAssistant, no_screen) -> None:
    """The Hikvision login step (issue #3: it crashed before asking the station anything)."""
    await async_setup_component(hass, "http", {})
    with patch("custom_components.arcane_screen.device.Screen.hik_adb_on", AsyncMock(return_value=True)), patch(
        "custom_components.arcane_screen.device.Screen.install_pipup", AsyncMock(return_value="0.31.0")
    ):
        r = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
        r = await hass.config_entries.flow.async_configure(
            r["flow_id"], {"name": "Hall", "host": "10.0.0.8", "platform": "hikvision", "adb_port": 5555, "ha_url": "http://10.0.0.2:8123"}
        )
        assert r["type"] is FlowResultType.FORM and r["step_id"] == "hikvision"
        r = await hass.config_entries.flow.async_configure(r["flow_id"], {"hik_username": "admin", "hik_password": "pw"})
        for _ in range(5):
            if r["type"] is FlowResultType.CREATE_ENTRY:
                break
            await hass.async_block_till_done()
            r = await hass.config_entries.flow.async_configure(r["flow_id"])
    assert r["type"] is FlowResultType.CREATE_ENTRY


async def test_hik_login_refused_once(hass: HomeAssistant) -> None:
    """A wrong Hikvision password is tried once, then never again until it is typed again."""
    from custom_components.arcane_screen.device import Screen, ScreenError

    screen = Screen(hass, "10.0.0.7", 5555, "admin", "wrong")
    put = AsyncMock(return_value=type("R", (), {"status_code": 401})())
    with patch("httpx.AsyncClient.put", put):
        with pytest.raises(ScreenError):
            await screen.hik_adb_on()
        assert await screen.hik_adb_on() is False
    assert put.await_count == 1
    Screen._refused.pop("10.0.0.7", None)


async def test_old_android_refused(hass: HomeAssistant) -> None:
    """Screens older than Android 6 get a plain message before anything is uploaded (issue #2)."""
    from custom_components.arcane_screen.device import Screen, ScreenError

    screen = Screen(hass, "10.0.0.6", 5555)
    with patch.object(Screen, "adb", AsyncMock(return_value=["19", "4.4.2"])), patch(
        "custom_components.arcane_screen.device.get_async_client"
    ) as client:
        with pytest.raises(ScreenError, match="Android 4.4.2"):
            await screen.install_pipup()
    client.assert_not_called()
