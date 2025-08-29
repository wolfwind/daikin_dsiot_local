from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any, Optional
from homeassistant.helpers.entity import EntityCategory

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

class _BaseDaikinSensor(SensorEntity):
    _attr_should_poll = True
    _attr_has_entity_name = True

    def __init__(self, hass: HomeAssistant, entry_id: str, host: str, title: str) -> None:
        self._hass = hass
        self._entry_id = entry_id
        self._host = host
        self._state: Any = None
        self._climate_entity_id_cache: Optional[str] = None
        self._attr_device_info = _build_device_info(host, title)

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
        for state in self._hass.states.async_all("climate"):
            if state.attributes.get("ip") == self._host:
                self._climate_entity_id_cache = state.entity_id
                break
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
        st = self._get_climate_state()
        if not st or st.state in ("unavailable", "unknown"):
            self._attr_available = False
            self._state = None
            return
        self._attr_available = True
        self._update_from_state(st)

    def _update_from_state(self, st) -> None:
        self._state = None

# --- Concrete sensors ---

class DaikinOutdoorTempSensor(_BaseDaikinSensor):
    _attr_name = "Outdoor Temperature"
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_state_class = SensorStateClass.MEASUREMENT
    def __init__(self, hass, entry_id, host, title): super().__init__(hass, entry_id, host, title); self._attr_unique_id=f"daikin_outdoor_temp_{host}"
    def _update_from_state(self, st): self._state = st.attributes.get("outside_temperature")

class DaikinIndoorTempSensor(_BaseDaikinSensor):
    _attr_name = "Indoor Temperature"
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_state_class = SensorStateClass.MEASUREMENT
    def __init__(self, hass, entry_id, host, title): super().__init__(hass, entry_id, host, title); self._attr_unique_id=f"daikin_indoor_temp_{host}"
    def _update_from_state(self, st): self._state = st.attributes.get("current_temperature")

class DaikinCurrentHumiditySensor(_BaseDaikinSensor):
    _attr_name = "Indoor Humidity"
    _attr_device_class = SensorDeviceClass.HUMIDITY
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT
    def __init__(self, hass, entry_id, host, title): super().__init__(hass, entry_id, host, title); self._attr_unique_id=f"daikin_indoor_humidity_{host}"
    def _update_from_state(self, st): self._state = st.attributes.get("current_humidity") or st.attributes.get("humidity")

class DaikinEnergyTodaySensor(_BaseDaikinSensor):
    _attr_name = "Energy Today"
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_state_class = SensorStateClass.TOTAL
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    def __init__(self, hass, entry_id, host, title): super().__init__(hass, entry_id, host, title); self._attr_unique_id=f"daikin_energy_today_{host}"
    def _update_from_state(self, st):
        val = st.attributes.get("energy_today")
        self._state = None if val in (None, 0, "0") else val

class DaikinEnergyYesterdaySensor(_BaseDaikinSensor):
    _attr_name = "Energy Yesterday"
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_state_class = SensorStateClass.TOTAL
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    def __init__(self, hass, entry_id, host, title): super().__init__(hass, entry_id, host, title); self._attr_unique_id=f"daikin_energy_yesterday_{host}"
    def _update_from_state(self, st):
        val = st.attributes.get("energy_yesterday")
        self._state = None if val in (None, 0, "0") else val

class DaikinEnergyWeekTotalSensor(_BaseDaikinSensor):
    _attr_name = "Energy This Week"
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_state_class = SensorStateClass.TOTAL
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    def __init__(self, hass, entry_id, host, title): super().__init__(hass, entry_id, host, title); self._attr_unique_id=f"daikin_energy_week_total_{host}"
    def _update_from_state(self, st):
        val = st.attributes.get("energy_week_total")
        self._state = None if val in (None, 0, "0") else val

class DaikinRuntimeTodaySensor(_BaseDaikinSensor):
    _attr_name = "Runtime Today"
    _attr_device_class = SensorDeviceClass.DURATION
    _attr_native_unit_of_measurement = UnitOfTime.MINUTES
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    def __init__(self, hass, entry_id, host, title): super().__init__(hass, entry_id, host, title); self._attr_unique_id=f"daikin_runtime_today_{host}"
    def _update_from_state(self, st): self._state = st.attributes.get("runtime_today")

class DaikinTargetTempSensor(_BaseDaikinSensor):
    _attr_name = "Target Temperature"
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_state_class = SensorStateClass.MEASUREMENT
    def __init__(self, hass, entry_id, host, title): super().__init__(hass, entry_id, host, title); self._attr_unique_id=f"daikin_target_temp_{host}"
    def _update_from_state(self, st): self._state = st.attributes.get("target_temperature") or st.attributes.get("temperature")

class DaikinTargetHumiditySensor(_BaseDaikinSensor):
    _attr_name = "Target Humidity"
    _attr_device_class = SensorDeviceClass.HUMIDITY
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    def __init__(self, hass, entry_id, host, title): super().__init__(hass, entry_id, host, title); self._attr_unique_id=f"daikin_target_humidity_{host}"
    def _update_from_state(self, st):
        self._state = (
            st.attributes.get("cool_humidity_target")
            or st.attributes.get("target_humidity")
            or st.attributes.get("humidity")
        )

class DaikinHumidityControlStatusSensor(_BaseDaikinSensor):
    _attr_name = "Humidity Control Status"
    _attr_icon = "mdi:water-percent"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    def __init__(self, hass, entry_id, host, title): super().__init__(hass, entry_id, host, title); self._attr_unique_id=f"daikin_humidity_ctrl_status_{host}"
    def _update_from_state(self, st):
        preset = st.attributes.get("preset_mode")  # 兼容舊版本
        if preset is not None:
            self._state = "on" if str(preset).lower() == "humidity_control" else "off"
            return
        enabled = st.attributes.get("cool_humidity_enabled")
        if enabled is None:
            self._attr_available = False
            self._state = None
            return
        self._state = "on" if bool(enabled) else "off"

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback):
    host = _get_host(entry)
    title = _get_title(entry)
    entities: list[SensorEntity] = [
        DaikinOutdoorTempSensor(hass, entry.entry_id, host, title),
        DaikinIndoorTempSensor(hass, entry.entry_id, host, title),
        DaikinCurrentHumiditySensor(hass, entry.entry_id, host, title),
        DaikinTargetTempSensor(hass, entry.entry_id, host, title),
        DaikinEnergyTodaySensor(hass, entry.entry_id, host, title),
        DaikinEnergyYesterdaySensor(hass, entry.entry_id, host, title),
        DaikinEnergyWeekTotalSensor(hass, entry.entry_id, host, title),
        DaikinRuntimeTodaySensor(hass, entry.entry_id, host, title),
        DaikinTargetHumiditySensor(hass, entry.entry_id, host, title),
        DaikinHumidityControlStatusSensor(hass, entry.entry_id, host, title),
    ]
    async_add_entities(entities, update_before_add=True)
