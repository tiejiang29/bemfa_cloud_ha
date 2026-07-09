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
    DELETE_TOPIC_URL,
    DELETE_TOPIC_V5_URL,
    EMAIL_LOGIN_URL,
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
        """Return the API item payload.

        Truncates the name to fit Bemfa's v_name column. Testing showed
        32 bytes still triggers 'Data too long' for some names, so we use
        30 bytes to be safe.
        """
        name = self.name
        if name:
            encoded = name.encode("utf-8")
            if len(encoded) > 30:
                # Truncate at 30 bytes, being careful not to split a
                # multi-byte UTF-8 character.
                truncated = encoded[:30]
                # UTF-8 continuation bytes start with 0b10xxxxxx (0x80-0xBF).
                # Walk back until we're not in the middle of a character.
                while truncated and (truncated[-1] & 0xC0) == 0x80:
                    truncated = truncated[:-1]
                name = truncated.decode("utf-8", errors="ignore")
                LOGGER.warning(
                    "Bemfa Cloud: truncated topic name %r -> %r (30 byte limit)",
                    self.name, name,
                )

        return {
            "type": BEMFA_TOPIC_TYPE_TCP_V2,
            "topic": self.topic,
            "name": name,
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

        We create topics ONE AT A TIME instead of using the batch
        addTopicsNoSecret endpoint. Reasons:
          1. The batch endpoint returns 40006 if ANY topic in the batch
             already exists, and does NOT create the other (new) topics
             in the batch — this is a silent failure for the new ones.
          2. If any topic's name is too long (Bemfa's v_name column is
             ~32 bytes), the batch endpoint fails the ENTIRE batch with
             a database error, blocking all other topics.
          3. Single-topic createTopicNoSecret returns clear per-topic
             success/failure, and one failure does not block others.
        """
        if not topics:
            return

        for topic in topics:
            try:
                await self._async_create_topic(topic)
            except BemfaCloudApiError as err:
                LOGGER.warning(
                    "Bemfa Cloud: failed to create topic %s (name=%r): %s. "
                    "Continuing with remaining topics.",
                    topic.topic, topic.name, err,
                )

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

    async def async_delete_topic(self, topic: str) -> None:
        """Delete a single topic from Bemfa Cloud.

        Uses the legacy `pro.bemfa.com/v1/deleteTopic` endpoint because
        Bemfa has not published a NoSecret variant for delete (the NoSecret
        family is create-only).

        The `type` field MUST match the type used at creation. This plugin
        always creates with type=7 (TCP V2), so we delete with type=7 too.

        Idempotent: if the topic is already gone (Bemfa returns business
        code 40004 = "uid or topic error"), we treat that as success and do
        not raise — the desired end state (topic gone) is already achieved.
        """

        await self._post_delete(
            DELETE_TOPIC_URL,
            {
                "uid": self._uid,
                "topic": topic,
                "type": BEMFA_TOPIC_TYPE_TCP_V2,
            },
        )

    async def async_delete_topic_v5(self, topic: str, bearer_token: str) -> None:
        """Delete a topic using the Bemfa Cloud console API (v5).

        The /v1/deleteTopic endpoint has a bug for type=7 topics (returns
        40000 "Unknown error"). The Bemfa web console uses a different
        endpoint — /v5/manage/v1/deleteTopic — which requires a Bearer
        token (JWT) instead of uid. This endpoint works for type=7.

        The Bearer token can be obtained from the Bemfa web console:
        1. Login at https://cloud.bemfa.com/
        2. Open browser DevTools → Application → Cookies → token
        3. Copy the token value

        Token expires after ~30 days. The user must re-enter it when it
        expires.

        Request format (confirmed via browser DevTools):
          POST https://go.bemfa.com/v5/manage/v1/deleteTopic
          Authorization: Bearer <token>
          Content-Type: application/json
          Body: {"topic":"<topic>","protoType":7}
        """

        headers = {
            "Authorization": f"Bearer {bearer_token}",
            "Content-Type": "application/json",
        }
        payload = {
            "topic": topic,
            "protoType": BEMFA_TOPIC_TYPE_TCP_V2,
        }

        async with self._session.post(
            DELETE_TOPIC_V5_URL, json=payload, headers=headers, timeout=30
        ) as response:
            try:
                data = await response.json(content_type=None)
            except ValueError:
                text = await response.text()
                raise BemfaCloudApiError(
                    f"Non-JSON response (HTTP {response.status}): {text[:200]}"
                )

        LOGGER.debug(
            "Bemfa v5 delete: topic=%s response_status=%s response_body=%s",
            topic, response.status, data,
        )

        if response.status >= 400:
            raise BemfaCloudApiError(f"HTTP {response.status}: {data}")

        code = data.get("code") if isinstance(data, dict) else None
        if code == 0 or code is None:
            LOGGER.debug("Bemfa v5 delete: topic %s deleted successfully", topic)
            return

        raise BemfaCloudApiError(
            f"Bemfa v5 delete error (code={code}): "
            f"{data.get('message') or data.get('msg') or data}"
        )

    async def async_login(self, email: str, password: str) -> str:
        """Login with email+password and return a Bearer JWT token.

        Uses the same endpoint as the Bemfa web console:
          POST https://go.bemfa.com/vb/web/v2/emailLogin

        The password is obfuscated the same way the web console does it:
          password field = 1 random char + base64(password) + 4 random chars
        This is obfuscation, not encryption — the server strips the random
        prefix/suffix and base64-decodes.

        Returns the JWT token string (valid ~30 days).
        """

        import base64
        import random
        import string

        # Charset matching the web console's random string generator
        # (no ambiguous chars: no I, L, O, 0, 1)
        rand_charset = "ABCDEFGHJKMNPQRSTWXYZabcdefhijkmnprstwxyz2345678"

        email_b64 = base64.b64encode(email.lower().encode("utf-8")).decode("utf-8")
        pw_b64 = base64.b64encode(password.encode("utf-8")).decode("utf-8")
        # 1 random char prefix + base64(password) + 4 random chars suffix
        pw_obfuscated = (
            "".join(random.choices(rand_charset, k=1))
            + pw_b64
            + "".join(random.choices(rand_charset, k=4))
        )

        payload = {"email": email_b64, "password": pw_obfuscated}

        async with self._session.post(
            EMAIL_LOGIN_URL, json=payload, timeout=30
        ) as response:
            try:
                data = await response.json(content_type=None)
            except ValueError:
                text = await response.text()
                raise BemfaCloudApiError(
                    f"Login non-JSON response (HTTP {response.status}): {text[:200]}"
                )

        if response.status >= 400:
            raise BemfaCloudApiError(f"Login HTTP {response.status}: {data}")

        # Response shape: {"data": {"code": 0, "token": "<JWT>", ...}}
        inner = data.get("data") if isinstance(data, dict) else {}
        if not isinstance(inner, dict):
            raise BemfaCloudApiError(f"Login unexpected response: {data}")

        code = inner.get("code")
        if code != 0:
            raise BemfaCloudApiError(
                f"Login failed (code={code}): {inner.get('message') or inner.get('msg') or data}"
            )

        token = inner.get("token")
        if not token:
            raise BemfaCloudApiError(f"Login succeeded but no token in response: {data}")

        LOGGER.debug("Bemfa login: successfully obtained token for %s", email)
        return token

    async def _post_delete(self, url: str, payload: dict[str, Any]) -> None:
        """POST helper specialized for the delete endpoint.

        The delete endpoint returns a flat response shape
        `{"code": 0, "message": "OK", "data": 0}` — `data` is an integer
        (or null), NOT a nested business object like the NoSecret create
        endpoints. So we cannot reuse `_post()` which expects a nested
        `data.code`.

        Business codes:
          0     = success
          40004 = uid/topic error (= topic doesn't exist or not owned by
                  this uid) — treat as idempotent success
          10002 = bad request parameters — real error
          40000 = unknown error — real error
        """

        async with self._session.post(url, json=payload, timeout=30) as response:
            try:
                data = await response.json(content_type=None)
            except ValueError:
                # Response was not JSON — treat as a transport error.
                text = await response.text()
                raise BemfaCloudApiError(
                    f"Non-JSON response (HTTP {response.status}): {text[:200]}"
                )

        if response.status >= 400:
            raise BemfaCloudApiError(f"HTTP {response.status}: {data}")

        # `data` is now guaranteed to be a parsed JSON dict.
        # `None` here means the key was explicitly null, which we treat as
        # success (the API sometimes returns null code on success).
        code = data.get("code") if isinstance(data, dict) else None
        if code == 0 or code is None:
            return
        if code == 40004:
            # Topic doesn't exist or not owned by this uid — the desired
            # end state (topic gone) is already true, so this is a success.
            LOGGER.warning(
                "Bemfa topic %s not found on cloud (code 40004) — treat as deleted",
                payload.get("topic"),
            )
            return
        raise BemfaCloudApiError(str(data.get("message") if isinstance(data, dict) else data))

    async def _post(self, url: str, payload: dict[str, Any]) -> None:
        async with self._session.post(url, json=payload, timeout=30) as response:
            try:
                data = await response.json(content_type=None)
            except ValueError:
                data = {"raw": await response.text()}

        # Log the raw response at debug level so we can diagnose silent
        # failures where Bemfa returns an unexpected shape.
        LOGGER.warning(
            "Bemfa API %s payload=%s response_status=%s response_body=%s",
            url, payload, response.status, data,
        )

        if response.status >= 400:
            raise BemfaCloudApiError(f"HTTP {response.status}: {data}")

        # Bemfa uses `code` at the top level for the transport status.
        # 0 = OK, None = some endpoints omit it, anything else = error.
        code = data.get("code")
        if code not in (0, None):
            raise BemfaCloudApiError(
                f"Bemfa API error (code={code}): {data.get('msg') or data.get('message') or data}"
            )

        # Some endpoints (createTopicNoSecret / addTopicsNoSecret) wrap a
        # business-level code inside `data`. Others return `data` as a
        # string, integer, or null — in which case there is no business
        # code to check and we treat the call as successful.
        raw_data = data.get("data")
        if isinstance(raw_data, dict):
            business_code = raw_data.get("code", 0)
            if business_code == 40006:
                LOGGER.debug("Bemfa topic already exists: %s", payload)
                return
            if business_code != 0:
                raise BemfaCloudApiError(
                    f"Bemfa business error (code={business_code}): "
                    f"{raw_data.get('message') or data}"
                )
        # `data` is not a dict — no business code to check, transport was OK.
