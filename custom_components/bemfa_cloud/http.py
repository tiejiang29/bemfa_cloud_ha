"""HTTP API client for Bemfa Cloud topic management."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    ADD_TOPICS_URL,
    BEMFA_REGION,
    BEMFA_TOPIC_TYPE_TCP_V2,
    CHANGE_TOPIC_ROOM_URL,
    CONF_UID,
    CREATE_TOPIC_URL,
    LOGGER,
    MODIFY_TOPIC_NAME_URL,
)


class BemfaCloudApiError(Exception):
    """Raised when Bemfa Cloud rejects an API request."""


@dataclass(slots=True)
class TopicPayload:
    """Topic create payload item."""

    topic: str
    name: str
    room: str = ""
    group: str = ""
    unit: str = ""

    def as_api_item(self) -> dict[str, Any]:
        """Return the API item payload."""

        return {
            "type": BEMFA_TOPIC_TYPE_TCP_V2,
            "topic": self.topic,
            "name": self.name,
            "room": self.room,
            "group": self.group,
            "unit": self.unit,
        }


class BemfaCloudHttp:
    """Small async client for Bemfa Cloud topic APIs."""

    def __init__(self, hass: HomeAssistant, credentials: dict[str, str]) -> None:
        """Initialize the HTTP client."""

        self._session = async_get_clientsession(hass)
        self._uid = credentials[CONF_UID]
        self._region = BEMFA_REGION

    async def async_create_topics(self, topics: list[TopicPayload]) -> None:
        """Create one or more TCP V2 topics.

        The Bemfa API supports up to 99 topics per batch.
        """

        if not topics:
            return

        if len(topics) == 1:
            await self._async_create_topic(topics[0])
            return

        for index in range(0, len(topics), 99):
            await self._async_add_topics(topics[index : index + 99])

    async def _async_create_topic(self, topic: TopicPayload) -> None:
        payload = {
            "uid": self._uid,
            **topic.as_api_item(),
            "region": self._region,
        }
        await self._post(CREATE_TOPIC_URL, payload)

    async def _async_add_topics(self, topics: list[TopicPayload]) -> None:
        payload = {
            "uid": self._uid,
            "topics": [topic.as_api_item() for topic in topics],
            "region": self._region,
        }
        await self._post(ADD_TOPICS_URL, payload)

    async def async_modify_name(self, topic: str, name: str) -> None:
        """Update a Bemfa topic display name."""

        await self._post(
            MODIFY_TOPIC_NAME_URL,
            {
                "uid": self._uid,
                "topic": topic,
                "type": BEMFA_TOPIC_TYPE_TCP_V2,
                "name": name,
            },
        )

    async def async_modify_room(self, topics: list[str], room: str) -> None:
        """Update Bemfa topic room for one or more topics."""

        if not topics:
            return

        for index in range(0, len(topics), 50):
            await self._post(
                CHANGE_TOPIC_ROOM_URL,
                {
                    "openID": self._uid,
                    "topicIDs": topics[index : index + 50],
                    "type": BEMFA_TOPIC_TYPE_TCP_V2,
                    "room": room,
                },
            )

    async def _post(self, url: str, payload: dict[str, Any]) -> None:
        async with self._session.post(url, json=payload, timeout=30) as response:
            try:
                data = await response.json(content_type=None)
            except ValueError:
                data = {"raw": await response.text()}

        if response.status >= 400:
            raise BemfaCloudApiError(f"HTTP {response.status}: {data}")

        if data.get("code") not in (0, None):
            raise BemfaCloudApiError(str(data.get("msg") or data))

        business = data.get("data") if isinstance(data.get("data"), dict) else {}
        business_code = business.get("code", 0)
        if business_code in (0, 40006):
            if business_code == 40006:
                LOGGER.debug("Bemfa topic already exists: %s", payload)
            return

        raise BemfaCloudApiError(str(business.get("message") or data))
