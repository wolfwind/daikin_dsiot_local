from __future__ import annotations

import logging
from typing import Optional, List, Tuple

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.entity import EntityCategory

DOMAIN = "local_daikin"
_LOGGER = logging.getLogger(__name__)

def _get_host(entry: ConfigEntry) -> str:
    return (
        entry.data.get("host")
        or entry.data.get("ip")
        or entry.data.get("ip_address")
        or entry.options.get("host")
        or entry.options.get("ip")
        or entry.options.get("ip_address")
    )

def _get_title(entry: ConfigEntry) -> str:
    host = _get_host(entry)
    return entry.title or f"Local Daikin ({host})"

def _build_device_info(host: str, title: str) -> DeviceInfo:
    return DeviceInfo(
        identifiers={(DOMAIN, f"daikin-{host}")},
        name=title,
        manufacturer="Daikin",
        model=f"Local API (/dsiot) @ {host}",
        configuration_url=f"http://{host}",
    )

class _BaseDaikinSelect(SelectEntity):
    _attr_should_poll = False
    _attr_has_entity_name = True

    def __init__(self, hass: HomeAssistant, entry_id: str, host: str, title: str) -> None:
        self._hass = hass
        self._entry_id = entry_id
        self._host = host
        self._climate_entity_id_cache: Optional[str] = None
        self._attr_device_info = _build_device_info(host, title)

    def _resolve_climate_entity_id(self) -> Optional[str]:
        if self._climate_entity_id_cache:
            return self._climate_entity_id_cache
        bucket = self._hass.data.get(DOMAIN, {}).get(self._entry_id)
        if bucket:
            ent = bucket.get("climate_entity_id") or bucket.get("climate_entity")
            if isinstance(ent, str):
                self._climate_entity_id_cache = ent
                return ent
            if getattr(ent, "entity_id", None):
                self._climate_entity_id_cache = ent.entity_id  # type: ignore[attr-defined]
                return self._climate_entity_id_cache
        for st in self._hass.states.async_all("climate"):
            if st.attributes.get("ip") == self._host:
                self._climate_entity_id_cache = st.entity_id
                break
        if not self._climate_entity_id_cache:
            _LOGGER.debug("Could not resolve climate entity for host=%s", self._host)
        return self._climate_entity_id_cache

    def _get_climate_state(self):
        ent_id = self._resolve_climate_entity_id()
        if not ent_id:
            return None
        return self._hass.states.get(ent_id)

class DaikinHVACModeSelect(_BaseDaikinSelect):
    _attr_name = "HVAC Mode"
    _attr_icon = "mdi:state-machine"

    @property
    def options(self) -> List[str]:
        st = self._get_climate_state()
        if not st:
            return []
        opts = st.attributes.get("hvac_modes") or []
        return [str(getattr(o, "value", o)) for o in opts]

    @property
    def current_option(self) -> Optional[str]:
        st = self._get_climate_state()
        if not st:
            return None
        return str(st.state) if st.state else None

    async def async_select_option(self, option: str) -> None:
        ent_id = self._resolve_climate_entity_id()
        if not ent_id:
            return
        await self._hass.services.async_call(
            "climate",
            "set_hvac_mode",
            {"entity_id": ent_id, "hvac_mode": option},
            blocking=True,
        )

    @property
    def available(self) -> bool:
        st = self._get_climate_state()
        return bool(st and st.state not in ("unavailable", "unknown") and st.attributes.get("hvac_modes"))

class DaikinFanSpeedSelect(_BaseDaikinSelect):
    _attr_name = "Fan Speed"
    _attr_icon = "mdi:fan"

    @property
    def options(self) -> List[str]:
        st = self._get_climate_state()
        if not st:
            return []
        opts = st.attributes.get("fan_modes") or []
        return [str(getattr(o, "value", o)) for o in opts]

    @property
    def current_option(self) -> Optional[str]:
        st = self._get_climate_state()
        if not st:
            return None
        fm = st.attributes.get("fan_mode")
        return str(getattr(fm, "value", fm)) if fm is not None else None

    async def async_select_option(self, option: str) -> None:
        ent_id = self._resolve_climate_entity_id()
        if not ent_id:
            return
        await self._hass.services.async_call(
            "climate",
            "set_fan_mode",
            {"entity_id": ent_id, "fan_mode": option},
            blocking=True,
        )

    @property
    def available(self) -> bool:
        st = self._get_climate_state()
        return bool(st and st.state not in ("unavailable", "unknown") and st.attributes.get("fan_modes"))

# 狀態顯示（診斷）
class DaikinHumidityControlStatusSelect(_BaseDaikinSelect):
    _attr_name = "Humidity Control Status"
    _attr_icon = "mdi:water-percent"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _options = ["Enabled", "Disabled"]

    @property
    def options(self) -> List[str]:
        return self._options

    @property
    def current_option(self) -> Optional[str]:
        st = self._get_climate_state()
        if not st:
            return None
        preset = st.attributes.get("preset_mode")
        if preset is not None:
            return "Enabled" if str(preset).lower() == "humidity_control" else "Disabled"
        enabled = st.attributes.get("cool_humidity_enabled")
        if enabled is None:
            return None
        return "Enabled" if bool(enabled) else "Disabled"

    async def async_select_option(self, option: str) -> None:
        # 只讀
        _LOGGER.debug("Humidity Control Status is read-only; ignore selection: %s", option)

    @property
    def available(self) -> bool:
        st = self._get_climate_state()
        return bool(st and st.state not in ("unavailable", "unknown"))

