# Changelog

## 0.1.6 (fork by @tiejiang29)

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
