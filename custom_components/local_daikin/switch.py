from __future__ import annotations

import logging
from datetime import timedelta
from typing import Optional

from homeassistant.components.switch import SwitchEntity
from homeassistant.components.climate.const import HVACMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

DOMAIN = "local_daikin"
_LOGGER = logging.getLogger(__name__)

SCAN_INTERVAL = timedelta(seconds=60)


# -------------------------
# helpers
# -------------------------

def _get_host(entry: ConfigEntry) -> str:
    host = _get_host(entry)
    return entry.title or f"Local Daikin ({host})"

def _get_title(entry: ConfigEntry) -> str:
    host = _get_host(entry)
    return entry.title or f"Local Daikin ({host})"

def _build_device_info(host: str, title: str) -> DeviceInfo:
    return DeviceInfo(
        identifiers={(DOMAIN, f"daikin-{host}")},
        name=title,
        manufacturer="Daikin",
        model="Local API (/dsiot)",
    )


class _BaseDaikinSwitch(SwitchEntity):
    """Base：不主動觸發對方更新，只讀取 climate 狀態機。"""

    _attr_should_poll = True
    _attr_has_entity_name = True

    def __init__(self, hass: HomeAssistant, entry_id: str, host: str, title: str) -> None:
        self._hass = hass
        self._entry_id = entry_id
        self._host = host
        self._climate_entity_id_cache: Optional[str] = None
        self._attr_device_info = _build_device_info(host, title)

    # ---- 找到 climate entity_id 並快取 ----
    def _resolve_climate_entity_id(self) -> Optional[str]:
        if self._climate_entity_id_cache:
            return self._climate_entity_id_cache

        # 先從 domain data 尋找（若平台在 setup 時有存）
        bucket = self._hass.data.get(DOMAIN, {}).get(self._entry_id)
        if bucket:
            ent = bucket.get("climate_entity_id") or bucket.get("climate_entity")
            if isinstance(ent, str):
                self._climate_entity_id_cache = ent
                return ent
            if getattr(ent, "entity_id", None):
                self._climate_entity_id_cache = ent.entity_id  # type: ignore[attr-defined]
                return self._climate_entity_id_cache

        # 後備方案：掃描 climate，找 attributes.ip == host 的
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


# -------------------------
# Power switch
# -------------------------

class DaikinPowerSwitch(_BaseDaikinSwitch):
    _attr_name = "Power"

    def __init__(self, hass: HomeAssistant, entry_id: str, host: str) -> None:
        super().__init__(hass, entry_id, host)
        self._attr_unique_id = f"daikin_power_{host}"
        self._state = False

    @property
    def is_on(self) -> bool:
        return self._state

    def update(self) -> None:
        st = self._get_climate_state()
        if not st:
            self._attr_available = False
            self._state = False
            return
        # climate 的 state 即為 hvac_mode 字串
        self._attr_available = st.state not in ("unavailable", "unknown")
        self._state = str(st.state).lower() != "off"

    async def async_turn_on(self, **kwargs) -> None:
        ent_id = self._resolve_climate_entity_id()
        if not ent_id:
            return
        # 若目前為 off，無法知道「上一次模式」，就用 COOL 作為保守預設
        st = self._hass.states.get(ent_id)
        next_mode = st.state if st and str(st.state).lower() != "off" else HVACMode.COOL.value
        await self._hass.services.async_call(
            "climate",
            "set_hvac_mode",
            {"entity_id": ent_id, "hvac_mode": next_mode},
            blocking=True,
        )

    async def async_turn_off(self, **kwargs) -> None:
        ent_id = self._resolve_climate_entity_id()
        if not ent_id:
            return
        await self._hass.services.async_call(
            "climate",
            "set_hvac_mode",
            {"entity_id": ent_id, "hvac_mode": HVACMode.OFF.value},
            blocking=True,
        )


# -------------------------
# Quiet Fan switch
# -------------------------

class DaikinQuietFanSwitch(_BaseDaikinSwitch):
    _attr_name = "Quiet Fan"

    def __init__(self, hass: HomeAssistant, entry_id: str, host: str) -> None:
        super().__init__(hass, entry_id, host)
        self._attr_unique_id = f"daikin_quiet_fan_{host}"
        self._state = False

    @property
    def is_on(self) -> bool:
        return self._state

    def update(self) -> None:
        st = self._get_climate_state()
        if not st:
            self._attr_available = False
            self._state = False
            return
        self._attr_available = st.state not in ("unavailable", "unknown")
        fan = st.attributes.get("fan_mode")
        fan_str = str(getattr(fan, "value", fan)).lower() if fan is not None else ""
        self._state = (fan_str == "quiet")

    async def async_turn_on(self, **kwargs) -> None:
        ent_id = self._resolve_climate_entity_id()
        if not ent_id:
            return
        await self._hass.services.async_call(
            "climate",
            "set_fan_mode",
            {"entity_id": ent_id, "fan_mode": "Quiet"},
            blocking=True,
        )

    async def async_turn_off(self, **kwargs) -> None:
        ent_id = self._resolve_climate_entity_id()
        if not ent_id:
            return
        # 若裝置沒有 Quiet，就退回 Auto；這裡直接指定 Auto
        await self._hass.services.async_call(
            "climate",
            "set_fan_mode",
            {"entity_id": ent_id, "fan_mode": "Auto"},
            blocking=True,
        )

# -------------------------
# Cool Humidity Control switch
# -------------------------

class DaikinCoolHumiditySwitch(_BaseDaikinSwitch):
    _attr_name = "Cool Humidity Control"

    def __init__(self, hass: HomeAssistant, entry_id: str, host: str) -> None:
        super().__init__(hass, entry_id, host)
        self._attr_unique_id = f"daikin_cool_humidity_{host}"
        self._state = False

    @property
    def is_on(self) -> bool:
        return self._state

    def update(self) -> None:
        st = self._get_climate_state()
        if not st:
            self._attr_available = False
            self._state = False
            return
        self._attr_available = st.state not in ("unavailable", "unknown")
        p0c = st.attributes.get("p_0C")  # "00"=off, "01"=on, "06"=continuous
        self._state = p0c in ("01", "06")

    async def async_turn_on(self, **kwargs) -> None:
        ent_id = self._resolve_climate_entity_id()
        if not ent_id:
            return
        # 預設目標：沿用目前值或 55%
        st = self._get_climate_state()
        target = st.attributes.get("cool_humidity_target") if st else None
        if target not in (50, 55, 60):
            target = 55
        await self._hass.services.async_call(
            "climate",
            "set_cool_humidity_control",
            {"entity_id": ent_id, "enabled": True, "target": target},
            blocking=True,
        )

    async def async_turn_off(self, **kwargs) -> None:
        ent_id = self._resolve_climate_entity_id()
        if not ent_id:
            return
        await self._hass.services.async_call(
            "climate",
            "set_cool_humidity_control",
            {"entity_id": ent_id, "enabled": False},
            blocking=True,
        )

# -------------------------
# setup
# -------------------------

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback):
    host = _get_host(entry)
    title = _get_title(entry)
    entities: list[SwitchEntity] = [
        DaikinPowerSwitch(hass, entry.entry_id, host, title),
        DaikinQuietFanSwitch(hass, entry.entry_id, host, title),
        DaikinCoolHumiditySwitch(hass, entry.entry_id, host, title),
    ]
    async_add_entities(entities, update_before_add=True)
