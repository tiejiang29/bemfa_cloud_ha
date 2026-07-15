"""Bemfa Cloud sync service."""

from __future__ import annotations

import asyncio
import base64
import json
import time

from homeassistant.const import EVENT_HOMEASSISTANT_STARTED, SERVICE_TURN_OFF, SERVICE_TURN_ON
from homeassistant.core import CALLBACK_TYPE, CoreState, Event, HomeAssistant
from homeassistant.helpers import area_registry, device_registry, entity_registry

from .const import CONF_BEARER_TOKEN, CONF_EMAIL, CONF_PASSWORD, DOMAIN, EXCLUDED_SOURCE_PLATFORMS, LOGGER, OPTIONS_NAME, WECHAT_LOGIN_POLL_URL, WECHAT_QR_IMAGE_URL, WECHAT_QR_URL
from .http import BemfaCloudApiError, BemfaCloudHttp, TopicPayload
from .sync import SYNC_TYPES, Sync
from .tcp import BemfaCloudTcp


class BemfaCloudService:
    """Manage topic creation and TCP synchronization."""

    def __init__(self, hass: HomeAssistant, credentials: dict[str, str]) -> None:
        """Initialize the service."""

        self._hass = hass
        self._http = BemfaCloudHttp(hass, credentials)
        self._tcp = BemfaCloudTcp(hass, credentials["uid"])
        self._bearer_token: str | None = credentials.get(CONF_BEARER_TOKEN)
        self._email: str | None = credentials.get(CONF_EMAIL)
        self._password: str | None = credentials.get(CONF_PASSWORD)
        self._config: dict[str, dict[str, str]] = {}
        self._syncs_by_entity_id: dict[str, Sync] = {}
        self._unsub_registry_listeners: list[CALLBACK_TYPE] = []
        self._restore_in_progress: bool = False
        self._wechat_sid_history: bool = bool(credentials.get(CONF_BEARER_TOKEN))

    async def async_start(self, config: dict[str, dict[str, str]]) -> None:
        """Start service and restore configured syncs."""

        self._config = config
        await self._tcp.async_start()

        # Token management is on-demand only:
        # - email+password: login when a delete is needed, no background task
        # - WeChat scan: show QR notification when a delete is needed
        # - No background polling, no wasted API calls

        async def _start(event: Event | None = None) -> None:
            await self._async_restore_syncs()

        if self._hass.state == CoreState.running:
            self._hass.async_create_background_task(
                self._async_restore_syncs(), "bemfa_cloud_restore_syncs"
            )
        else:
            self._hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, _start)

        self._start_registry_listeners()

    async def _async_ensure_valid_token(self) -> str | None:
        """Ensure we have a valid Bearer token for cloud deletion.

        Called on-demand when a delete is needed (not proactively).
        Returns the token, or None if no token can be obtained.

        Logic:
        1. If we have a token and it's not expired → return it
        2. If email+password configured → login to get fresh token
        3. If only expired token (from WeChat) → show QR notification,
           wait for scan, return new token
        4. No token and no credentials → return None
        """

        # Check if current token is still valid
        if self._bearer_token and not self._should_refresh_token():
            return self._bearer_token

        # Token is missing or expired — try to refresh
        if self._email and self._password:
            try:
                await self._async_refresh_token()
                return self._bearer_token
            except Exception as err:  # noqa: BLE001
                LOGGER.warning(
                    "Bemfa Cloud: email login failed during delete: %s", err
                )
                return None

        # WeChat scan mode — show QR and wait for scan
        if self._bearer_token is not None or self._wechat_sid_history:
            # Had a token before (from WeChat scan), show QR for renewal
            new_token = await self._async_wechat_renew_via_qr()
            return new_token

        return None

    async def _async_wechat_renew_via_qr(self) -> str | None:
        """Show a WeChat QR notification and poll for scan to get a token.

        Called on-demand when a delete is needed and the token is expired.
        Returns the new token, or None if scan fails/times out.
        """

        from homeassistant.components import persistent_notification
        from homeassistant.helpers.aiohttp_client import async_get_clientsession
        import time

        notification_id = f"{DOMAIN}_token_renew_{id(self)}"

        # Step 1: Fetch QR code
        session = async_get_clientsession(self._hass)
        try:
            async with session.get(WECHAT_QR_URL, timeout=30) as response:
                qr_data = await response.json(content_type=None)

            if response.status >= 400 or qr_data.get("code") not in (0, None):
                LOGGER.debug("Bemfa Cloud: failed to fetch WeChat QR: %s", qr_data)
                return None

            payload = qr_data.get("data") if isinstance(qr_data.get("data"), dict) else {}
            ticket = str(payload.get("url") or "")
            sid = str(payload.get("sid") or "")
            if not ticket or not sid:
                LOGGER.debug("Bemfa Cloud: WeChat QR response missing ticket/sid: %s", qr_data)
                return None

            qr_image_url = WECHAT_QR_IMAGE_URL.format(ticket=ticket)
        except Exception as err:  # noqa: BLE001
            LOGGER.debug("Bemfa Cloud: failed to prepare WeChat QR: %s", err)
            return None

        # Step 2: Show persistent notification with QR code
        persistent_notification.async_create(
            self._hass,
            f"需要删除巴法云云端主题，但 Bearer Token 已过期。\n\n"
            f"**请用微信扫描下方二维码续期：**\n\n"
            f"![微信扫码续期]({qr_image_url})\n\n"
            f"扫码成功后此通知会自动消失，删除操作会继续执行。\n\n"
            f"或者，在集成配置里填入邮箱+密码，以后自动刷新无需扫码。",
            title="Bemfa Cloud — 请扫码续期 Token 以删除云端主题",
            notification_id=notification_id,
        )
        LOGGER.debug("Bemfa Cloud: showing WeChat QR notification for token renewal (needed for delete)")

        # Step 3: Poll for scan (every 3s, up to 5 minutes)
        deadline = time.time() + 300  # 5 minutes
        poll_interval = 3

        while time.time() < deadline:
            try:
                async with session.post(
                    WECHAT_LOGIN_POLL_URL,
                    json={"eventKey": sid},
                    timeout=30,
                ) as response:
                    data = await response.json(content_type=None)

                if response.status >= 400:
                    await asyncio.sleep(poll_interval)
                    continue

                if data.get("code") != 0:
                    await asyncio.sleep(poll_interval)
                    continue

                inner = data.get("data") if isinstance(data.get("data"), dict) else None
                if not inner or inner.get("code") != 0:
                    await asyncio.sleep(poll_interval)
                    continue

                # Scanned! Extract token
                new_token = inner.get("token")
                persistent_notification.async_dismiss(self._hass, notification_id)

                if new_token and isinstance(new_token, str) and new_token.startswith("eyJ"):
                    self._bearer_token = new_token
                    self._wechat_sid_history = True  # remember we've had a WeChat token
                    LOGGER.debug("Bemfa Cloud: Bearer token renewed via WeChat scan")
                    return new_token
                else:
                    LOGGER.debug("Bemfa Cloud: WeChat scan succeeded but no token: %s", inner)
                    return None

            except asyncio.CancelledError:
                raise
            except Exception:
                pass

            await asyncio.sleep(poll_interval)

        # Step 4: Timeout — update notification
        persistent_notification.async_create(
            self._hass,
            f"5 分钟内未检测到扫码，删除操作已取消。\n\n"
            f"请选择以下方式之一：\n"
            f"1. 重新扫码：再次执行删除操作会重新弹出二维码\n"
            f"2. 配置邮箱+密码：删除并重新添加集成时填入邮箱密码，以后自动刷新\n"
            f"3. 手动删除：去 https://cloud.bemfa.com/ 控制台手动删",
            title="Bemfa Cloud — Token 续期超时，删除已取消",
            notification_id=notification_id,
        )
        LOGGER.debug("Bemfa Cloud: WeChat QR renewal timed out after 5 minutes")
        return None

    def _should_refresh_token(self) -> bool:
        """Check if token needs refresh (missing or <2 days to expiry)."""

        if not self._bearer_token:
            return True

        try:
            # JWT structure: header.payload.signature
            parts = self._bearer_token.split(".")
            if len(parts) != 3:
                return True
            # Decode payload (add padding for base64)
            payload_b64 = parts[1] + "=" * (4 - len(parts[1]) % 4)
            payload = json.loads(base64.b64decode(payload_b64).decode("utf-8"))
            exp = payload.get("exp")
            if not exp:
                return True
            # Refresh if <2 days to expiry
            remaining = exp - time.time()
            if remaining < 172800:  # 2 days
                LOGGER.debug(
                    "Bemfa Cloud: token expires in %.0f hours, refreshing",
                    remaining / 3600,
                )
                return True
            return False
        except Exception:  # noqa: BLE001
            return True

    async def _async_refresh_token(self) -> None:
        """Login with email+password and update the stored token."""

        if not self._email or not self._password:
            return

        LOGGER.debug("Bemfa Cloud: refreshing Bearer token via email login")
        token = await self._http.async_login(self._email, self._password)
        self._bearer_token = token
        LOGGER.debug("Bemfa Cloud: Bearer token refreshed successfully")

    async def async_stop(self) -> None:
        """Stop service."""

        for unsub in self._unsub_registry_listeners:
            unsub()
        self._unsub_registry_listeners.clear()
        await self._tcp.async_stop()

    async def _async_restore_syncs(self) -> None:
        # Debounce: if a restore is already in progress FOR THIS HASS
        # INSTANCE (not just this service instance — HA creates a new
        # BemfaCloudService on every reload, so an instance-level guard
        # is useless). We use hass.data as a shared namespace across
        # service instances.
        #
        # Without this, HA's add_update_listener fires multiple reload
        # events on options change, each creating a new service that
        # races to call the Bemfa create-topics API. The result is
        # dozens of duplicate API calls in 1-2 seconds, which can:
        #   1. Trigger Bemfa's rate limit
        #   2. Overwhelm the TCP connection (hundreds of concurrent
        #      connect attempts -> "TCP connection failed: " with empty
        #      error message)
        #   3. Cause aiohttp "Session is closed" errors that propagate
        #      up as empty-message exceptions caught by _ensure_topics.
        DOMAIN_DATA = self._hass.data.setdefault(DOMAIN, {})
        service_data = DOMAIN_DATA.setdefault("_restore_state", {})
        if service_data.get("in_progress", False):
            LOGGER.warning(
                "Bemfa Cloud restore: another restore is already in progress "
                "(probably from a recent reload), skipping this one to avoid "
                "duplicate API calls and TCP connection storms."
            )
            return

        service_data["in_progress"] = True
        try:
            await self._async_restore_syncs_inner()
        finally:
            service_data["in_progress"] = False

    async def _async_restore_syncs_inner(self) -> None:
        LOGGER.warning(
            "Bemfa Cloud restore: starting. Configured topics=%d, hass state=%s",
            len(self._config),
            self._hass.state,
        )
        syncs = []
        all_collected = self.collect_supported_syncs()
        LOGGER.warning(
            "Bemfa Cloud restore: collected %d candidate syncs from HA",
            len(all_collected),
        )
        for sync in all_collected:
            # `default_topic` is the stable key under which config is stored.
            # It does NOT change when the user overrides the device type.
            if sync.default_topic not in self._config:
                LOGGER.debug(
                    "Bemfa Cloud restore: skip %s (default_topic=%s not in config)",
                    sync.entity_id, sync.default_topic,
                )
                continue
            sync.config = self._config.get(sync.default_topic, {OPTIONS_NAME: sync.name}).copy()
            sync.name = sync.config.get(OPTIONS_NAME, sync.name)
            LOGGER.warning(
                "Bemfa Cloud restore: will create topic=%s name=%s for entity=%s",
                sync.topic, sync.name, sync.entity_id,
            )
            syncs.append(sync)

        if not syncs:
            LOGGER.warning(
                "Bemfa Cloud restore: 0 syncs matched. Config keys=%s, "
                "collected default_topics=%s",
                list(self._config.keys()),
                [s.default_topic for s in all_collected],
            )
            return

        try:
            await self._ensure_topics(syncs)
            LOGGER.debug("Bemfa Cloud restore: _ensure_topics succeeded for %d syncs", len(syncs))
        except Exception as err:  # noqa: BLE001
            # Use repr() instead of str() because some exceptions (e.g.
            # aiohttp ClientError / RuntimeError "Session is closed") have
            # an empty str() but a meaningful repr().
            LOGGER.error(
                "Bemfa Cloud restore: _ensure_topics FAILED for %d syncs: "
                "%s (type=%s, repr=%r). "
                "Topics that should have been created: %s",
                len(syncs), err, type(err).__name__, err,
                [(s.topic, s.name) for s in syncs],
                exc_info=True,
            )
            raise

        try:
            await self._tcp.async_add_syncs(syncs)
            LOGGER.debug("Bemfa Cloud restore: TCP subscribe succeeded for %d syncs", len(syncs))
        except Exception as err:  # noqa: BLE001
            LOGGER.error(
                "Bemfa Cloud restore: TCP subscribe FAILED: %s (type=%s, repr=%r)",
                err, type(err).__name__, err,
                exc_info=True,
            )
            raise

        self._syncs_by_entity_id = {sync.entity_id: sync for sync in syncs}
        LOGGER.debug("Bemfa Cloud restore: done, %d syncs active", len(self._syncs_by_entity_id))

    def collect_supported_syncs(self) -> list[Sync]:
        """Collect all supported HA syncs."""

        syncs: list[Sync] = []
        for sync_type in SYNC_TYPES.values():
            try:
                syncs.extend(sync_type.collect_supported_syncs(self._hass))
            except Exception as err:  # noqa: BLE001
                LOGGER.debug("Failed to collect %s syncs: %s", sync_type.__name__, err)

        syncs = [sync for sync in syncs if not self._is_excluded_sync(sync)]
        covered_entity_ids = {sync.entity_id for sync in syncs}
        syncs.extend(self._collect_fallback_switch_syncs(covered_entity_ids))
        return sorted(syncs, key=lambda item: item.entity_id)

    def _collect_fallback_switch_syncs(self, covered_entity_ids: set[str]) -> list[Sync]:
        """Map unrecognized turnable entities to Bemfa switch devices."""

        from .sync_switch import Switch

        fallback_syncs: list[Sync] = []
        for state in self._hass.states.async_all():
            try:
                if state.entity_id in covered_entity_ids:
                    continue
                if self._is_excluded_entity_id(state.entity_id):
                    continue

                domain = state.entity_id.split(".", 1)[0]
                if not (
                    self._hass.services.has_service(domain, SERVICE_TURN_ON)
                    and self._hass.services.has_service(domain, SERVICE_TURN_OFF)
                ):
                    continue

                fallback_syncs.append(Switch(self._hass, state.entity_id, state.name))
            except Exception as err:  # noqa: BLE001
                LOGGER.debug("Failed to collect fallback sync for %s: %s", state.entity_id, err)
        return fallback_syncs

    async def async_create_sync(self, sync: Sync, user_input: dict[str, str]) -> None:
        """Create one sync."""

        sync.name = user_input.get(OPTIONS_NAME, sync.name)
        sync.config = user_input.copy()
        await self._ensure_topics([sync])
        await self._tcp.async_add_sync(sync)
        self._syncs_by_entity_id[sync.entity_id] = sync

    async def async_create_syncs(self, syncs: list[Sync]) -> None:
        """Create multiple syncs with default names."""

        for sync in syncs:
            sync.config = {OPTIONS_NAME: sync.name}
        await self._ensure_topics(syncs)
        await self._tcp.async_add_syncs(syncs)
        self._syncs_by_entity_id.update({sync.entity_id: sync for sync in syncs})

    async def async_modify_sync(self, sync: Sync, user_input: dict[str, str]) -> None:
        """Modify sync configuration and publish the latest state.

        If the user changed the device type override, the sync's effective
        Bemfa topic changes (because the topic embeds the 3-digit suffix).
        We need to:
          1. capture the old effective topic (for TCP unsubscribe)
          2. apply the new config (which changes `topic_suffix` / `topic`)
          3. delete the OLD topic from Bemfa Cloud (best effort)
          4. create the NEW topic on Bemfa cloud
          5. re-subscribe on the TCP long connection
        The persistent config key (`default_topic`) is unchanged.
        """

        from .const import OPTIONS_DEVICE_TYPE

        old_topic = sync.topic
        old_override = sync.config.get(OPTIONS_DEVICE_TYPE) if sync.config else ""

        sync.name = user_input.get(OPTIONS_NAME, sync.name)
        sync.config = user_input.copy()
        new_override = sync.config.get(OPTIONS_DEVICE_TYPE, "")

        # Empty string means "auto" (use the HA-domain-implied suffix).
        type_changed = (old_override or "") != (new_override or "")

        if type_changed:
            new_topic = sync.topic
            # Update the stored config under the stable default_topic key.
            self._config[sync.default_topic] = sync.config.copy()
            # Unsubscribe the old effective topic and re-subscribe the new one.
            if old_topic != new_topic:
                await self._tcp.async_remove_sync(old_topic)
                # Try to delete the old topic from Bemfa Cloud.
                # If Bearer token is configured, uses v5 API (works for type=7).
                # Otherwise, falls back to v1 API (may fail for type=7).
                try:
                    await self.async_delete_cloud_topic(old_topic)
                    LOGGER.debug(
                        "Bemfa Cloud: deleted old topic %s after type change",
                        old_topic,
                    )
                except Exception as err:  # noqa: BLE001
                    LOGGER.warning(
                        "Bemfa Cloud: failed to delete old topic %s after type "
                        "change: %s. You may need to remove it manually in the "
                        "Bemfa console.",
                        old_topic, err,
                    )
                await self._ensure_topics([sync])
                await self._tcp.async_add_sync(sync)
            else:
                # Override resolved to the same suffix as the default — no
                # topic change, just publish the latest state.
                await self._tcp.async_update_sync(sync)
            self._syncs_by_entity_id[sync.entity_id] = sync
            return

        self._config[sync.default_topic] = sync.config.copy()
        await self._ensure_topics([sync])
        await self._tcp.async_update_sync(sync)
        self._syncs_by_entity_id[sync.entity_id] = sync

    async def async_delete_cloud_topic(self, topic: str) -> None:
        """Delete a single topic from Bemfa Cloud (on-demand token).

        Ensures a valid Bearer token is available before calling v5 delete.
        If token is expired:
        - email+password configured → auto-login
        - WeChat scan mode → show QR notification, wait for scan
        If no token can be obtained, raises an error.
        """

        token = await self._async_ensure_valid_token()
        if not token:
            raise BemfaCloudApiError(
                "No valid Bearer token available for cloud deletion. "
                "Please configure email+password or scan WeChat QR."
            )
        await self._http.async_delete_topic_v5(topic, token)

    def has_bearer_token(self) -> bool:
        """Return whether a Bearer token is available (for UI display)."""
        return bool(self._bearer_token) or bool(self._email and self._password)

    async def async_destroy_sync(self, topic: str) -> None:
        """Remove a local sync AND delete its topic from Bemfa Cloud.

        `topic` here is the *default_topic* (the stable config key)
        shown in the destroy menu. We need to find the corresponding
        sync, unsubscribe its *effective* topic from TCP, delete the
        *effective* topic from Bemfa Cloud, and drop the config entry.
        Cloud-deletion is best-effort — failures are logged but do not
        block local cleanup.
        """

        # Find the sync whose default_topic matches the one the user picked.
        target_sync: Sync | None = None
        for sync in list(self._syncs_by_entity_id.values()):
            if sync.default_topic == topic:
                target_sync = sync
                break

        if target_sync is not None:
            effective_topic = target_sync.topic
            # Unsubscribe the *effective* topic (which may differ from
            # default_topic if the user set a type override).
            await self._tcp.async_remove_sync(effective_topic)
            # Best-effort cloud-side delete.
            try:
                await self._http.async_delete_topic(effective_topic)
            except Exception as err:  # noqa: BLE001
                LOGGER.warning(
                    "Failed to delete Bemfa cloud topic %s: %s. "
                    "You may need to remove it manually in the Bemfa console.",
                    effective_topic,
                    err,
                )
            self._syncs_by_entity_id = {
                entity_id: sync
                for entity_id, sync in self._syncs_by_entity_id.items()
                if sync.entity_id != target_sync.entity_id
            }

        self._config.pop(topic, None)

    async def _ensure_topics(self, syncs: list[Sync]) -> None:
        if not syncs:
            LOGGER.debug("Bemfa Cloud _ensure_topics: no syncs to create, skipping")
            return
        payloads = [
            TopicPayload(topic=sync.topic, name=sync.name, room=self._sync_room(sync))
            for sync in syncs
        ]
        LOGGER.warning(
            "Bemfa Cloud _ensure_topics: creating %d topics: %s",
            len(payloads),
            [(p.topic, p.name) for p in payloads],
        )
        await self._http.async_create_topics(payloads)
        LOGGER.debug("Bemfa Cloud _ensure_topics: API call returned successfully")

    def _start_registry_listeners(self) -> None:
        """Listen for HA name and area changes and mirror them to Bemfa."""

        self._unsub_registry_listeners.append(
            self._hass.bus.async_listen(
                entity_registry.EVENT_ENTITY_REGISTRY_UPDATED,
                self._async_entity_registry_updated,
            )
        )
        self._unsub_registry_listeners.append(
            self._hass.bus.async_listen(
                device_registry.EVENT_DEVICE_REGISTRY_UPDATED,
                self._async_device_registry_updated,
            )
        )
        self._unsub_registry_listeners.append(
            self._hass.bus.async_listen(
                area_registry.EVENT_AREA_REGISTRY_UPDATED,
                self._async_area_registry_updated,
            )
        )

    async def _async_entity_registry_updated(self, event: Event) -> None:
        """Mirror HA entity name and room changes to Bemfa."""

        if event.data.get("action") != "update":
            return

        entity_id = event.data.get("entity_id")
        old_entity_id = event.data.get("old_entity_id")
        sync = self._syncs_by_entity_id.get(entity_id)
        if sync is None and old_entity_id:
            sync = self._syncs_by_entity_id.pop(old_entity_id, None)
            if sync is not None:
                sync._entity_id = entity_id
                self._syncs_by_entity_id[entity_id] = sync
        if sync is None:
            return
        if self._is_excluded_sync(sync):
            return

        changes = event.data.get("changes") or {}
        if any(key in changes for key in ("name", "name_by_user", "original_name")):
            await self._sync_bemfa_name(sync)

        if any(key in changes for key in ("area_id", "device_id")):
            await self._sync_bemfa_room(sync)

    async def _async_device_registry_updated(self, event: Event) -> None:
        """Mirror HA device room changes to all related Bemfa topics."""

        if event.data.get("action") != "update":
            return
        if "area_id" not in (event.data.get("changes") or {}):
            return

        device_id = event.data.get("device_id")
        entity_reg = entity_registry.async_get(self._hass)
        for entry in entity_registry.async_entries_for_device(entity_reg, device_id):
            if sync := self._syncs_by_entity_id.get(entry.entity_id):
                await self._sync_bemfa_room(sync)

    async def _async_area_registry_updated(self, event: Event) -> None:
        """Mirror HA area rename/removal to affected Bemfa topic rooms."""

        if event.data.get("action") not in ("update", "remove"):
            return

        area_id = event.data.get("area_id")
        for sync in self._syncs_by_entity_id.values():
            if self._sync_area_id(sync) == area_id:
                await self._sync_bemfa_room(sync)

    async def _sync_bemfa_name(self, sync: Sync) -> None:
        entry = entity_registry.async_get(self._hass).async_get(sync.entity_id)
        if entry is not None:
            name = entity_registry.async_get_full_entity_name(self._hass, entry)
        else:
            state = self._hass.states.get(sync.entity_id)
            name = state.name if state is not None else sync.name
        if name == sync.name:
            return
        sync.name = name
        try:
            await self._http.async_modify_name(sync.topic, name)
        except Exception as err:  # noqa: BLE001
            LOGGER.debug("Failed to sync Bemfa topic name for %s: %s", sync.topic, err)

    async def _sync_bemfa_room(self, sync: Sync) -> None:
        try:
            await self._http.async_modify_room([sync.topic], self._sync_room(sync))
        except Exception as err:  # noqa: BLE001
            LOGGER.debug("Failed to sync Bemfa topic room for %s: %s", sync.topic, err)

    def _sync_room(self, sync: Sync) -> str:
        """Return the HA area name that should be used as Bemfa room."""

        area_id = self._sync_area_id(sync)
        if area_id is None:
            return ""
        area = area_registry.async_get(self._hass).async_get_area(area_id)
        return area.name if area is not None else ""

    def _sync_area_id(self, sync: Sync) -> str | None:
        """Return entity or device area id for a sync."""

        entity_entry = entity_registry.async_get(self._hass).async_get(sync.entity_id)
        if entity_entry is None:
            return None
        if entity_entry.area_id:
            return entity_entry.area_id
        if entity_entry.device_id is None:
            return None
        device = device_registry.async_get(self._hass).async_get(entity_entry.device_id)
        return device.area_id if device is not None else None

    def _is_excluded_sync(self, sync: Sync) -> bool:
        """Return if a HA entity should never be mirrored to Bemfa."""

        return self._is_excluded_entity_id(sync.entity_id)

    def _is_excluded_entity_id(self, entity_id: str) -> bool:
        """Skip entities created by BeHome or by this integration."""

        entry = entity_registry.async_get(self._hass).async_get(entity_id)
        if entry is None:
            return False
        if entry.platform in EXCLUDED_SOURCE_PLATFORMS:
            return True
        return bool(entry.unique_id) and entry.unique_id.startswith(
            tuple(f"{platform}_" for platform in EXCLUDED_SOURCE_PLATFORMS)
        )