# Vane 對照
_HVAC_MODE_TO_SWING_ATTR_BY_STR: dict[str, Tuple[str, str]] = {
    "cool": ("p_05", "p_06"),
    "heat": ("p_05", "p_06"),
    "auto": ("p_05", "p_06"),
    "fan_only": ("p_05", "p_06"),
    "dry": ("p_05", "p_06"),
}

_VERTICAL_OPTIONS = [
    "Auto","Swing","Circulation",
    "Top","Upper","Upper-Middle","Lower-Middle","Lower","Bottom",
    "1","2","3","4","5","6","Off",
]
_VERTICAL_TO_HEX = {
    "Off": "000000","Auto": "100000","Swing": "0F0000","Circulation": "140000",
    "Top": "010000","Upper": "020000","Upper-Middle": "030000",
    "Lower-Middle": "040000","Lower": "050000","Bottom": "060000",
    "1":"010000","2":"020000","3":"030000","4":"040000","5":"050000","6":"060000",
}
_HEX_TO_VERTICAL = {
    "000000": "Off","100000": "Auto","0F0000": "Swing","140000": "Circulation",
    "010000": "Top","020000": "Upper","030000": "Upper-Middle",
    "040000": "Lower-Middle","050000": "Lower","060000": "Bottom",
}

_HORIZONTAL_OPTIONS = ["Off","Swing","Left","Left-Center","Center","Right-Center","Right"]
_HORIZONTAL_TO_HEX = {
    "Off":"000000","Swing":"0F0000","Left":"010000","Left-Center":"020000","Center":"030000","Right-Center":"040000","Right":"050000",
}
_HEX_TO_HORIZONTAL = {v:k for k,v in _HORIZONTAL_TO_HEX.items()}

class DaikinVerticalVaneSelect(_BaseDaikinSelect):
    _attr_name = "Vertical Vane"
    _attr_icon = "mdi:pan-vertical"
    @property
    def options(self) -> List[str]:
        return _VERTICAL_OPTIONS
    @property
    def current_option(self) -> Optional[str]:
        st = self._get_climate_state()
        if not st:
            return None
        mode = str(st.state).lower()
        v_attr = _HVAC_MODE_TO_SWING_ATTR_BY_STR.get(mode, ("p_05","p_06"))[0]
        hexv = st.attributes.get(v_attr)
        if isinstance(hexv, str):
            return _HEX_TO_VERTICAL.get(hexv)
        return None
    async def async_select_option(self, option: str) -> None:
        ent_id = self._resolve_climate_entity_id()
        if not ent_id or option not in _VERTICAL_TO_HEX:
            return
        await self._hass.services.async_call("climate","set_vane_position",
            {"entity_id": ent_id, "vertical": option}, blocking=True)
    @property
    def available(self) -> bool:
        st = self._get_climate_state()
        if not st or st.state in ("unavailable","unknown"):
            return False
        if st.attributes.get("supports_vertical_vane") is True:
            return True
        mode = str(st.state).lower()
        v_attr = _HVAC_MODE_TO_SWING_ATTR_BY_STR.get(mode, ("p_05","p_06"))[0]
        return v_attr in st.attributes

class DaikinHorizontalVaneSelect(_BaseDaikinSelect):
    _attr_name = "Horizontal Vane"
    _attr_icon = "mdi:pan-horizontal"
    @property
    def options(self) -> List[str]:
        return _HORIZONTAL_OPTIONS
    @property
    def current_option(self) -> Optional[str]:
        st = self._get_climate_state()
        if not st:
            return None
        mode = str(st.state).lower()
        h_attr = _HVAC_MODE_TO_SWING_ATTR_BY_STR.get(mode, ("p_05","p_06"))[1]
        hexv = st.attributes.get(h_attr)
        if isinstance(hexv, str):
            return _HEX_TO_HORIZONTAL.get(hexv)
        return None
    async def async_select_option(self, option: str) -> None:
        ent_id = self._resolve_climate_entity_id()
        if not ent_id or option not in _HORIZONTAL_TO_HEX:
            return
        await self._hass.services.async_call("climate","set_vane_position",
            {"entity_id": ent_id, "horizontal": option}, blocking=True)
    @property
    def available(self) -> bool:
        st = self._get_climate_state()
        if not st or st.state in ("unavailable","unknown"):
            return False
        if st.attributes.get("supports_horizontal_vane") is True:
            return True
        mode = str(st.state).lower()
        h_attr = _HVAC_MODE_TO_SWING_ATTR_BY_STR.get(mode, ("p_05","p_06"))[1]
        return h_attr in st.attributes

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback):
    host = _get_host(entry)
    title = _get_title(entry)
    entities: list[SelectEntity] = [
        DaikinHVACModeSelect(hass, entry.entry_id, host, title),
        DaikinFanSpeedSelect(hass, entry.entry_id, host, title),
        DaikinHumidityControlStatusSelect(hass, entry.entry_id, host, title),
        DaikinVerticalVaneSelect(hass, entry.entry_id, host, title),
        DaikinHorizontalVaneSelect(hass, entry.entry_id, host, title),
    ]
    async_add_entities(entities, update_before_add=True)
