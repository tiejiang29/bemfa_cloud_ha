# Changelog

## 0.1.6-type-override.2 (fork by @tiejiang29)

- **New feature: Auto-delete cloud topics on destroy / type change.**
  The destroy-sync flow and the type-override-change flow now both call
  the Bemfa Cloud `POST https://pro.bemfa.com/v1/deleteTopic` API to
  remove the corresponding topic from the cloud, so users no longer
  need to manually clean up orphan topics in the Bemfa console.
- Added `BemfaCloudHttp.async_delete_topic()` method that uses the
  legacy `/v1/deleteTopic` endpoint (no NoSecret variant exists for
  delete). Idempotent: business code 40004 (topic doesn't exist or not
  owned) is treated as success.
- Added `BemfaCloudService.async_delete_cloud_topic()` thin wrapper so
  `config_flow` can call cloud delete without touching the HTTP client.
- `async_modify_sync` now deletes the OLD effective topic from the cloud
  when the user changes the type override (best-effort, non-blocking).
- `async_destroy_sync` now also deletes the effective topic from the
  cloud (best-effort, non-blocking).
- `async_step_destroy_sync` in config_flow resolves the effective topic
  via the sync object (which honors type overrides) and calls cloud
  delete before dropping the local config entry.
- Cloud-deletion failures are logged as warnings but do NOT block local
  cleanup — the user can still retry manually in the Bemfa console.
- Updated destroy_sync translations (zh-Hans, en, strings.json) to
  describe the new auto-delete behavior.
- Updated README "修改类型后的云端清理" section.

## 0.1.6-type-override.1 (fork by @tiejiang29)

- **New feature: Device type override.** Add a "Bemfa device type (override)"
  dropdown to every sync's edit page. Users can now manually map any HA entity
  to any of the 7 base Bemfa device types (001 插座 / 002 灯 / 003 风扇 /
  004 传感器 / 005 空调 / 006 开关 / 009 窗帘). This makes it possible to, for
  example, map a `switch` to `002 灯` so that voice commands like "turn off all
  lights" include the smart plug running a light fixture.
- Smart graceful degradation when source entity lacks target-type attributes
  (e.g. switch→light only syncs on/off; brightness/color are silently omitted).
- Introduce a stable `default_topic` key for persistent config storage so that
  repeated type overrides never orphan the stored name / climate fan-speed
  mappings.
- Updated translations (zh-Hans, en) and strings.json for the new field.
- Updated README with a dedicated "设备类型覆盖" section.
- Bumped manifest version to 0.1.6 and added @tiejiang29 as codeowner.

## 0.1.4

- Fix entities being synced even when they were not selected.
- Improve TCP keepalive with 30-second heartbeats and periodic topic resubscription.
- Reload the integration when sync options change so runtime subscriptions match saved settings.
- Support multiple Bemfa Cloud hubs/accounts and show the account suffix in hub names.
- Improve climate control for `mode`, `t`, and `fan`, including full state feedback after control commands.
- Clarify README wording, FAQ, multi-account usage, and local sync removal behavior.
