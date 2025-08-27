from __future__ import annotations

import logging
from typing import Any, Dict

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

DOMAIN = "local_daikin"

# 啟用哪些平台；你的 repo 目前就有這四個
PLATFORMS: list[Platform] = [Platform.CLIMATE, Platform.SWITCH, Platform.SENSOR, Platform.SELECT]


async def async_setup(hass: HomeAssistant, config: Dict[str, Any]) -> bool:
    """YAML 不支援；一律走 ConfigEntry。"""
    return True


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """資料結構遷移（保險起見把舊 key 改成 host）。"""
    data = dict(entry.data)
    version = entry.version

    changed = False
    if "ip" in data and "host" not in data:
        data["host"] = data.pop("ip")
        changed = True

    if changed:
        hass.config_entries.async_update_entry(entry, data=data)
        _LOGGER.info("Migrated %s entry %s to new data schema", DOMAIN, entry.entry_id)

    # 你可以視需要 bump 版本：entry.version = max(version, 1)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """設定整合（每個裝置一個 entry）。"""
    host = entry.data.get("host") or entry.options.get("host")
    if not host:
        _LOGGER.error("[%s] Missing 'host' in config entry data (entry_id=%s)", DOMAIN, entry.entry_id)
        return False

    # 建立 domain data
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN].setdefault("by_host", {})

    hass.data[DOMAIN][entry.entry_id] = {
        "host": host,
        "entry": entry,
    }
    hass.data[DOMAIN]["by_host"][host] = entry.entry_id

    # 讓各平台自行從 entry.data['host'] 取用；或從 hass.data[DOMAIN][entry_id] 取
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # options 變更時自動 reload
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    _LOGGER.debug("[%s] Setup entry %s host=%s", DOMAIN, entry.entry_id, host)
    return True


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """當 options 改變時，重新載入 entry。"""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """卸載整合。"""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        data = hass.data.get(DOMAIN, {})
        info = data.pop(entry.entry_id, None)
        if info:
            host = info.get("host")
            if host and "by_host" in data:
                data["by_host"].pop(host, None)
        _LOGGER.debug("[%s] Unloaded entry %s", DOMAIN, entry.entry_id)
    return unloaded
