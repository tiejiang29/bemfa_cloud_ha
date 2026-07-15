# Bemfa Cloud Home Assistant 集成（修改版）

[![GitHub Release](https://img.shields.io/github/v/release/tiejiang29/bemfa_cloud_ha.svg)](https://github.com/tiejiang29/bemfa_cloud_ha/releases)
[![GitHub Activity](https://img.shields.io/github/commit-activity/y/tiejiang29/bemfa_cloud_ha.svg)](https://github.com/tiejiang29/bemfa_cloud_ha/commits/main)
[![License](https://img.shields.io/github/license/tiejiang29/bemfa_cloud_ha.svg)](LICENSE)

[![hacs](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://hacs.xyz)

_将 Home Assistant 中的设备同步到巴法云，通过小爱同学等语音助手控制 HA 设备。_

**基于 [`bemfa/bemfa_cloud_ha`](https://github.com/bemfa/bemfa_cloud_ha) 官方插件修改，已合并官方全部更新，并额外增加了多项实用功能。**

简体中文 | [English](README_en.md)

## 与官方版本的差异

| 特性 | 官方版本 | 修改版 |
| --- | --- | --- |
| 设备类型覆盖 | ❌ | ✅ 可手动把任意实体映射为任意巴法云设备类型（如 switch → 灯） |
| 空调调温度模式不变 | ❌ 调温度后模式变自动（issue #16 未修复） | ✅ 已修复 |
| 自动删除云端主题 | ❌ 需手动清理 | ✅ 通过 Bearer Token 自动删除 |
| Token 自动获取 | ❌ 无 | ✅ 邮箱+密码自动登录 / 微信扫码续期 |
| 热重载不断 TCP | ❌ 改配置后设备离线 | ✅ 配置变更时 TCP 保持连接 |
| 长名称自动截断 | ❌ 超长名称创建失败 | ✅ 自动截断到 30 字节 |
| 名称同步到巴法云 | ❌ 改名后云端不更新 | ✅ 自动调用 modifyName 更新 |
| 列表显示巴法云类型 | ❌ 只显示 HA 域名 | ✅ 显示 `[switch→灯] 餐厅灯` |
| 空调扫风控制 | ✅ | ✅ 已同步 |
| CameraState 兼容 | ✅ | ✅ 已同步 |
| OAuth 默认凭据 | ✅ | ✅ 已同步 |
| 反回声机制 | ✅ | ✅ 保留 |
| 名称/房间镜像 | ✅ | ✅ 保留 |
| Stable Topic ID | ✅ | ✅ 保留 |
| BeHome 防环 | ✅ | ✅ 保留 |

## 功能特性

- **三种认证方式**：私钥 / 微信扫码 / OAuth 登录
- **按需创建主题**：选择需要同步的 HA 实体后，创建为巴法云 TCP V2 主题
- **批量创建**：多个设备会优先使用批量接口创建
- **TCP 长连接**：使用巴法云 TCP JSON V2 长连接订阅控制消息
- **状态同步**：HA 状态变化会同步到巴法云云端缓存
- **昵称和房间同步**：HA 中修改实体昵称、区域或区域名称后，会同步到巴法云设备昵称和房间
- **来源过滤**：自动跳过 BeHome 生成的 HA 实体，避免控制回环
- **设备类型覆盖**：可手动把任意实体映射为任意巴法云设备类型（如 switch → 灯）
- **自动删除云端主题**：移除同步或修改类型时，自动删除巴法云云端 topic
- **Token 自动管理**：邮箱+密码自动登录获取 Token，或微信扫码续期
- **空调扫风控制**：支持左右扫风（l2r）和上下扫风（u2d）

## 支持的设备类型

| 巴法云后缀 | 设备类型 | HA 默认映射 |
| --- | --- | --- |
| `001` | 插座 | `switch` 且 `device_class=outlet` |
| `002` | 灯泡 | `light` |
| `003` | 风扇 | 普通 `fan` |
| `004` | 传感器 | `sensor`、`binary_sensor` |
| `005` | 空调 | 支持制冷/制热的 `climate` |
| `006` | 开关 | `switch`、`input_boolean`、`script`、`automation`、`remote`，以及其他支持 `turn_on/turn_off` 的实体 |
| `009` | 窗帘 | `cover` |
| `010` | 温控器 | 非空调类 `climate` |
| `011` | 热水器 | `water_heater` |
| `012` | 电视 | `media_player` |
| `013` | 空气净化器 | `fan` 且 `device_class=air_purifier` |

## 安装方法

### HACS 自定义仓库安装（推荐）

1. 在 Home Assistant 中打开 **HACS**
2. 右上角 ⋮ 菜单 → **自定义存储库**（Custom repositories）
3. 填写：
   - **存储库地址**：`https://github.com/tiejiang29/bemfa_cloud_ha`
   - **类型**：Integration（集成）
4. 点击 **添加**，然后关闭
5. 在 HACS 搜索 **Bemfa Cloud**
6. 点击 **下载**，重启 Home Assistant

### 接收自动更新

HACS 会自动检测本仓库的新 Release。当有新版本发布时：

1. Home Assistant **设置 → 更新** 里会出现 `update.bemfa_cloud_update` 实体
2. 点击 **安装** 即可一键升级
3. 升级后 HA 会提示重启，重启后新版本生效

### 从官方版本迁移

如果你已经安装了官方 `bemfa/bemfa_cloud_ha`：

1. **不要先卸载官方版本**（避免丢失现有的同步配置）
2. 在 HACS 删除官方 Bemfa Cloud（配置 entry 会保留）
3. 按上面的步骤添加本修改版为自定义仓库并下载
4. 重启 HA
5. 进入 **设置 → 设备与服务 → Bemfa Cloud → 配置**，你原来的同步配置应该都还在
6. 现在可以在"编辑同步配置"里看到新的"巴法云设备类型（覆盖）"下拉框

### 手动安装

1. 打开 Home Assistant 配置目录（包含 `configuration.yaml` 的目录）
2. 如果没有 `custom_components` 目录，请创建一个
3. 从 [Latest Release](https://github.com/tiejiang29/bemfa_cloud_ha/releases/latest) 下载 zip 包
4. 解压后把 `custom_components/bemfa_cloud/` 复制到 HA 的 `custom_components/` 目录
5. 重启 Home Assistant

## 配置说明

### 私钥方式

1. 进入 **设置 → 设备与服务 → 添加集成**
2. 搜索 **Bemfa Cloud**
3. 选择 **私钥**
4. 输入巴法云用户私钥 `uid`
5. （可选）填入 **邮箱 + 密码**：用于自动获取 Bearer Token，移除同步时自动删除云端主题
6. （可选）填入 **Bearer Token**：手动填入 token，不填邮箱密码时使用。获取方法：登录 cloud.bemfa.com → F12 → Application → Cookies → 复制 token。约 30 天过期。
7. 保存后，进入 Bemfa Cloud 的配置页面，选择需要同步的 HA 实体

### Token 管理方案

| 登录方式 | 获取 Token | 刷新方式 | 过期处理 |
| --- | --- | --- | --- |
| **邮箱+密码（推荐）** | 删除时自动登录 | 自动 | 永久有效 |
| **微信扫码** | 扫码时自动获取 | 弹通知+二维码扫码续期 | 扫码后继续 |
| **手动 Token** | 手动填入 | 无 | 30 天后手动重新填 |
| **纯私钥** | 无 | 无 | 手动删云端 |

### OAuth 方式

大多数用户建议使用私钥方式。OAuth 登录复用 BeHome 的认证方式，主要用于需要 BeHome 授权流程的场景。

## 设备类型覆盖

默认情况下，集成根据 HA 实体的 domain 自动决定巴法云设备类型。但有些场景下你可能希望**手动**改变映射，例如：

- 用智能插座（`switch`）接了一盏灯，希望小爱同学的"关全部灯"能联动到这个插座
- 用智能开关（`switch`）接了风扇，希望被识别为风扇（`003`）

### 使用方法

1. 进入 **设置 → 设备与服务 → Bemfa Cloud → 配置**
2. 选择 **编辑同步配置**
3. 选中要修改的实体
4. 在配置表单里找到 **巴法云设备类型（覆盖）** 下拉框
5. 选择目标类型（或选"自动"恢复默认），点击提交

### 支持的覆盖目标

| 后缀 | 类型 | 说明 |
| --- | --- | --- |
| (自动) | — | 按 HA domain 自动决定（默认行为） |
| `001` | 插座 | |
| `002` | 灯 | 让语音"开/关灯"指令联动到此实体 |
| `003` | 风扇 | |
| `004` | 传感器 | |
| `005` | 空调 | |
| `006` | 开关 | |
| `009` | 窗帘 | |

### 智能降级

如果源实体不支持目标类型的所有属性，会自动降级：

- `switch` 覆盖为 `002 灯`：只同步 on/off，亮度/颜色字段留空
- `switch` 覆盖为 `003 风扇`：只同步 on/off
- `light` 覆盖为 `006 开关`：只同步 on/off，亮度/颜色不上报

## 注意事项

- 本集成方向是 **HA → 巴法云**
- BeHome 集成方向是 **巴法云 → HA**
- 两个集成可以同时安装，Bemfa Cloud 会跳过 BeHome 生成的实体
- 同一个 HA 实体可以分别添加到不同 Bemfa Cloud 中枢，配置互相独立
- 集成会尽量使用 HA 的稳定实体标识生成巴法云 topic；没有稳定标识的实体会退回使用 `entity_id`

## 常见问题

### 移除本地同步会删除巴法云设备吗？

如果配置了邮箱+密码或 Bearer Token，会自动删除云端主题。否则需要手动到巴法云控制台删除。

### 空调风速 `fan` 是怎么对应的？

`fan=0` 表示自动风速，`fan=1` 到 `fan=5` 表示一档到五档风速，`fan=7/8/9` 分别表示低风、中风、高风。可在"编辑同步配置"里调整映射。

### 调温度后空调模式变了？

官方版本存在此问题（issue #16），本修改版已修复——调温度时不会调用 `turn_on`，避免模式被重置。

## 支持与反馈

- [GitHub Issues](https://github.com/tiejiang29/bemfa_cloud_ha/issues)
- [Home Assistant 中文社区论坛](https://bbs.hassbian.com/)

## 致谢

- [bemfa/bemfa_cloud_ha](https://github.com/bemfa/bemfa_cloud_ha) — 官方巴法云 HA 集成
- [larry-wong/bemfa](https://github.com/larry-wong/bemfa) — 早期社区参考项目

## 许可证

本项目使用 MIT 许可证 - 查看 [LICENSE](LICENSE) 文件了解详情。
