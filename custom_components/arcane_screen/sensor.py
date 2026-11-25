"""One status sensor per screen: showing / hidden / in_call / reconnecting / offline."""
from __future__ import annotations

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, PIPUP_VERSION, SIGNAL_STATUS, VERSION
from .manager import ScreenManager

STATES = ["starting", "showing", "hidden", "in_call", "reconnecting", "offline"]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, add: AddEntitiesCallback) -> None:
    add([StatusSensor(hass.data[DOMAIN][entry.entry_id])])


class StatusSensor(SensorEntity):
    _attr_has_entity_name = True
    _attr_translation_key = "status"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = STATES
    _attr_should_poll = False

    def __init__(self, mgr: ScreenManager) -> None:
        self.mgr = mgr
        eid = mgr.entry.entry_id
        self._attr_unique_id = f"{eid}_status"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, eid)},
            name=mgr.entry.title,
            manufacturer="Arcane Screen",
            model="Screen dashboard (PiPup)",
            sw_version=f"{VERSION} / PiPup {PIPUP_VERSION}",
        )

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            async_dispatcher_connect(self.hass, SIGNAL_STATUS.format(self.mgr.entry.entry_id), self._update)
        )

    @callback
    def _update(self) -> None:
        self.async_write_ha_state()

    @property
    def native_value(self) -> str:
        return self.mgr.status

    @property
    def extra_state_attributes(self) -> dict:
        m = self.mgr
        return {
            "listeners": len(m.line.queues),
            "in_call": m.in_call,
            "hidden_until": m.hidden_until.isoformat() if m.hidden_until else None,
            "last_pipup_restart": m.last_restart.isoformat() if m.last_restart else None,
            "last_error": m.last_error,
            "notices": list(m.notices),   # ids of the notices on the screen now (blueprints check this)
        }
