"""Support for syncing Home Assistant water heaters to Bemfa Cloud."""
from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from homeassistant.components.water_heater import (
    ATTR_CURRENT_TEMPERATURE,
    ATTR_OPERATION_MODE,
    DOMAIN,
)
from homeassistant.const import ATTR_TEMPERATURE, STATE_OFF
from homeassistant.util.read_only_dict import ReadOnlyDict

from .const import MSG_OFF, MSG_ON, TopicSuffix
from .sync import SYNC_TYPES, ControllableSync
from .utils import has_key


@SYNC_TYPES.register("water_heater")
class WaterHeater(ControllableSync):
    """Sync a Home Assistant water heater to Bemfa water heater device."""

    @staticmethod
    def get_config_step_id() -> str:
        return "sync_config_water_heater"

    @staticmethod
    def _get_topic_suffix() -> TopicSuffix:
        return TopicSuffix.WATER_HEATER

    @staticmethod
    def _supported_domain() -> str:
        return DOMAIN

    def _msg_generators(
        self,
    ) -> list[Callable[[str, ReadOnlyDict[Mapping[str, Any]]], str | int]]:
        return [
            lambda state, attributes: MSG_OFF if state == STATE_OFF else MSG_ON,
            lambda state, attributes: round(
                attributes.get(ATTR_TEMPERATURE, attributes.get(ATTR_CURRENT_TEMPERATURE))
            )
            if has_key(attributes, ATTR_TEMPERATURE)
            or has_key(attributes, ATTR_CURRENT_TEMPERATURE)
            else "",
            lambda state, attributes: attributes[ATTR_OPERATION_MODE]
            if has_key(attributes, ATTR_OPERATION_MODE)
            else "",
        ]

    def _msg_resolvers(self):
        return []
