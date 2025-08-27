from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any, Optional

from homeassistant.components.sensor import (
    SensorEntity,
    SensorDeviceClass,
    SensorStateClass,
)
from homeassistant.const import (
    UnitOfTemperature,
    UnitOfEnergy,
    PERCENTAGE,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.entity_platform import AddEntitiesCallback

DOMAIN = "local_daikin"
_LOGGER = logging.getLogger(__name__)

SCAN_INTERVAL = timedelta(seconds=60)

# -------------------------
# Helpers
# -------------------------

def _get_host(entry: ConfigEntry) -> str:
    # 支援新舊資料鍵
    return entry.data.get("host") or entry.data.get("ip") or entry.data.get("ip_address")  # type: ignore[return-value]


def _build_device_info(host: str) -> DeviceInfo:
    return DeviceInfo(
        identifiers={(DOMAIN, f"daikin-{host}")},
        name=f"Local Daikin ({host})",
        manufacturer="Daikin",
        model="Local API (/dsiot)",
    )


class _BaseDaikinSensor(SensorEntity):
    """Base：從 climate 的狀態機讀取資料，不主動更新對方。"""

    _attr_should_poll = True  # 輕量，只讀狀態機
    _attr_has_entity_name = True

    def __init__(self, hass: HomeAssistant, entry_id: str, host: str) -> None:
        self._hass = hass
        self._entry_id = entry_id
        self._host = host
        self._state: Any = None
        self._climate_entity_id_cache: Optional[str] = None
        self._attr_device_info = _build_device_info(host)

    # ---- 找到 climate entity_id 並快取 ----
    def _resolve_climate_entity_id(self) -> Optional[str]:
        if self._climate_entity_id_cache:
            return self._climate_entity_id_cache
        entry_bucket = self._hass.data.get(DOMAIN, {}).get(self._entry_id)
        ent = None
        if entry_bucket:
            ent = entry_bucket.get("climate_entity_id") or entry_bucket.get("climate_entity")
            if getattr(ent, "entity_id", None):
                self._climate_entity_id_cache = ent.entity_id  # type: ignore[attr-defined]
                return self._climate_entity_id_cache
            if isinstance(ent, str):
                self._climate_entity_id_cache = ent
                return ent

        # 後備方案：掃描 climate domain，找 attributes.ip == host 的
        for state in self._hass.states.async_all("climate"):
            if state.attributes.get("ip") == self._host:
                self._climate_entity_id_cache = state.entity_id
                break
        if not self._climate_entity_id_cache:
            _LOGGER.debug("Could not resolve climate entity_id for host=%s", self._host)
        return self._climate_entity_id_cache

    def _get_climate_state(self):
        ent_id = self._resolve_climate_entity_id()
        if not ent_id:
            return None
        return self._hass.states.get(ent_id)

    @property
    def native_value(self):
        return self._state

    def update(self) -> None:
        """從 climate 實體的 attributes 抓最新值。"""
        st = self._get_climate_state()
        if not st:
            return
        self._update_from_state(st)

    # 子類覆寫
    def _update_from_state(self, st) -> None:
        raise NotImplementedError


# -------------------------
# Concrete sensors
# -------------------------

class DaikinOutdoorTempSensor(_BaseDaikinSensor):
    _attr_name = "Outdoor Temperature"
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, hass: HomeAssistant, entry_id: str, host: str) -> None:
        super().__init__(hass, entry_id, host)
        self._attr_unique_id = f"daikin_outdoor_temp_{host}"

    def _update_from_state(self, st) -> None:
        self._state = st.attributes.get("outside_temperature")


class DaikinIndoorTempSensor(_BaseDaikinSensor):
    _attr_name = "Indoor Temperature"
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, hass: HomeAssistant, entry_id: str, host: str) -> None:
        super().__init__(hass, entry_id, host)
        self._attr_unique_id = f"daikin_indoor_temp_{host}"

    def _update_from_state(self, st) -> None:
        self._state = st.attributes.get("current_temperature")


