from __future__ import annotations

import logging
from typing import Any, Dict

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

DOMAIN = "local_daikin"
PLATFORMS: list[Platform] = [Platform.CLIMATE, Platform.SWITCH, Platform.SENSOR, Platform.SELECT]
_LOGGER = logging.getLogger(__name__)


async def async_setup(hass: HomeAssistant, config: Dict[str, Any]) -> bool:
    return True


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Migrate older keys to 'host'."""
    data = dict(entry.data)
    changed = False

    if "ip" in data and "host" not in data:
        data["host"] = data.pop("ip")
        changed = True
    if "ip_address" in data and "host" not in data:
        data["host"] = data.pop("ip_address")
        changed = True

    if changed:
        hass.config_entries.async_update_entry(entry, data=data)
        _LOGGER.info("[%s] Migrated entry %s data schema -> has 'host'", DOMAIN, entry.entry_id)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up one Local Daikin device (one entry per device)."""
    host = (
        entry.data.get("host")
        or entry.data.get("ip")
        or entry.data.get("ip_address")
        or entry.options.get("host")
        or entry.options.get("ip")
        or entry.options.get("ip_address")
    )
    if not host:
        _LOGGER.error("[%s] Missing host/ip in config entry (entry_id=%s)", DOMAIN, entry.entry_id)
        return False

    # Backfill 'host' into entry.data for future boots
    if "host" not in entry.data:
        new_data = dict(entry.data)
        new_data["host"] = host
        hass.config_entries.async_update_entry(entry, data=new_data)
        _LOGGER.info("[%s] Backfilled 'host' for entry %s", DOMAIN, entry.entry_id)

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN].setdefault("by_host", {})

    hass.data[DOMAIN][entry.entry_id] = {"host": host, "entry": entry}
    hass.data[DOMAIN]["by_host"][host] = entry.entry_id

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    _LOGGER.debug("[%s] Setup entry %s host=%s", DOMAIN, entry.entry_id, host)
    return True


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        data = hass.data.get(DOMAIN, {})
        info = data.pop(entry.entry_id, None)
        if info:
            host = info.get("host")
            data.get("by_host", {}).pop(host, None)
        _LOGGER.debug("[%s] Unloaded entry %s", DOMAIN, entry.entry_id)
    return unloaded
