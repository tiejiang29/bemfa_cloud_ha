"""Bemfa Cloud sync service."""

from __future__ import annotations

from homeassistant.const import EVENT_HOMEASSISTANT_STARTED, SERVICE_TURN_OFF, SERVICE_TURN_ON
from homeassistant.core import CALLBACK_TYPE, CoreState, Event, HomeAssistant
from homeassistant.helpers import area_registry, device_registry, entity_registry

from .const import DOMAIN, EXCLUDED_SOURCE_PLATFORMS, LOGGER, OPTIONS_NAME
from .http import BemfaCloudHttp, TopicPayload
from .sync import SYNC_TYPES, Sync
from .tcp import BemfaCloudTcp


class BemfaCloudService:
    """Manage topic creation and TCP synchronization."""

    def __init__(self, hass: HomeAssistant, credentials: dict[str, str]) -> None:
        """Initialize the service."""

        self._hass = hass
        self._http = BemfaCloudHttp(hass, credentials)
        self._tcp = BemfaCloudTcp(hass, credentials["uid"])
        self._config: dict[str, dict[str, str]] = {}
        self._syncs_by_entity_id: dict[str, Sync] = {}
        self._unsub_registry_listeners: list[CALLBACK_TYPE] = []
        self._restore_in_progress: bool = False

    async def async_start(self, config: dict[str, dict[str, str]]) -> None:
        """Start service and restore configured syncs."""

        self._config = config
        await self._tcp.async_start()

        async def _start(event: Event | None = None) -> None:
            await self._async_restore_syncs()

        if self._hass.state == CoreState.running:
            self._hass.async_create_background_task(
                self._async_restore_syncs(), "bemfa_cloud_restore_syncs"
            )
        else:
            self._hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, _start)

        self._start_registry_listeners()

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
                LOGGER.warning("Failed to collect %s syncs: %s", sync_type.__name__, err)

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
                LOGGER.warning("Failed to collect fallback sync for %s: %s", state.entity_id, err)
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
                # Note: we do NOT delete the old topic from Bemfa Cloud
                # because /v1/deleteTopic has a bug for type=7 (returns
                # 40000 "Unknown error"). The user must manually delete
                # the orphaned topic in the Bemfa console. This matches
                # the official bemfa_cloud_ha plugin's behavior.
                LOGGER.warning(
                    "Bemfa Cloud: type override changed for %s. "
                    "Old topic %s is now orphaned on Bemfa Cloud — "
                    "please delete it manually in the Bemfa console. "
                    "New topic %s will be created.",
                    sync.entity_id, old_topic, new_topic,
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
        """Delete a single topic from Bemfa Cloud.

        Thin wrapper around `BemfaCloudHttp.async_delete_topic` so config_flow
        can call it without touching the HTTP client directly. Best-effort:
        callers should catch and log exceptions.
        """

        await self._http.async_delete_topic(topic)

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
            LOGGER.warning("Failed to sync Bemfa topic name for %s: %s", sync.topic, err)

    async def _sync_bemfa_room(self, sync: Sync) -> None:
        try:
            await self._http.async_modify_room([sync.topic], self._sync_room(sync))
        except Exception as err:  # noqa: BLE001
            LOGGER.warning("Failed to sync Bemfa topic room for %s: %s", sync.topic, err)

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
