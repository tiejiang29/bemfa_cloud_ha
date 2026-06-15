"""The Bemfa Cloud integration."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN, OPTIONS_CONFIG
from .service import BemfaCloudService

from . import (  # noqa: F401
    sync_binary_sensor,
    sync_climate,
    sync_cover,
    sync_fan,
    sync_light,
    sync_sensor,
    sync_switch,
    sync_water_heater,
)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Bemfa Cloud from a config entry."""

    hass.data.setdefault(DOMAIN, {})
    service = BemfaCloudService(hass, dict(entry.data))
    await service.async_start(entry.options.get(OPTIONS_CONFIG, {}))
    hass.data[DOMAIN][entry.entry_id] = {"service": service}
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""

    data = hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    if data is not None:
        await data["service"].async_stop()
    return True
