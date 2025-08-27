import logging
import time
import requests
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Optional

from homeassistant.components.climate import ClimateEntity, ClimateEntityFeature
from homeassistant.components.climate.const import (
    HVACMode,
    SWING_OFF,
    SWING_BOTH,
    SWING_VERTICAL,
    SWING_HORIZONTAL,
)
from homeassistant.const import UnitOfTemperature
from homeassistant.helpers.device_registry import format_mac
from homeassistant.helpers import entity_platform
import voluptuous as vol

_LOGGER = logging.getLogger(__name__)

# ===== 可調參數 =====
DEFAULT_TIMEOUT = 5  # HTTP 逾時秒數
BACKOFFS = [10, 30, 60, 120, 300]  # 退避階梯（秒），成功一次就歸零

# ===== 對照表 =====
class HAFanMode(StrEnum):
    FAN_QUIET = "Quiet"
    FAN_AUTO = "Auto"
    FAN_LEVEL1 = "Level 1"
    FAN_LEVEL2 = "Level 2"
    FAN_LEVEL3 = "Level 3"
    FAN_LEVEL4 = "Level 4"
    FAN_LEVEL5 = "Level 5"

# 模式 hex → HVACMode
MODE_MAP: dict[str, HVACMode] = {
    "0000": HVACMode.FAN_ONLY,
    "0100": HVACMode.HEAT,
    "0200": HVACMode.COOL,
    "0300": HVACMode.AUTO,
    "0500": HVACMode.DRY,
}

# 風速名稱 → hex（多數機種 p_09）
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

# 各模式對應其「風速屬性名」；有些模式（如 DRY）不允許改風速 → None
HVAC_MODE_TO_FAN_SPEED_ATTR_NAME: dict[HVACMode, str | None] = {
    HVACMode.COOL: "p_09",
    HVACMode.HEAT: "p_09",
    HVACMode.FAN_ONLY: "p_09",
    HVACMode.AUTO: "p_09",
    HVACMode.DRY: None,
}

# 各模式對應其目標溫度屬性名（℃×2 的十六進位字串）
HVAC_TO_TEMP_HEX: dict[HVACMode, str | None] = {
    HVACMode.COOL: "p_02",
    HVACMode.HEAT: "p_03",
    HVACMode.AUTO: "p_1D",  # 有些機種支援 AUTO 設溫
    HVACMode.DRY: None,
    HVACMode.FAN_ONLY: None,
}

# 各模式對應（垂直, 水平）葉片屬性名（最常見：COOL 用 p_05/p_06）
HVAC_MODE_TO_SWING_ATTR_NAMES: dict[HVACMode, tuple[str | None, str | None]] = {
    HVACMode.COOL: ("p_05", "p_06"),
    HVACMode.HEAT: ("p_05", "p_06"),
    HVACMode.AUTO: ("p_05", "p_06"),
    HVACMode.FAN_ONLY: ("p_05", "p_06"),
    HVACMode.DRY: ("p_05", "p_06"),  # 某些機型不支援 → 之後可偵測 available
}

TURN_OFF_SWING_AXIS = "000000"
TURN_ON_SWING_AXIS = "0F0000"

# ---- 冷房濕度控制（p_0C / p_0B）對照 ----
HUMIDITY_TARGET_TO_HEX = {50: "0A", 55: "0B", 60: "0C"}
HEX_TO_HUMIDITY_TARGET = {v: k for k, v in HUMIDITY_TARGET_TO_HEX.items()}
HUMIDITY_CONTROL_MAP = {
    "00": "off",
    "01": "on",
    "06": "continuous",
}

