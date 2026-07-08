"""TCP JSON long connection for Bemfa Cloud."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from homeassistant.const import EVENT_STATE_CHANGED
from homeassistant.core import CALLBACK_TYPE, HomeAssistant

from .const import (
    CONF_UID,
    LOGGER,
    TCP_CONNECT_TIMEOUT,
    TCP_HOST,
    TCP_PING_INTERVAL,
    TCP_PORT,
    TCP_RECONNECT_DELAY,
    TCP_RESUBSCRIBE_INTERVAL,
)
from .sync import Sync


class BemfaCloudTcp:
    """Maintain Bemfa TCP JSON subscriptions and state publishing."""

    def __init__(self, hass: HomeAssistant, uid: str) -> None:
        """Initialize the TCP client."""

        self._hass = hass
        self._uid = uid
        self._topic_to_sync: dict[str, Sync] = {}
        self._writer: asyncio.StreamWriter | None = None
        self._runner: asyncio.Task[None] | None = None
        self._remove_listener: CALLBACK_TYPE | None = None
        self._feedback_suppressed_until: dict[str, float] = {}
        self._stopped = asyncio.Event()

    async def async_start(self) -> None:
        """Start connection management."""

        if self._runner is not None:
            return

        self._stopped.clear()
        self._remove_listener = self._hass.bus.async_listen(
            EVENT_STATE_CHANGED, self._state_listener
        )
        self._runner = self._hass.async_create_background_task(
            self._run(), "bemfa_cloud_tcp"
        )

    async def async_stop(self) -> None:
        """Stop the connection."""

        self._stopped.set()
        if self._remove_listener is not None:
            self._remove_listener()
            self._remove_listener = None
        if self._runner is not None:
            self._runner.cancel()
            try:
                await self._runner
            except asyncio.CancelledError:
                pass
            self._runner = None
        await self._close_writer()

    async def async_add_sync(self, sync: Sync) -> None:
        """Add a sync and publish its current state."""

        await self.async_add_syncs([sync])

    async def async_add_syncs(self, syncs: list[Sync]) -> None:
        """Add syncs, subscribe their topics in one TCP command, and publish state."""

        topics: list[str] = []
        for sync in syncs:
            self._topic_to_sync[sync.topic] = sync
            topics.append(sync.topic)

        LOGGER.warning(
            "Bemfa TCP: subscribing to %d topics via async_add_syncs: %s. "
            "Current writer is %s",
            len(topics), topics,
            "connected" if (self._writer and not self._writer.is_closing()) else "NOT connected",
        )

        sub_ok = await self._subscribe(topics)
        LOGGER.warning("Bemfa TCP: subscribe result=%s", sub_ok)

        for sync in syncs:
            try:
                await self.async_publish_sync(sync)
                LOGGER.warning(
                    "Bemfa TCP: published state for topic=%s msg=%s",
                    sync.topic, sync.generate_msg(),
                )
            except Exception as err:  # noqa: BLE001
                LOGGER.warning(
                    "Bemfa TCP: publish failed for topic=%s: %s (type=%s)",
                    sync.topic, err, type(err).__name__,
                )

    async def async_subscribe_all(self) -> bool:
        """Subscribe all known topics on the current TCP connection."""

        return await self._subscribe(list(self._topic_to_sync))

    async def async_update_sync(self, sync: Sync) -> None:
        """Update a sync."""

        self._topic_to_sync[sync.topic] = sync
        await self.async_publish_sync(sync)

    async def async_remove_sync(self, topic: str) -> None:
        """Remove a sync locally."""

        self._topic_to_sync.pop(topic, None)
        await self._close_writer()

    async def async_publish_sync(self, sync: Sync) -> None:
        """Publish the current Home Assistant state for a sync."""

        msg = sync.generate_msg()
        if not msg:
            return
        await self._send(
            {
                "cmd": 2,
                "uid": self._uid,
                "topics": [sync.topic],
                "msg": msg,
                "mode": 3,
            }
        )

    async def _run(self) -> None:
        while not self._stopped.is_set():
            try:
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection(TCP_HOST, TCP_PORT),
                    timeout=TCP_CONNECT_TIMEOUT,
                )
                self._writer = writer
                LOGGER.warning(
                    "Bemfa TCP connected to %s:%s, subscribing to %d topics",
                    TCP_HOST, TCP_PORT, len(self._topic_to_sync),
                )
                await self._subscribe(list(self._topic_to_sync))
                await self._publish_all()
                LOGGER.warning("Bemfa TCP ready, entering read loop")
                await self._read_loop(reader)
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001
                LOGGER.warning(
                    "Bemfa TCP connection failed: %s (type=%s, repr=%r)",
                    err, type(err).__name__, err,
                )
            finally:
                await self._close_writer()

            try:
                await asyncio.wait_for(self._stopped.wait(), timeout=TCP_RECONNECT_DELAY)
            except TimeoutError:
                continue

    async def _read_loop(self, reader: asyncio.StreamReader) -> None:
        next_ping = self._hass.loop.time() + TCP_PING_INTERVAL
        next_resubscribe = self._hass.loop.time() + TCP_RESUBSCRIBE_INTERVAL
        while not self._stopped.is_set():
            now = self._hass.loop.time()
            if now >= next_resubscribe:
                if not await self.async_subscribe_all():
                    raise ConnectionError("Bemfa TCP resubscribe failed")
                next_resubscribe = self._hass.loop.time() + TCP_RESUBSCRIBE_INTERVAL

            timeout = max(1, min(next_ping, next_resubscribe) - now)
            try:
                raw = await asyncio.wait_for(reader.readline(), timeout=timeout)
            except TimeoutError:
                if self._hass.loop.time() >= next_resubscribe:
                    if not await self.async_subscribe_all():
                        raise ConnectionError("Bemfa TCP resubscribe failed")
                    next_resubscribe = self._hass.loop.time() + TCP_RESUBSCRIBE_INTERVAL
                    continue
                # Heartbeat — matches official bemfa_cloud_ha implementation
                if not await self._send({"cmd": 7, "uid": self._uid}):
                    raise ConnectionError("Bemfa TCP heartbeat failed")
                next_ping = self._hass.loop.time() + TCP_PING_INTERVAL
                continue

            if not raw:
                raise ConnectionError("Bemfa TCP connection closed")

            # Log EVERY raw line received from Bemfa, regardless of whether
            # it parses as JSON. This is critical for debugging reverse
            # control (Bemfa -> HA) issues.
            LOGGER.warning("Bemfa TCP raw received: %r", raw[:500])

            try:
                payload = json.loads(raw.decode("utf-8"))
            except ValueError:
                LOGGER.warning(
                    "Bemfa TCP: non-JSON payload received, ignoring: %r", raw[:200]
                )
                continue

            LOGGER.warning("Bemfa TCP parsed payload: %s", payload)
            self._handle_payload(payload)

    def _handle_payload(self, payload: dict[str, Any]) -> None:
        # Bemfa may send either "topics" (array) or "topic" (singular string).
        # Check both for maximum compatibility.
        topic = None
        topics = payload.get("topics")
        if topics and isinstance(topics, list) and len(topics) > 0:
            topic = topics[0]
        elif payload.get("topic"):
            topic = payload["topic"]

        if not topic:
            LOGGER.warning(
                "Bemfa TCP _handle_payload: no topic found in payload. Keys=%s",
                list(payload.keys()),
            )
            return

        sync = self._topic_to_sync.get(topic)
        if sync is None:
            LOGGER.warning(
                "Bemfa TCP _handle_payload: topic %s not in subscribed syncs %s",
                topic, list(self._topic_to_sync.keys()),
            )
            return
        if "msg" not in payload:
            LOGGER.warning(
                "Bemfa TCP _handle_payload: no 'msg' field in payload for topic %s",
                topic,
            )
            return
        msg = payload["msg"]
        LOGGER.warning("Bemfa TCP received topic=%s msg=%s", topic, self._msg_to_text(msg))
        self._suppress_state_feedback(sync, seconds=2)
        sync.resolve_msg(msg)
        self._hass.async_create_task(
            self._async_publish_control_feedback(sync, msg),
            "bemfa_cloud_control_feedback",
        )

    def _suppress_state_feedback(self, sync: Sync, seconds: int) -> None:
        until = self._hass.loop.time() + seconds
        for entity_id in sync.get_watched_entity_ids():
            self._feedback_suppressed_until[entity_id] = until

    async def _async_publish_control_feedback(self, sync: Sync, msg: Any) -> None:
        """Publish a complete state payload shortly after a control command."""

        await asyncio.sleep(1)
        feedback = sync.generate_feedback_msg(msg)
        if not feedback:
            return
        await self._send(
            {
                "cmd": 2,
                "uid": self._uid,
                "topics": [sync.topic],
                "msg": feedback,
                "mode": 3,
            }
        )

    async def _state_listener(self, event) -> None:
        new_state = event.data.get("new_state")
        if new_state is None:
            return
        entity_id = new_state.entity_id
        suppress_until = self._feedback_suppressed_until.get(entity_id, 0)
        if suppress_until > self._hass.loop.time():
            return
        self._feedback_suppressed_until.pop(entity_id, None)
        for sync in list(self._topic_to_sync.values()):
            if entity_id in sync.get_watched_entity_ids():
                await self.async_publish_sync(sync)

    async def _subscribe(self, topics: list[str]) -> bool:
        if topics:
            # Match official bemfa_cloud_ha: use "topics" array
            payload = {"cmd": 1, "uid": self._uid, "topics": topics, "mode": 0}
            LOGGER.warning(
                "Bemfa TCP subscribe: sending cmd=1 for %d topics: %s",
                len(topics), topics,
            )
            if not await self._send(payload):
                LOGGER.warning("Bemfa TCP subscribe: _send returned False, closing writer")
                await self._close_writer()
                return False
            LOGGER.warning("Bemfa TCP subscribe: _send returned True")
        return True

    async def _publish_all(self) -> None:
        for sync in list(self._topic_to_sync.values()):
            await self.async_publish_sync(sync)

    async def _send(self, payload: dict[str, Any]) -> bool:
        writer = self._writer
        if writer is None or writer.is_closing():
            LOGGER.warning(
                "Bemfa TCP _send: writer is %s, cannot send payload cmd=%s",
                "None" if writer is None else "closing",
                payload.get("cmd"),
            )
            return False
        try:
            data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
            writer.write(data)
            await writer.drain()
            LOGGER.warning(
                "Bemfa TCP _send: sent %d bytes for cmd=%s topics=%s",
                len(data), payload.get("cmd"), payload.get("topics"),
            )
        except Exception as err:  # noqa: BLE001
            LOGGER.warning(
                "Bemfa TCP _send failed: %s (type=%s, repr=%r) for payload cmd=%s",
                err, type(err).__name__, err, payload.get("cmd"),
            )
            await self._close_writer()
            return False
        return True

    async def _close_writer(self) -> None:
        writer = self._writer
        self._writer = None
        if writer is None:
            return
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:  # noqa: BLE001
            pass

    @staticmethod
    def _msg_to_text(msg: Any) -> str:
        if isinstance(msg, str):
            return msg
        return json.dumps(msg, ensure_ascii=False, separators=(",", ":"))
