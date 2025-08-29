from __future__ import annotations
from functools import partial
import logging
import time
import requests
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Optional

import voluptuous as vol
from homeassistant.helpers import config_validation as cv
from . import DOMAIN

from homeassistant.components.climate import ClimateEntity, ClimateEntityFeature
from homeassistant.components.climate.const import (
    HVACMode,
    SWING_OFF,
    SWING_BOTH,
    SWING_VERTICAL,
    SWING_HORIZONTAL,
)
from homeassistant.const import UnitOfTemperature
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers import entity_platform

_LOGGER = logging.getLogger(__name__)

# ===== 可調參數 =====
DEFAULT_TIMEOUT = 60
BACKOFFS = [10, 30, 60, 120, 300]

# ===== 對照表 =====
class HAFanMode(StrEnum):
    FAN_QUIET = "Quiet"
    FAN_AUTO = "Auto"
    FAN_LEVEL1 = "Level 1"
    FAN_LEVEL2 = "Level 2"
    FAN_LEVEL3 = "Level 3"
    FAN_LEVEL4 = "Level 4"
    FAN_LEVEL5 = "Level 5"

MODE_MAP: dict[str, HVACMode] = {
    "0000": HVACMode.FAN_ONLY,
    "0100": HVACMode.HEAT,
    "0200": HVACMode.COOL,
    "0300": HVACMode.AUTO,
    "0500": HVACMode.DRY,
}

FAN_MODE_MAP: dict[str, str] = {
    HAFanMode.FAN_AUTO: "0A00",
    HAFanMode.FAN_QUIET: "0B00",
    HAFanMode.FAN_LEVEL1: "0300",
    HAFanMode.FAN_LEVEL2: "0400",
    HAFanMode.FAN_LEVEL3: "0500",
    HAFanMode.FAN_LEVEL4: "0600",
    HAFanMode.FAN_LEVEL5: "0700",
}
REVERSE_FAN_MODE_MAP: dict[str, str] = {v: k for k, v in FAN_MODE_MAP.items()}

HVAC_TO_TEMP_HEX: dict[HVACMode, str | None] = {
    HVACMode.COOL: "p_02",
    HVACMode.HEAT: "p_03",
    HVACMode.AUTO: "p_02",
    HVACMode.FAN_ONLY: None,
    HVACMode.DRY: None,
}

HVAC_MODE_TO_FAN_SPEED_ATTR_NAME: dict[HVACMode, str | None] = {
    HVACMode.COOL: "p_09",
    HVACMode.HEAT: "p_09",
    HVACMode.FAN_ONLY: "p_09",
    HVACMode.AUTO: "p_09",
    HVACMode.DRY: None,
}

HVAC_MODE_TO_SWING_ATTR_NAMES: dict[HVACMode, tuple[str | None, str | None]] = {
    HVACMode.COOL: ("p_05", "p_06"),
    HVACMode.HEAT: ("p_05", "p_06"),
    HVACMode.AUTO: ("p_05", "p_06"),
    HVACMode.FAN_ONLY: ("p_05", "p_06"),
    HVACMode.DRY: ("p_05", "p_06"),
}

TURN_OFF_SWING_AXIS = "000000"
TURN_ON_SWING_AXIS = "0F0000"

VANE_VERTICAL_TO_HEX = {
    "Off": "000000", "Auto": "100000", "Swing": "0F0000", "Circulation": "140000",
    "1": "010000", "2": "020000", "3": "030000", "4": "040000", "5": "050000", "6": "060000",
    "Top": "010000", "Upper": "020000", "Upper-Middle": "030000",
    "Lower-Middle": "040000", "Lower": "050000", "Bottom": "060000",
}
VANE_HEX_TO_VERTICAL = {
    "000000": "Off", "100000": "Auto", "0F0000": "Swing", "140000": "Circulation",
    "010000": "Top", "020000": "Upper", "030000": "Upper-Middle",
    "040000": "Lower-Middle", "050000": "Lower", "060000": "Bottom",
}
VANE_HORIZONTAL_TO_HEX = {
    "Off": "000000", "Swing": "0F0000",
    "Left": "010000", "Left-Center": "020000", "Center": "030000",
    "Right-Center": "040000", "Right": "050000",
}
VANE_HEX_TO_HORIZONTAL = {v: k for k, v in VANE_HORIZONTAL_TO_HEX.items()}

