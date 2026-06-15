# Bemfa Cloud Home Assistant 集成

[![GitHub Release][releases-shield]][releases]
[![GitHub Activity][commits-shield]][commits]
[![License][license-shield]](LICENSE)

[![hacs][hacsbadge]][hacs]
[![Community Forum][forum-shield]][forum]

_将 Home Assistant 中的设备同步到巴法云。_

**Bemfa Cloud 会把 HA 里的本地实体映射为巴法云 TCP V2 设备，让用户可以通过巴法云、小爱同学等入口控制 HA 设备。**

简体中文 | [English](README_en.md)

## 功能特性

- **两种认证方式**：支持直接输入巴法云私钥，也支持 OAuth 登录
- **自动创建主题**：自动把 HA 实体创建为巴法云 TCP V2 主题
- **批量创建**：多个设备会优先使用批量接口创建
- **TCP 长连接**：使用巴法云 TCP JSON V2 长连接订阅控制消息
- **状态同步**：HA 状态变化会同步到巴法云云端缓存
- **昵称和房间同步**：HA 中修改实体昵称、区域或区域名称后，会同步到巴法云设备昵称和房间
- **稳定 topic**：基于 HA entity registry 的稳定 ID 生成 topic，修改实体 ID、昵称或房间不会改变 topic
- **来源过滤**：自动跳过 BeHome 生成的 HA 实体，避免把巴法云设备再次同步回巴法云

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

### HACS 安装（推荐）

1. 在 Home Assistant 中打开 HACS
2. 进入 **集成**
3. 点击 **浏览和下载存储库**
4. 搜索 **Bemfa Cloud**
5. 下载并重启 Home Assistant

### HACS 自定义仓库安装（开发/测试）

如果 HACS 商店暂未收录，或者需要测试开发版本：

1. 在 HACS 的右上角菜单中选择 **自定义存储库**
2. 填写存储库地址：`https://github.com/bemfa/bemfa_cloud_ha`
3. 类型选择 **集成**
4. 添加后搜索 **Bemfa Cloud** 并下载
5. 重启 Home Assistant

### 手动安装

1. 打开 Home Assistant 配置目录，也就是包含 `configuration.yaml` 的目录
2. 如果没有 `custom_components` 目录，请创建一个
3. 将本仓库的 `custom_components/bemfa_cloud` 复制到 HA 的 `custom_components` 目录中
4. 重启 Home Assistant

## 配置说明

### 私钥方式

1. 进入 **设置** -> **设备与服务** -> **添加集成**
2. 搜索 **Bemfa Cloud**
3. 选择 **私钥**
4. 输入巴法云用户私钥 `uid`
5. 保存后，集成会自动发现 HA 支持的实体并创建巴法云主题

### OAuth 方式

OAuth 登录复用 BeHome 的认证方式。仅使用 OAuth 登录的用户需要先配置应用程序凭据。

1. 进入 **设置** -> **设备与服务** -> **助手** -> **应用程序凭据**
2. 创建新的应用程序凭据：
   - **名称**：`Bemfa Cloud`
   - **域**：`bemfa_cloud`
   - **客户端 ID**：`88ac425b4558463aa813aed1690db730`
   - **客户端密钥**：可填写任意安全字符串
3. 回到 **集成** 页面添加 **Bemfa Cloud**
4. 选择 OAuth 并完成授权

如果授权后跳转到 `homeassistant.local` 并提示无法访问，请在 Home Assistant 的 **设置** -> **系统** -> **网络** 中配置正确的 Home Assistant URL。Docker 本机测试可使用 `http://localhost:8123`，局域网访问可使用 `http://HA主机IP:8123`。

## 工作方式

1. 集成读取 HA 中支持的实体
2. 排除 BeHome 和 Bemfa Cloud 自身生成的实体
3. 根据实体类型生成巴法云设备 topic
4. 使用 NoSecret 接口创建主题，固定 `type=7`、`region=cn-03`
5. 通过 TCP V2 长连接批量订阅所有 topic
6. 收到巴法云控制消息后调用 HA 服务控制实体
7. HA 实体状态变化后，同步状态到巴法云

## Topic 规则

topic 使用以下规则生成：

```text
ha + md5(稳定来源ID)前12位 + 三位设备类型后缀
```

稳定来源 ID 优先级：

1. HA entity registry 的 `unique_id`
2. HA entity registry 的 `entry.id`
3. 实体不在 registry 中时，退回 `entity_id`

## 注意事项

- 本集成方向是 **HA -> 巴法云**
- BeHome 集成方向是 **巴法云 -> HA**
- 两个集成可以同时安装，但 Bemfa Cloud 会跳过 BeHome 生成的实体，避免重复同步或控制回环
- 如果旧版本已经按旧 topic 规则创建过主题，切换到稳定 topic 规则后可能会创建新的主题，旧主题需要在巴法云控制台手动清理

## 支持与反馈

- [GitHub Issues](https://github.com/bemfa/bemfa_cloud_ha/issues)
- [Home Assistant 中文社区论坛](https://bbs.hassbian.com/)
- [Home Assistant 官方社区论坛](https://community.home-assistant.io/)

## 许可证

本项目使用 MIT 许可证 - 查看 [LICENSE](LICENSE) 文件了解详情。

---

[commits-shield]: https://img.shields.io/github/commit-activity/y/bemfa/bemfa_cloud_ha.svg?style=for-the-badge
[commits]: https://github.com/bemfa/bemfa_cloud_ha/commits/main
[hacs]: https://hacs.xyz
[hacsbadge]: https://img.shields.io/badge/HACS-Custom-orange.svg?style=for-the-badge
[forum-shield]: https://img.shields.io/badge/community-forum-brightgreen.svg?style=for-the-badge
[forum]: https://community.home-assistant.io/
[license-shield]: https://img.shields.io/github/license/bemfa/bemfa_cloud_ha.svg?style=for-the-badge
[releases-shield]: https://img.shields.io/github/release/bemfa/bemfa_cloud_ha.svg?style=for-the-badge
[releases]: https://github.com/bemfa/bemfa_cloud_ha/releases