# ---- 葉片位置（選單用）----
# 垂直：新增友善名稱；同時保留數字 1..6 當同義詞
VANE_VERTICAL_TO_HEX = {
    "Off": "000000", "Auto": "100000", "Swing": "0F0000", "Circulation": "140000",
    # 數字同義詞
    "1": "010000", "2": "020000", "3": "030000", "4": "040000", "5": "050000", "6": "060000",
    # 友善名稱（顯示用）
    "Top": "010000",
    "Upper": "020000",
    "Upper-Middle": "030000",
    "Lower-Middle": "040000",
    "Lower": "050000",
    "Bottom": "060000",
}
# 顯示時優先用友善名稱
VANE_HEX_TO_VERTICAL = {
    "000000": "Off", "100000": "Auto", "0F0000": "Swing", "140000": "Circulation",
    "010000": "Top", "020000": "Upper", "030000": "Upper-Middle",
    "040000": "Lower-Middle", "050000": "Lower", "060000": "Bottom",
}
# 水平葉片：增列固定角度（部分機型支援）
VANE_HORIZONTAL_TO_HEX = {
    "Off": "000000",
    "Swing": "0F0000",
    "Left": "010000",
    "Left-Center": "020000",
    "Center": "030000",
    "Right-Center": "040000",
    "Right": "050000",
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
            req = {"op": 3, "to": to, "pc": {"pch": []}}
            payload["requests"].append(req)
            return req

        for attr in self.attributes:
            req = ensure_request(attr.to)
            pc = req["pc"]
            # 走 path（e_1002 → e_3001 等），最後把 name/pv 塞進 pch
            node = pc
            for key in attr.path:
                # 找或建構該層
                found = None
                if "pch" in node:
                    for ch in node["pch"]:
                        if ch.get("pn") == key:
                            found = ch
                            break
                if not found:
                    found = {"pn": key, "pch": []}
                    node.setdefault("pch", []).append(found)
                node = found
            # 塞入最終屬性
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
    def __init__(self, ip_address: str) -> None:
        self._ip = ip_address
        self._name = f"Local Daikin ({ip_address})"
        self._attr_unique_id = f"daikin_climate_{ip_address}"
        self.url = f"http://{ip_address}/dsiot/multireq"

        # 狀態
        self._hvac_mode: HVACMode = HVACMode.OFF
        self._fan_mode: str = HAFanMode.FAN_AUTO
        self._swing_mode: str = SWING_OFF
        self._target_temperature: float | None = None
        self._current_temperature: float | None = None
        self._current_humidity: int | None = None
        self._outside_temperature: float | None = None
        self._energy_today: float | None = None
        self._runtime_today: int | None = None
        self._mac: str | None = None
        # 濕度控制狀態（原始十六進位；供 switch 讀取）
        self._p0c_humidity_ctrl: Optional[str] = None   # "00"/"01"/"06"
        self._p0b_humidity_target: Optional[str] = None # "0A"/"0B"/"0C"
        # 葉片位置（原始 hex，及屬性名）
        self._vane_vert_hex: str | None = None
        self._vane_horz_hex: str | None = None
        self._vane_vert_attr: str | None = None
        self._vane_horz_attr: str | None = None

        # 其他屬性
        self._max_temp = 30
        self._min_temp = 10
        self._attr_available = True

        # 退避
        self._fail_count = 0
        self._next_retry = 0.0

        # 提供給 select 用的選單
        self._attr_hvac_modes = [HVACMode.OFF, HVACMode.HEAT, HVACMode.COOL, HVACMode.AUTO, HVACMode.DRY, HVACMode.FAN_ONLY]
        self._attr_fan_modes = [
            HAFanMode.FAN_QUIET, HAFanMode.FAN_AUTO,
            HAFanMode.FAN_LEVEL1, HAFanMode.FAN_LEVEL2, HAFanMode.FAN_LEVEL3, HAFanMode.FAN_LEVEL4, HAFanMode.FAN_LEVEL5
        ]
        self._attr_swing_modes = [SWING_OFF, SWING_BOTH, SWING_VERTICAL, SWING_HORIZONTAL]

    # ===== HA 規定的屬性/方法 =====
    @property
    def name(self) -> str:
        return self._name

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
        return True  # 你也可以改成 coordinator 後關閉輪詢

    @property
    def available(self) -> bool:
        return self._attr_available

    @property
    def supported_features(self) -> int:
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
            "runtime_today": self._runtime_today,
        }
        # 冷房濕度控制（額外曝露）
        if self._p0c_humidity_ctrl is not None:
            attrs["p_0C"] = self._p0c_humidity_ctrl
            attrs["cool_humidity_control"] = HUMIDITY_CONTROL_MAP.get(self._p0c_humidity_ctrl, "off")
        if self._p0b_humidity_target is not None:
            attrs["p_0B"] = self._p0b_humidity_target
            attrs["cool_humidity_target"] = HEX_TO_HUMIDITY_TARGET.get(self._p0b_humidity_target)
        # 暴露目前模式對應之葉片欄位與數值（供 select 讀）
        if self._vane_vert_attr and self._vane_vert_hex is not None:
            attrs[self._vane_vert_attr] = self._vane_vert_hex
        if self._vane_horz_attr and self._vane_horz_hex is not None:
            attrs[self._vane_horz_attr] = self._vane_horz_hex
        # 能力旗標（即使關機也存在）
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
        # 從 responses 中挑出 fr 匹配者，沿 pn/pch 向下找
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
        # 最終層的 pv
        return found.get("pv")

    @staticmethod
    def hex_to_temp(hex_value: str, divisor: int = 2) -> float:
        # 兩位十六進位，值=℃×divisor（常見 divisor=2）
        raw = int(hex_value[:2], 16)
        if raw >= 128:  # 裝置有時用補碼表示負數
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
        """Fetch new state data for the entity（含退避）."""
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
            # HVAC 模式 / 電源
            is_off = (
                self.find_value_by_pn(
                    data, "/dsiot/edge/adr_0100.dgc_status", "dgc_status", "e_1002", "e_A002", "p_01"
                )
                == "00"
            )
            if is_off:
                self._hvac_mode = HVACMode.OFF
            else:
                mode_hex = self.find_value_by_pn(
                    data, "/dsiot/edge/adr_0100.dgc_status", "dgc_status", "e_1002", "e_3001", "p_01"
                )
                self._hvac_mode = MODE_MAP.get(mode_hex, HVACMode.COOL)

            # 溫度（室外/室內/目標）
            self._outside_temperature = self.hex_to_temp(
                self.find_value_by_pn(
                    data, "/dsiot/edge/adr_0200.dgc_status", "dgc_status", "e_1003", "e_A00D", "p_01"
                )
            )
            temp_attr = HVAC_TO_TEMP_HEX.get(self._hvac_mode)
            if temp_attr:
                try:
                    self._target_temperature = self.hex_to_temp(
                        self.find_value_by_pn(
                            data, "/dsiot/edge/adr_0100.dgc_status", "dgc_status", "e_1002", "e_3001", temp_attr
                        )
                    )
                except Exception:
                    self._target_temperature = None
            else:
                self._target_temperature = None

            self._current_temperature = self.hex_to_temp(
                self.find_value_by_pn(
                    data, "/dsiot/edge/adr_0100.dgc_status", "dgc_status", "e_1002", "e_A00B", "p_01"
                ),
                divisor=1,  # 有些機種內溫就是 1 倍
            )

            # 風速
            fan_attr = HVAC_MODE_TO_FAN_SPEED_ATTR_NAME.get(self._hvac_mode)
            if fan_attr:
                hex_value = self.find_value_by_pn(
                    data, "/dsiot/edge/adr_0100.dgc_status", "dgc_status", "e_1002", "e_3001", fan_attr
                )
                self._fan_mode = REVERSE_FAN_MODE_MAP.get(hex_value, HAFanMode.FAN_AUTO)
            else:
                self._fan_mode = HAFanMode.FAN_AUTO

            # 濕度
            try:
                self._current_humidity = int(
                    self.find_value_by_pn(
                        data, "/dsiot/edge/adr_0100.dgc_status", "dgc_status", "e_1002", "e_A00B", "p_02"
                    ),
                    16,
                )
            except Exception:
                self._current_humidity = None

            # 葉片目前 hex（同時推導 swing 狀態）
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
                    self._energy_today = week_datas[-1]
            except Exception:
                self._energy_today = None

            try:
                self._runtime_today = self.find_value_by_pn(
                    data, "/dsiot/edge/adr_0100.i_power.week_power", "week_power", "today_runtime"
                )
            except Exception:
                self._runtime_today = None

            # 冷房濕度控制（只要裝置回傳就解析，不強制檢查模式）
            try:
                self._p0c_humidity_ctrl = self.find_value_by_pn(
                    data, "/dsiot/edge/adr_0100.dgc_status", "dgc_status", "e_1002", "e_3001", "p_0C"
                )
            except Exception:
                self._p0c_humidity_ctrl = None
            try:
                self._p0b_humidity_target = self.find_value_by_pn(
                    data, "/dsiot/edge/adr_0100.dgc_status", "dgc_status", "e_1002", "e_3001", "p_0B"
                )
            except Exception:
                self._p0b_humidity_target = None

            # 成功：恢復 available / 清退避
            self._attr_available = True
            self._fail_count = 0
            self._next_retry = 0.0

        except Exception as err:
            # 失敗：標記 unavailable + 退避，不往外拋
            self._attr_available = False
            self._fail_count = min(self._fail_count + 1, len(BACKOFFS))
            backoff = BACKOFFS[self._fail_count - 1]
            self._next_retry = now + backoff
            _LOGGER.warning("Local Daikin update failed: %s; backing off %ss", err, backoff)

    # ===== 控制命令 =====
    def update_attribute(self, request: dict) -> None:
        """送設定命令（失敗時退避），成功後刷新一次狀態。"""
        try:
            # multireq 寫入也走 POST（較通用）
            resp = self._http("POST", request)
            # 有些機種是以 2004 表示設定成功
            code = resp["responses"][0].get("rsc")
            if code != 2004:
                _LOGGER.error("Unexpected response: %s", resp)
                return
            # 寫入後抓一次最新狀態
            self.update()
        except Exception as err:
            self._attr_available = False
            self._fail_count = min(self._fail_count + 1, len(BACKOFFS))
            self._next_retry = time.monotonic() + BACKOFFS[self._fail_count - 1]
            _LOGGER.warning("update_attribute failed: %s", err)

    # -------- 冷房濕度控制：實體服務 --------
    async def async_set_cool_humidity_control(
        self,
        enabled: bool,
        target: Optional[int] = None,
        continuous: Optional[bool] = False,
    ) -> None:
        """
        設定冷房濕度控制：
          enabled=False  -> p_0C="00"
          enabled=True   -> p_0C="01"（或 continuous=True -> "06"）
          target 可選 50/55/60（對應 p_0B）
        """
        # 組 p_0C
        if not enabled:
            p0c = "00"
        else:
            p0c = "06" if continuous else "01"
        attrs = [DaikinAttribute("p_2D", "02", ["e_1002","e_3003"], "/dsiot/edge/adr_0100.dgc_status"),
                 DaikinAttribute("p_0C", p0c, ["e_1002","e_3001"], "/dsiot/edge/adr_0100.dgc_status")]
        # 組 p_0B（若提供）
        if target is not None:
            hex_target = HUMIDITY_TARGET_TO_HEX.get(int(target))
            if hex_target:
                attrs.append(DaikinAttribute("p_0B", hex_target, ["e_1002","e_3001"], "/dsiot/edge/adr_0100.dgc_status"))
        self.update_attribute(DaikinRequest(attrs).serialize())
        await self.hass.async_add_executor_job(
            self.update_attribute, DaikinRequest(attrs).serialize()
        )


    # ========== 葉片定位：實體服務 ==========
    async def async_set_vane_position(self, vertical: str | None = None, horizontal: str | None = None) -> None:
        """設定葉片位置：vertical 可為 Auto/Swing/Circulation/1..6/Off；horizontal 可為 Off/Swing"""
        if not vertical and not horizontal:
            return
        v_attr, h_attr = HVAC_MODE_TO_SWING_ATTR_NAMES.get(self._hvac_mode, (None, None))
        attrs: list[DaikinAttribute] = [DaikinAttribute("p_2D", "02", ["e_1002","e_3003"], "/dsiot/edge/adr_0100.dgc_status")]
        if vertical and v_attr:
            hexv = VANE_VERTICAL_TO_HEX.get(vertical)
            if hexv:
                attrs.append(DaikinAttribute(v_attr, hexv, ["e_1002","e_3001"], "/dsiot/edge/adr_0100.dgc_status"))
        if horizontal and h_attr:
            hexh = VANE_HORIZONTAL_TO_HEX.get(horizontal)
            if hexh:
                attrs.append(DaikinAttribute(h_attr, hexh, ["e_1002","e_3001"], "/dsiot/edge/adr_0100.dgc_status"))
        if len(attrs) > 1:
            await self.hass.async_add_executor_job(
                self.update_attribute, DaikinRequest(attrs).serialize()
            )

    async def async_added_to_hass(self) -> None:
        # 初始化 MAC 做為 unique_id（非阻塞）
        self.hass.async_create_task(self.initialize_unique_id(self.hass))

    def set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        if hvac_mode == HVACMode.OFF:
            req = DaikinRequest([
                DaikinAttribute("p_01", "00", ["e_1002", "e_A002"], "/dsiot/edge/adr_0100.dgc_status"),
                DaikinAttribute("p_2D", "01", ["e_1002", "e_3003"], "/dsiot/edge/adr_0100.dgc_status"),  # 停止
            ]).serialize()
            self.update_attribute(req)
            return

        mode_hex = None
        for h, m in MODE_MAP.items():
            if m == hvac_mode:
                mode_hex = h
                break
        if mode_hex is None:
            _LOGGER.error("Unknown HVAC mode: %s", hvac_mode)
            return

        req = DaikinRequest([
            DaikinAttribute("p_01", "01", ["e_1002", "e_A002"], "/dsiot/edge/adr_0100.dgc_status"),  # 開機
            DaikinAttribute("p_01", mode_hex, ["e_1002", "e_3001"], "/dsiot/edge/adr_0100.dgc_status"),
            DaikinAttribute("p_2D", "02", ["e_1002", "e_3003"], "/dsiot/edge/adr_0100.dgc_status"),  # 設定變更
        ]).serialize()
        self.update_attribute(req)

    def set_fan_mode(self, fan_mode: str) -> None:
        name = HVAC_MODE_TO_FAN_SPEED_ATTR_NAME.get(self._hvac_mode)
        if not name:
            _LOGGER.debug("Fan speed not applicable in %s", self._hvac_mode)
            return
        hexv = FAN_MODE_MAP.get(fan_mode, FAN_MODE_MAP[HAFanMode.FAN_AUTO])
        req = DaikinRequest([
            DaikinAttribute("p_2D", "02", ["e_1002", "e_3003"], "/dsiot/edge/adr_0100.dgc_status"),
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
        # ℃×2 → 兩位十六進位字串（左側補 0）
        v = max(self._min_temp, min(self._max_temp, float(temp)))
        hexv = f"{int(round(v * 2)) & 0xFF:02X}0000"
        req = DaikinRequest([
            DaikinAttribute("p_2D", "02", ["e_1002", "e_3003"], "/dsiot/edge/adr_0100.dgc_status"),
            DaikinAttribute(attr, hexv, ["e_1002", "e_3001"], "/dsiot/edge/adr_0100.dgc_status"),
        ]).serialize()
        self.update_attribute(req)

    def set_swing_mode(self, swing_mode: str) -> None:
        v_attr, h_attr = HVAC_MODE_TO_SWING_ATTR_NAMES.get(self._hvac_mode, (None, None))
        if not v_attr and not h_attr:
            _LOGGER.debug("Swing not applicable in %s", self._hvac_mode)
            return
        v_cmd = h_cmd = TURN_OFF_SWING_AXIS
        if swing_mode in (SWING_BOTH, SWING_VERTICAL):
            v_cmd = TURN_ON_SWING_AXIS
        if swing_mode in (SWING_BOTH, SWING_HORIZONTAL):
            h_cmd = TURN_ON_SWING_AXIS

        attrs: list[DaikinAttribute] = [DaikinAttribute("p_2D", "02", ["e_1002", "e_3003"], "/dsiot/edge/adr_0100.dgc_status")]
        if v_attr:
            attrs.append(DaikinAttribute(v_attr, v_cmd, ["e_1002", "e_3001"], "/dsiot/edge/adr_0100.dgc_status"))
        if h_attr:
            attrs.append(DaikinAttribute(h_attr, h_cmd, ["e_1002", "e_3001"], "/dsiot/edge/adr_0100.dgc_status"))
        self.update_attribute(DaikinRequest(attrs).serialize())

    async def initialize_unique_id(self, hass) -> None:
        payload = {"requests": [{"op": 2, "to": "/dsiot/edge.adp_i"}]}
        def _call():
            return requests.post(self.url, json=payload, timeout=DEFAULT_TIMEOUT)
        try:
            resp = await hass.async_add_executor_job(_call)
            resp.raise_for_status()
            data = resp.json()
            mac = self.find_value_by_pn(data, "/dsiot/edge.adp_i", "adp_i", "mac")
            self._mac = format_mac(mac)
        except Exception as err:
            _LOGGER.warning("Init unique_id failed: %s; fallback to IP-based unique_id", err)
            self._mac = self._attr_unique_id

# ---- 在平台 setup 時註冊本實體的「濕度控制」服務 ----
async def async_setup_entry(hass, entry, async_add_entities):
    host = entry.data.get("host") or entry.data.get("ip") or entry.data.get("ip_address")
    ent = LocalDaikinClimate(host)
    async_add_entities([ent], update_before_add=True)

    platform = entity_platform.current_platform.get()
    reg = hass.data.setdefault(DOMAIN, {}).get("services_registered")
    if not reg:
        hass.data[DOMAIN]["services_registered"] = True
        # 註冊 entity service：climate.set_cool_humidity_control
        platform.async_register_entity_service(
            "set_cool_humidity_control",
            vol.Schema({
                vol.Required("enabled"): bool,
                vol.Optional("target"): vol.In([50, 55, 60]),
                vol.Optional("continuous", default=False): bool,
            }),
            "async_set_cool_humidity_control",
        )

    # 註冊：葉片定位（至少一個參數）
    platform.async_register_entity_service(
        "set_vane_position",
        vol.Schema({
            vol.Optional("vertical"): vol.In([
                "Auto","Swing","Circulation","Off",
                "1","2","3","4","5","6",
                "Top","Upper","Upper-Middle","Lower-Middle","Lower","Bottom",
            ]),
            vol.Optional("horizontal"): vol.In([
                "Off","Swing","Left","Left-Center","Center","Right-Center","Right"
            ]),
        }),
        "async_set_vane_position",
    )