# ===== 資料打包輔助 =====
@dataclass
class DaikinAttribute:
    name: str
    value: str
    path: list[str]
    to: str
    def format(self) -> dict:
        return {"pn": self.name, "pv": self.value}

@dataclass
class DaikinRequest:
    attributes: list[DaikinAttribute] = field(default_factory=list)
    def serialize(self, payload: dict | None = None) -> dict:
        if payload is None:
            payload = {"requests": []}
        def ensure_request(to: str) -> dict:
            for req in payload["requests"]:
                if req.get("to") == to:
                    return req
            last = to.rsplit("/", 1)[-1]
            root_pn = last.split(".")[-1] or last
            req = {"op": 3, "to": to, "pc": {"pn": root_pn, "pch": []}}
            payload["requests"].append(req)
            return req
        for attr in self.attributes:
            req = ensure_request(attr.to)
            node = req["pc"]
            for key in attr.path:
                found = None
                for ch in node.setdefault("pch", []):
                    if ch.get("pn") == key:
                        found = ch
                        break
                if not found:
                    found = {"pn": key, "pch": []}
                    node["pch"].append(found)
                node = found
            children = node.setdefault("pch", [])
            for ch in children:
                if ch.get("pn") == attr.name:
                    ch["pv"] = attr.value
                    break
            else:
                children.append(attr.format())
        return payload

