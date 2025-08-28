from __future__ import annotations
from functools import partial

import logging
import time
import requests
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Optional

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
from homeassistant.helpers.device_registry import format_mac, DeviceInfo
from homeassistant.helpers import entity_platform
from homeassistant.helpers import config_validation as cv
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

# 目標溫度所在欄位（多數 DSIoT：COOL→p_02, HEAT→p_03；AUTO 先沿用 COOL）
HVAC_TO_TEMP_HEX: dict[HVACMode, str | None] = {
    HVACMode.COOL: "p_02",
    HVACMode.HEAT: "p_03",
    HVACMode.AUTO: "p_02",
    HVACMode.FAN_ONLY: None,
    HVACMode.DRY: None,
}

# 各模式對應其「風速屬性名」；有些模式（如 DRY）不允許改風速 → None
HVAC_MODE_TO_FAN_SPEED_ATTR_NAME: dict[HVACMode, str | None] = {
    HVACMode.COOL: "p_09",
    HVACMode.HEAT: "p_09",
    HVACMode.FAN_ONLY: "p_09",
    HVACMode.AUTO: "p_09",
    HVACMode.DRY: None,
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
            # 需包含根節點的 pn，例如 '/dsiot/edge/adr_0100.dgc_status' → pn='dgc_status'
            last = to.rsplit("/", 1)[-1]           # 'adr_0100.dgc_status'
            root_pn = last.split(".")[-1] or last  # 'dgc_status'
            req = {"op": 3, "to": to, "pc": {"pn": root_pn, "pch": []}}
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
    def __init__(self, ip_address: str, name: str | None = None) -> None:
        self._ip = ip_address
        self._name = name or f"Local Daikin ({ip_address})"
        self._attr_unique_id = f"daikin_climate_{ip_address}"
        self.url = f"http://{ip_address}/dsiot/multireq"

        # 狀態
        self._hvac_mode: HVACMode = HVACMode.OFF
        self._fan_mode: str = HAFanMode.FAN_AUTO
        self._swing_mode: str = SWING_OFF
        self._target_temperature: float | None = 26.0  # 預設 26°C，避免 UI 初始為未知而隱藏滑桿
        self._current_temperature: float | None = None
        self._current_humidity: int | None = None
        self._outside_temperature: float | None = None
        self._energy_today: float | None = None
        self._runtime_today: int | None = None
        self._mac: str | None = None

        # 濕度控制（新版：e_3003）
        self._cool_humidity_enabled: Optional[bool] = None
        self._cool_humidity_target: Optional[int] = None

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
        # 告訴前端可調溫，每步 0.5℃
        self._attr_target_temperature_step = 0.5
        self._attr_precision = 0.5

    # ===== HA 規定的屬性/方法 =====
    @property
    def name(self) -> str:
        return self._name

    @property
    def device_info(self) -> DeviceInfo:
        # 裝置名稱也跟著 entry.title
        return DeviceInfo(
            identifiers={(DOMAIN, f"daikin-{self._ip}")},
            name=self._name,
            manufacturer="Daikin",
            model="Local API (/dsiot)",
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
        return True  # 你也可以改成 coordinator 後關閉輪詢

    @property
    def available(self) -> bool:
        return self._attr_available

    @property
    def supported_features(self) -> int:
        return (
            ClimateEntityFeature.TARGET_TEMPERATURE
            | ClimateEntityFeature.TARGET_HUMIDITY
            | ClimateEntityFeature.FAN_MODE
            | ClimateEntityFeature.SWING_MODE
            | ClimateEntityFeature.PRESET_MODE
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
        if self._cool_humidity_enabled is not None:
            attrs["cool_humidity_enabled"] = self._cool_humidity_enabled
        if self._cool_humidity_target is not None:
            attrs["cool_humidity_target"] = self._cool_humidity_target
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

    # ---- 內建濕度 API 對應 ----
    @property
    def target_humidity(self) -> int | None:
        # 映射到 e_3003.p_1A（0~100）
        return self._cool_humidity_target

    @property
    def current_humidity(self) -> int | None:
        return self._current_humidity

    @property
    def preset_modes(self) -> list[str] | None:
        # 用 preset 代表「是否啟用冷房濕度控制」
        return ["none", "humidity_control"]

    @property
    def preset_mode(self) -> str | None:
        if self._cool_humidity_enabled:
            return "humidity_control"
        return "none"

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
            # 讀取目標溫度：主欄位 + 容錯候選，讀不到就保留上次值，避免 UI 隱藏滑桿
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

            # 冷房濕度控制（正確欄位：e_3003.p_2C 開關、e_3003.p_1A 目標%）
            try:
                p2c = self.find_value_by_pn(data, "/dsiot/edge/adr_0100.dgc_status",
                                            "dgc_status", "e_1002", "e_3003", "p_2C")
                self._cool_humidity_enabled = (p2c == "01")
            except Exception:
                self._cool_humidity_enabled = None
            try:
                p1a = self.find_value_by_pn(data, "/dsiot/edge/adr_0100.dgc_status",
                                            "dgc_status", "e_1002", "e_3003", "p_1A")
                self._cool_humidity_target = int(p1a, 16) if p1a is not None else None
            except Exception:
                self._cool_humidity_target = None

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
            if code not in (2000, 2004):
                # 簡單檢測：若 payload 內同時含有 p_2D 與其他 p_*，嘗試兩階段提交
                try:
                    reqs = request.get("requests", [])
                    needs_split = False
                    for r in reqs:
                        pc = r.get("pc", {})
                        # 粗略掃描是否同時出現 p_2D 與其他屬性
                        found_commit = False
                        found_others = False
                        stack = [pc]
                        while stack:
                            node = stack.pop()
                            for ch in node.get("pch", []):
                                if ch.get("pn") == "p_2D":
                                    found_commit = True
                                elif ch.get("pn", "").startswith("p_") and ch.get("pn") != "p_2D":
                                    found_others = True
                                stack.append(ch)
                        if found_commit and found_others:
                            needs_split = True
                            break
                    if needs_split:
                        # 1) 先送「去掉 p_2D」的版本
                        def strip_p2d(node):
                            node["pch"] = [ch for ch in node.get("pch", []) if ch.get("pn") != "p_2D"]
                            for ch in node["pch"]:
                                strip_p2d(ch)
                        first = {"requests": []}
                        for r in reqs:
                            r1 = {"op": r["op"], "to": r["to"], "pc": {"pn": r["pc"].get("pn"), "pch": []}}
                            # 深拷貝後移除 p_2D
                            import copy
                            r1["pc"] = copy.deepcopy(r["pc"])
                            strip_p2d(r1["pc"])
                            first["requests"].append(r1)
                        self._http("POST", first)
                        # 2) 再送只含 p_2D 的版本
                        def keep_only_p2d(node):
                            node["pch"] = [ch for ch in node.get("pch", []) if ch.get("pn") == "p_2D"]
                            for ch in node["pch"]:
                                keep_only_p2d(ch)
                        second = {"requests": []}
                        for r in reqs:
                            r2 = {"op": r["op"], "to": r["to"], "pc": {"pn": r["pc"].get("pn"), "pch": []}}
                            import copy
                            r2["pc"] = copy.deepcopy(r["pc"])
                            keep_only_p2d(r2["pc"])
                            second["requests"].append(r2)
                        self._http("POST", second)
                    else:
                        _LOGGER.error("Unexpected response (no split fallback applied): %s", resp)
                        return
                except Exception:
                    _LOGGER.error("Unexpected response: %s", resp)
                    _LOGGER.error("Request: %s", request)
                    return
            self.update()
        except Exception as err:
            self._attr_available = False
            self._fail_count = min(self._fail_count + 1, len(BACKOFFS))
            self._next_retry = time.monotonic() + BACKOFFS[self._fail_count - 1]
            _LOGGER.warning("update_attribute failed: %s", err)
            _LOGGER.warning("Request: %s", request)

    # -------- 冷房濕度控制：實體服務 --------
    async def async_set_cool_humidity_control(self, enabled: bool, target: Optional[int] = None, continuous: Optional[bool] = False):
        # 新版：p_2C（開關），p_1A（0~100 %）
        hex_target = None
        if target is not None:
            target = max(0, min(100, int(target)))
            hex_target = f"{target:02X}"  # 50 -> "32", 55 -> "37", 60 -> "3C"
        attrs = [
            DaikinAttribute("p_2D", "02", ["e_1002","e_3003"], "/dsiot/edge/adr_0100.dgc_status"),
            DaikinAttribute("p_2C", "01" if enabled else "00", ["e_1002","e_3003"], "/dsiot/edge/adr_0100.dgc_status"),
        ]
        if hex_target is not None:
            attrs.append(DaikinAttribute("p_1A", hex_target, ["e_1002","e_3003"], "/dsiot/edge/adr_0100.dgc_status"))
        await self.hass.async_add_executor_job(self.update_attribute, DaikinRequest(attrs).serialize())


    # === 內建服務：設定目標濕度 ===
    def set_humidity(self, humidity: int) -> None:
        # 將 0~100 的整數寫入 e_3003.p_1A；並確保 p_2C=01（啟用濕度控制）
        value = max(0, min(100, int(humidity)))
        hexv = f"{value:02X}"
        req = DaikinRequest([
            DaikinAttribute("p_2D", "02", ["e_1002","e_3003"], "/dsiot/edge/adr_0100.dgc_status"),
            DaikinAttribute("p_2C", "01", ["e_1002","e_3003"], "/dsiot/edge/adr_0100.dgc_status"),
            DaikinAttribute("p_1A", hexv, ["e_1002","e_3003"], "/dsiot/edge/adr_0100.dgc_status"),
        ]).serialize()
        self.update_attribute(req)

    async def async_set_humidity(self, humidity: int) -> None:
        await self.hass.async_add_executor_job(self.set_humidity, humidity)

    # === 內建服務：設定預設模式（用來開/關濕度控制）===
    def set_preset_mode(self, preset_mode: str) -> None:
        on = (str(preset_mode).lower() == "humidity_control")
        req = DaikinRequest([
            DaikinAttribute("p_2D", "02", ["e_1002","e_3003"], "/dsiot/edge/adr_0100.dgc_status"),
            DaikinAttribute("p_2C", "01" if on else "00", ["e_1002","e_3003"], "/dsiot/edge/adr_0100.dgc_status"),
        ]).serialize()
        self.update_attribute(req)

    async def async_set_preset_mode(self, preset_mode: str) -> None:
        await self.hass.async_add_executor_job(self.set_preset_mode, preset_mode)

    # ========== 葉片定位：實體服務 ==========
    async def async_set_vane_position(self, vertical: str | None = None, horizontal: str | None = None) -> None:
        """設定葉片位置：vertical 可為 Auto/Swing/Circulation/1..6/Off；horizontal 可為 Off/Swing/固定角度"""
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
            # 關機：很多機型不接受帶 p_2D，單獨送電源 OFF 即可
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

        # 開機改兩段送：先電源 ON，再設定模式+提交，避免有些韌體對同包多欄位挑剔
        req_on = DaikinRequest([
            DaikinAttribute("p_01", "01", ["e_1002", "e_A002"], "/dsiot/edge/adr_0100.dgc_status"),
        ]).serialize()
        self.update_attribute(req_on)

        req_mode = DaikinRequest([
            DaikinAttribute("p_01", mode_hex, ["e_1002", "e_3001"], "/dsiot/edge/adr_0100.dgc_status"),
            DaikinAttribute("p_2D", "02", ["e_1002", "e_3003"], "/dsiot/edge/adr_0100.dgc_status"),
        ]).serialize()
        self.update_attribute(req_mode)

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
        # ℃×2 → 兩位十六進位字串（左側補 0）；本機型只需單一位元組
        v = max(self._min_temp, min(self._max_temp, float(temp)))
        hexv = f"{int(round(v * 2)) & 0xFF:02X}"
        req = DaikinRequest([
            DaikinAttribute("p_2D", "02", ["e_1002", "e_3003"], "/dsiot/edge/adr_0100.dgc_status"),
            DaikinAttribute(attr, hexv, ["e_1002", "e_3001"], "/dsiot/edge/adr_0100.dgc_status"),
        ]).serialize()
        self.update_attribute(req)
    async def async_set_temperature(self, **kwargs) -> None:
        """供前端/服務呼叫的 async 版本。"""
        # HA 會把 entity_id 等塞進 kwargs；executor_job 不吃 kwargs，改用 partial
        kwargs.pop("entity_id", None)
        await self.hass.async_add_executor_job(partial(self.set_temperature, **kwargs))

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

# ---- 在平台 setup 時註冊本實體的「濕度控制 / 葉片定位」服務 ----
async def async_setup_entry(hass, entry, async_add_entities):
    host = entry.data.get("host") or entry.data.get("ip") or entry.data.get("ip_address")
    title = entry.title or f"Local Daikin ({host})"

    # 1) 先註冊實體服務，避免首次 update 失敗造成服務永遠沒註冊
    platform = entity_platform.async_get_current_platform()
    hass.data.setdefault(DOMAIN, {})
    if not hass.data[DOMAIN].get("services_registered"):
        hass.data[DOMAIN]["services_registered"] = True

        platform.async_register_entity_service(
            "set_cool_humidity_control",
            cv.make_entity_service_schema({
                vol.Required("enabled"): bool,
                vol.Optional("target"): vol.All(vol.Coerce(int), vol.Range(min=0, max=100)),
                vol.Optional("continuous", default=False): bool,
            }),
            "async_set_cool_humidity_control",
        )

        platform.async_register_entity_service(
            "set_vane_position",
            cv.make_entity_service_schema({
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

    # 2) 再加 entity（允許首次 update 失敗，但服務已經存在）
    ent = LocalDaikinClimate(host, name=title)
    async_add_entities([ent], update_before_add=True)