class DaikinCurrentHumiditySensor(_BaseDaikinSensor):
    _attr_name = "Indoor Humidity"
    _attr_device_class = SensorDeviceClass.HUMIDITY
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, hass: HomeAssistant, entry_id: str, host: str) -> None:
        super().__init__(hass, entry_id, host)
        self._attr_unique_id = f"daikin_indoor_humidity_{host}"

    def _update_from_state(self, st) -> None:
        self._state = st.attributes.get("current_humidity") or st.attributes.get("humidity")


class DaikinEnergyTodaySensor(_BaseDaikinSensor):
    _attr_name = "Energy Today"
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    # 今日用電：白天累積、午夜歸零
    _attr_state_class = SensorStateClass.TOTAL

    def __init__(self, hass: HomeAssistant, entry_id: str, host: str) -> None:
        super().__init__(hass, entry_id, host)
        self._attr_unique_id = f"daikin_energy_today_{host}"

    def _update_from_state(self, st) -> None:
        self._state = st.attributes.get("energy_today")


class DaikinEnergyYesterdaySensor(_BaseDaikinSensor):
    _attr_name = "Energy Yesterday"
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_state_class = SensorStateClass.TOTAL

    def __init__(self, hass: HomeAssistant, entry_id: str, host: str) -> None:
        super().__init__(hass, entry_id, host)
        self._attr_unique_id = f"daikin_energy_yesterday_{host}"

    def _update_from_state(self, st) -> None:
        self._state = st.attributes.get("energy_yesterday")


class DaikinEnergyWeekTotalSensor(_BaseDaikinSensor):
    _attr_name = "Energy This Week"
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_state_class = SensorStateClass.TOTAL

    def __init__(self, hass: HomeAssistant, entry_id: str, host: str) -> None:
        super().__init__(hass, entry_id, host)
        self._attr_unique_id = f"daikin_energy_week_total_{host}"

    def _update_from_state(self, st) -> None:
        self._state = st.attributes.get("energy_week_total")


class DaikinRuntimeTodaySensor(_BaseDaikinSensor):
    _attr_name = "Runtime Today"
    _attr_device_class = SensorDeviceClass.DURATION
    _attr_native_unit_of_measurement = UnitOfTime.MINUTES
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, hass: HomeAssistant, entry_id: str, host: str) -> None:
        super().__init__(hass, entry_id, host)
        self._attr_unique_id = f"daikin_runtime_today_{host}"

    def _update_from_state(self, st) -> None:
        self._state = st.attributes.get("runtime_today")


class DaikinTargetTempSensor(_BaseDaikinSensor):
    _attr_name = "Target Temperature"
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, hass: HomeAssistant, entry_id: str, host: str) -> None:
        super().__init__(hass, entry_id, host)
        self._attr_unique_id = f"daikin_target_temp_{host}"

    def _update_from_state(self, st) -> None:
        # climate 可能以 target_temperature 或 temperature 暴露
        self._state = st.attributes.get("target_temperature") or st.attributes.get("temperature")


# -------------------------
# setup
# -------------------------

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback):
    host = _get_host(entry)
    entities: list[SensorEntity] = [
        DaikinOutdoorTempSensor(hass, entry.entry_id, host),
        DaikinIndoorTempSensor(hass, entry.entry_id, host),
        DaikinCurrentHumiditySensor(hass, entry.entry_id, host),
        DaikinTargetTempSensor(hass, entry.entry_id, host),
        DaikinEnergyTodaySensor(hass, entry.entry_id, host),
        DaikinEnergyYesterdaySensor(hass, entry.entry_id, host),
        DaikinEnergyWeekTotalSensor(hass, entry.entry_id, host),
        DaikinRuntimeTodaySensor(hass, entry.entry_id, host),
    ]
    async_add_entities(entities, update_before_add=True)