# ===== 主要 Entity =====
class LocalDaikinClimate(ClimateEntity):
    def __init__(self, ip_address: str, name: str | None = None) -> None:
        self._ip = ip_address
        self._name = name or f"Local Daikin ({ip_address})"
        self._attr_unique_id = f"daikin_climate_{ip_address}"
        self.url = f"http://{ip_address}/dsiot/multireq"

        # 狀態
        self._hvac_mode: HVACMode = HVACMode.OFF
        self._fan_mode: str = HAFanMode.FAN_AUTO
        self._swing_mode: str = SWING_OFF
        self._target_temperature: float | None = 26.0
        self._current_temperature: float | None = None
        self._current_humidity: int | None = None
        self._outside_temperature: float | None = None
        self._energy_today: float | None = None
        self._energy_yesterday: float | None = None
        self._energy_week_total: float | None = None
        self._runtime_today: int | None = None
        self._mac: str | None = None

        # 濕度控制（僅讀）
        self._cool_humidity_enabled: Optional[bool] = None
        self._cool_humidity_target: Optional[int] = None

        # 葉片
        self._vane_vert_hex: str | None = None
        self._vane_horz_hex: str | None = None
        self._vane_vert_attr: str | None = None
        self._vane_horz_attr: str | None = None

        self._max_temp = 30
        self._min_temp = 10
        self._attr_available = True

        self._fail_count = 0
        self._next_retry = 0.0

        self._attr_hvac_modes = [HVACMode.OFF, HVACMode.HEAT, HVACMode.COOL, HVACMode.AUTO, HVACMode.DRY, HVACMode.FAN_ONLY]
        self._attr_fan_modes = [
            HAFanMode.FAN_QUIET, HAFanMode.FAN_AUTO,
            HAFanMode.FAN_LEVEL1, HAFanMode.FAN_LEVEL2, HAFanMode.FAN_LEVEL3, HAFanMode.FAN_LEVEL4, HAFanMode.FAN_LEVEL5
        ]
        self._attr_swing_modes = [SWING_OFF, SWING_BOTH, SWING_VERTICAL, SWING_HORIZONTAL]
        self._attr_target_temperature_step = 0.5
        self._attr_precision = 0.5

    # ===== HA 屬性 =====
    @property
    def name(self) -> str:
        return self._name

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, f"daikin-{self._ip}")},
            name=self._name,
            manufacturer="Daikin",
            model=f"Local API (/dsiot) @ {self._ip}",
            configuration_url=f"http://{self._ip}",
        )

    @property
    def temperature_unit(self) -> str:
        return UnitOfTemperature.CELSIUS

    @property
    def min_temp(self) -> float:
        return self._min_temp

    @property
    def max_temp(self) -> float:
        return self._max_temp

    @property
    def target_temperature(self) -> float | None:
        return self._target_temperature

    @property
    def current_temperature(self) -> float | None:
        return self._current_temperature

    @property
    def hvac_mode(self) -> str:
        return self._hvac_mode.value

    @property
    def hvac_modes(self) -> list[str]:
        return [m.value for m in self._attr_hvac_modes]

    @property
    def fan_mode(self) -> str:
        return self._fan_mode

    @property
    def fan_modes(self) -> list[str]:
        return [m.value for m in self._attr_fan_modes]

    @property
    def swing_mode(self) -> str:
        return self._swing_mode

    @property
    def swing_modes(self) -> list[str]:
        return self._attr_swing_modes

    @property
    def should_poll(self) -> bool:
        return True

    @property
    def available(self) -> bool:
        return self._attr_available

    @property
    def supported_features(self) -> int:
        # 不宣告 PRESET_MODE（避免 UI 出現 humidity_control 按鈕）
        return (
            ClimateEntityFeature.TARGET_TEMPERATURE
            | ClimateEntityFeature.FAN_MODE
            | ClimateEntityFeature.SWING_MODE
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        attrs = {
            "ip": self._ip,
            "outside_temperature": self._outside_temperature,
            "current_humidity": self._current_humidity,
            "energy_today": self._energy_today,
            "energy_yesterday": self._energy_yesterday,
            "energy_week_total": self._energy_week_total,
            "runtime_today": self._runtime_today,
        }
        # 濕度控制：僅讀 → 供 sensor/選單顯示
        if self._cool_humidity_enabled is not None:
            attrs["cool_humidity_enabled"] = self._cool_humidity_enabled
        if self._cool_humidity_target is not None:
            attrs["cool_humidity_target"] = self._cool_humidity_target
        if self._vane_vert_attr and self._vane_vert_hex is not None:
            attrs[self._vane_vert_attr] = self._vane_vert_hex
        if self._vane_horz_attr and self._vane_horz_hex is not None:
            attrs[self._vane_horz_attr] = self._vane_horz_hex
        attrs["supports_vertical_vane"] = self._vane_vert_attr is not None
        attrs["supports_horizontal_vane"] = self._vane_horz_attr is not None
        return attrs

    @property
    def unique_id(self) -> str | None:
        return self._mac or self._attr_unique_id

    # ===== 對裝置的讀/寫 =====
    def _http(self, method: str, payload: dict) -> dict:
        if method == "POST":
            r = requests.post(self.url, json=payload, timeout=DEFAULT_TIMEOUT)
        elif method == "PUT":
            r = requests.put(self.url, json=payload, timeout=DEFAULT_TIMEOUT)
        else:
            raise ValueError(method)
        r.raise_for_status()
        return r.json()

    @staticmethod
    def find_value_by_pn(data: dict, fr: str, *keys):
        blocks = [x["pc"] for x in data["responses"] if x["fr"] == fr]
        cur = blocks
        for key in keys:
            found = None
            for pc in cur:
                if pc.get("pn") == key:
                    found = pc
                    break
            if found is None:
                raise Exception(f"Key {key} not found")
            cur = found.get("pch", [])
        return found.get("pv")

    @staticmethod
    def hex_to_temp(hex_value: str, divisor: int = 2) -> float:
        raw = int(hex_value[:2], 16)
        if raw >= 128:
            raw -= 256
        return round(raw / divisor, 1)

    def get_swing_state(self, data: dict) -> str:
        v_attr, h_attr = HVAC_MODE_TO_SWING_ATTR_NAMES.get(self._hvac_mode, (None, None))
        vert = horiz = None
        try:
            if v_attr:
                vert = self.find_value_by_pn(data, "/dsiot/edge/adr_0100.dgc_status", "dgc_status", "e_1002", "e_3001", v_attr)
            if h_attr:
                horiz = self.find_value_by_pn(data, "/dsiot/edge/adr_0100.dgc_status", "dgc_status", "e_1002", "e_3001", h_attr)
        except Exception:
            return SWING_OFF
        v_on = vert == TURN_ON_SWING_AXIS
        h_on = horiz == TURN_ON_SWING_AXIS
        if v_on and h_on:
            return SWING_BOTH
        if v_on:
            return SWING_VERTICAL
        if h_on:
            return SWING_HORIZONTAL
        return SWING_OFF

    def update(self) -> None:
        now = time.monotonic()
        if now < self._next_retry:
            return

        payload = {
            "requests": [
                {"op": 2, "to": "/dsiot/edge/adr_0100.dgc_status?filter=pv,pt,md"},
                {"op": 2, "to": "/dsiot/edge/adr_0200.dgc_status?filter=pv,pt,md"},
                {"op": 2, "to": "/dsiot/edge/adr_0100.i_power.week_power?filter=pv,pt,md"},
            ]
        }

        try:
            data = self._http("POST", payload)

            is_off = (
                self.find_value_by_pn(
                    data, "/dsiot/edge/adr_0100.dgc_status", "dgc_status", "e_1002", "e_A002", "p_01"
                ) == "00"
            )
            if is_off:
                self._hvac_mode = HVACMode.OFF
            else:
                mode_hex = self.find_value_by_pn(
                    data, "/dsiot/edge/adr_0100.dgc_status", "dgc_status", "e_1002", "e_3001", "p_01"
                )
                self._hvac_mode = MODE_MAP.get(mode_hex, HVACMode.COOL)

            self._outside_temperature = self.hex_to_temp(
                self.find_value_by_pn(
                    data, "/dsiot/edge/adr_0200.dgc_status", "dgc_status", "e_1003", "e_A00D", "p_01"
                )
            )

            prev = self._target_temperature
            candidates = []
            if self._hvac_mode == HVACMode.COOL:
                candidates = ["p_02", "p_04", "p_03"]
            elif self._hvac_mode == HVACMode.HEAT:
                candidates = ["p_03", "p_02", "p_04"]
            elif self._hvac_mode == HVACMode.AUTO:
                candidates = ["p_02", "p_03", "p_04"]
            found = None
            for attr in candidates:
                try:
                    val = self.find_value_by_pn(
                        data, "/dsiot/edge/adr_0100.dgc_status", "dgc_status", "e_1002", "e_3001", attr
                    )
                    if val is not None:
                        found = self.hex_to_temp(val)
                        break
                except Exception:
                    continue
            self._target_temperature = found if found is not None else prev

            self._current_temperature = self.hex_to_temp(
                self.find_value_by_pn(
                    data, "/dsiot/edge/adr_0100.dgc_status", "dgc_status", "e_1002", "e_A00B", "p_01"
                ),
                divisor=1,
            )

            fan_attr = HVAC_MODE_TO_FAN_SPEED_ATTR_NAME.get(self._hvac_mode)
            if fan_attr:
                hex_value = self.find_value_by_pn(
                    data, "/dsiot/edge/adr_0100.dgc_status", "dgc_status", "e_1002", "e_3001", fan_attr
                )
                self._fan_mode = REVERSE_FAN_MODE_MAP.get(hex_value, HAFanMode.FAN_AUTO)
            else:
                self._fan_mode = HAFanMode.FAN_AUTO

            # 目前濕度
            try:
                self._current_humidity = int(
                    self.find_value_by_pn(
                        data, "/dsiot/edge/adr_0100.dgc_status", "dgc_status", "e_1002", "e_A00B", "p_02"
                    ),
                    16,
                )
            except Exception:
                self._current_humidity = None

            # 葉片+Swing
            self._vane_vert_attr, self._vane_horz_attr = HVAC_MODE_TO_SWING_ATTR_NAMES.get(self._hvac_mode, (None, None))
            self._vane_vert_hex = self._vane_horz_hex = None
            if self._hvac_mode != HVACMode.OFF:
                try:
                    if self._vane_vert_attr:
                        self._vane_vert_hex = self.find_value_by_pn(
                            data, "/dsiot/edge/adr_0100.dgc_status", "dgc_status", "e_1002", "e_3001", self._vane_vert_attr
                        )
                    if self._vane_horz_attr:
                        self._vane_horz_hex = self.find_value_by_pn(
                            data, "/dsiot/edge/adr_0100.dgc_status", "dgc_status", "e_1002", "e_3001", self._vane_horz_attr
                        )
                except Exception:
                    pass
                v_on = self._vane_vert_hex == TURN_ON_SWING_AXIS
                h_on = self._vane_horz_hex == TURN_ON_SWING_AXIS
                self._swing_mode = SWING_BOTH if (v_on and h_on) else SWING_VERTICAL if v_on else SWING_HORIZONTAL if h_on else SWING_OFF
            else:
                self._swing_mode = SWING_OFF

            # 能源/運轉
            try:
                week_datas = self.find_value_by_pn(
                    data, "/dsiot/edge/adr_0100.i_power.week_power", "week_power", "datas"
                )
                if isinstance(week_datas, list) and week_datas:
                    # 正規化為 float
                    vals: list[float] = []
                    for v in week_datas:
                        try:
                            vals.append(float(v))
                        except Exception:
                            try:
                                vals.append(float(str(v)))
                            except Exception:
                                vals.append(0.0)
                    total = sum(vals)
                    all_zero = all(x == 0.0 for x in vals)
                    if all_zero:
                        self._energy_today = None
                        self._energy_yesterday = None
                        self._energy_week_total = None
                    else:
                        last = vals[-1]
                        self._energy_today = last if last > 0 else None
                        if len(vals) >= 2:
                            y = vals[-2]
                            self._energy_yesterday = y if y > 0 else None
                        else:
                            self._energy_yesterday = None
                        self._energy_week_total = total if total > 0 else None
                else:
                    self._energy_today = None
                    self._energy_yesterday = None
                    self._energy_week_total = None
            except Exception:
                self._energy_today = None
                self._energy_yesterday = None
                self._energy_week_total = None

            try:
                self._runtime_today = self.find_value_by_pn(
                    data, "/dsiot/edge/adr_0100.i_power.week_power", "week_power", "today_runtime"
                )
            except Exception:
                self._runtime_today = None

            # 濕度控制：讀 p_2C（01=啟用 / 00=關閉），與「僅讀」 p_1A（目標%）
            try:
                p2c = self.find_value_by_pn(
                    data, "/dsiot/edge/adr_0100.dgc_status", "dgc_status", "e_1002", "e_3003", "p_2C"
                )
                self._cool_humidity_enabled = (p2c == "01")
            except Exception:
                self._cool_humidity_enabled = None
            try:
                p1a = self.find_value_by_pn(
                    data, "/dsiot/edge/adr_0100.dgc_status", "dgc_status", "e_1002", "e_3003", "p_1A"
                )
                self._cool_humidity_target = int(p1a, 16) if p1a is not None else None
            except Exception:
                self._cool_humidity_target = None

            self._attr_available = True
            self._fail_count = 0
            self._next_retry = 0.0

        except Exception as err:
            self._attr_available = False
            self._fail_count = min(self._fail_count + 1, len(BACKOFFS))
            backoff = BACKOFFS[self._fail_count - 1]
            self._next_retry = now + backoff
            _LOGGER.warning("Local Daikin update failed: %s; backing off %ss", err, backoff)

    # ===== 控制命令（僅保留：溫度 / 風速 / Swing / 葉片） =====
    def update_attribute(self, request: dict) -> None:
        try:
            resp = self._http("POST", request)
            code = resp["responses"][0].get("rsc")
            if code not in (2000, 2004):
                _LOGGER.error("Unexpected response: %s", resp)
                return
            self.update()
        except Exception as err:
            self._attr_available = False
            self._fail_count = min(self._fail_count + 1, len(BACKOFFS))
            self._next_retry = time.monotonic() + BACKOFFS[self._fail_count - 1]
            _LOGGER.warning("update_attribute failed: %s", err)
            _LOGGER.warning("Request: %s", request)

    def set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        if hvac_mode == HVACMode.OFF:
            req_off = DaikinRequest([
                DaikinAttribute("p_01", "00", ["e_1002", "e_A002"], "/dsiot/edge/adr_0100.dgc_status"),
            ]).serialize()
            self.update_attribute(req_off)
            return

        mode_hex = None
        for h, m in MODE_MAP.items():
            if m == hvac_mode:
                mode_hex = h
                break
        if mode_hex is None:
            _LOGGER.error("Unknown HVAC mode: %s", hvac_mode)
            return

        req_on = DaikinRequest([
            DaikinAttribute("p_01", "01", ["e_1002", "e_A002"], "/dsiot/edge/adr_0100.dgc_status"),
        ]).serialize()
        self.update_attribute(req_on)

        req_mode = DaikinRequest([
            DaikinAttribute("p_01", mode_hex, ["e_1002", "e_3001"], "/dsiot/edge/adr_0100.dgc_status"),
        ]).serialize()
        self.update_attribute(req_mode)

    def set_fan_mode(self, fan_mode: str) -> None:
        name = HVAC_MODE_TO_FAN_SPEED_ATTR_NAME.get(self._hvac_mode)
        if not name:
            _LOGGER.debug("Fan speed not applicable in %s", self._hvac_mode)
            return
        hexv = FAN_MODE_MAP.get(fan_mode, FAN_MODE_MAP[HAFanMode.FAN_AUTO])
        req = DaikinRequest([
            DaikinAttribute(name, hexv, ["e_1002", "e_3001"], "/dsiot/edge/adr_0100.dgc_status"),
        ]).serialize()
        self.update_attribute(req)

    def set_temperature(self, **kwargs) -> None:
        temp = kwargs.get("temperature")
        if temp is None:
            return
        attr = HVAC_TO_TEMP_HEX.get(self._hvac_mode)
        if not attr:
            _LOGGER.error("Cannot set temperature in %s mode.", self._hvac_mode)
            return
        c = float(temp)
        raw = int(round(c * 2))
        hexv = f"{raw & 0xFF:02X}0000"
        req = DaikinRequest([
            DaikinAttribute(attr, hexv, ["e_1002", "e_3001"], "/dsiot/edge/adr_0100.dgc_status"),
        ]).serialize()
        self.update_attribute(req)

    async def async_set_temperature(self, **kwargs) -> None:
        job = partial(self.set_temperature, **kwargs)
        await self.hass.async_add_executor_job(job)

    # Swing（UI 用）
    def set_swing_mode(self, swing_mode: str) -> None:
        v_attr, h_attr = "p_05", "p_06"
        v = h = TURN_OFF_SWING_AXIS
        mode = (swing_mode or "").lower()
        if mode == SWING_BOTH:
            v = h = TURN_ON_SWING_AXIS
        elif mode == SWING_VERTICAL:
            v = TURN_ON_SWING_AXIS
        elif mode == SWING_HORIZONTAL:
            h = TURN_ON_SWING_AXIS
        req = DaikinRequest([
            DaikinAttribute(v_attr, v, ["e_1002", "e_3001"], "/dsiot/edge/adr_0100.dgc_status"),
            DaikinAttribute(h_attr, h, ["e_1002", "e_3001"], "/dsiot/edge/adr_0100.dgc_status"),
        ]).serialize()
        self.update_attribute(req)

    async def async_set_swing_mode(self, swing_mode: str) -> None:
        job = partial(self.set_swing_mode, swing_mode)
        await self.hass.async_add_executor_job(job)

    # 葉片（供 select 呼叫）
    def set_vane_position(self, vertical: Optional[str] = None, horizontal: Optional[str] = None) -> None:
        attrs: list[DaikinAttribute] = []
        if vertical:
            if vertical not in VANE_VERTICAL_TO_HEX:
                _LOGGER.warning("Unknown vertical vane: %s", vertical); return
            attrs.append(DaikinAttribute("p_05", VANE_VERTICAL_TO_HEX[vertical], ["e_1002","e_3001"], "/dsiot/edge/adr_0100.dgc_status"))
        if horizontal:
            if horizontal not in VANE_HORIZONTAL_TO_HEX:
                _LOGGER.warning("Unknown horizontal vane: %s", horizontal); return
            attrs.append(DaikinAttribute("p_06", VANE_HORIZONTAL_TO_HEX[horizontal], ["e_1002","e_3001"], "/dsiot/edge/adr_0100.dgc_status"))
        if not attrs:
            return
        req = DaikinRequest(attrs).serialize()
        self.update_attribute(req)

    async def async_set_vane_position(self, **kwargs) -> None:
        job = partial(self.set_vane_position, **kwargs)
        await self.hass.async_add_executor_job(job)

    async def async_added_to_hass(self) -> None:
        self.hass.async_create_task(self.initialize_unique_id(self.hass))

    async def initialize_unique_id(self, hass) -> None:
        try:
            # 可在此填入使用 /common/basic_info 讀 MAC → 設置 self._mac
            pass
        except Exception:
            pass

# ---------------------------------------------------------------------------
# platform setup
# ---------------------------------------------------------------------------
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

def _get_host(entry: ConfigEntry) -> str:
    return (
        entry.data.get("host")
        or entry.data.get("ip")
        or entry.data.get("ip_address")
        or entry.options.get("host")
        or entry.options.get("ip")
        or entry.options.get("ip_address")
    )

async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    host = _get_host(entry)
    name = entry.title or f"Local Daikin ({host})"

    entity = LocalDaikinClimate(host, name)
    async_add_entities([entity], update_before_add=True)

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN].setdefault(entry.entry_id, {})
    hass.data[DOMAIN][entry.entry_id]["climate_entity"] = entity

    # 綁定 entity_id（安全檢查 + 延遲重試）
    attempts = {"n": 0}
    def _bind_pointer() -> None:
        ent_id = getattr(entity, "entity_id", None)
        if not ent_id:
            attempts["n"] += 1
            if attempts["n"] < 50:
                hass.loop.call_later(0.2, _bind_pointer)
            return
        st = hass.states.get(ent_id)
        if st:
            hass.data[DOMAIN][entry.entry_id]["climate_entity_id"] = ent_id
            return
        attempts["n"] += 1
        if attempts["n"] < 50:
            hass.loop.call_later(0.2, _bind_pointer)
    hass.loop.call_later(0.2, _bind_pointer)

    # 註冊葉片定位服務（select 會呼叫）
    platform = entity_platform.async_get_current_platform()
    platform.async_register_entity_service(
        "set_vane_position",
        vol.Schema(
            {
                vol.Optional("vertical"): cv.string,
                vol.Optional("horizontal"): cv.string,
            }
        ),
        "async_set_vane_position",
    )
