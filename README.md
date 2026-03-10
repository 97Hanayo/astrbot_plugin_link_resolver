# 🔗 Link Resolver

[![AstrBot Plugin](https://img.shields.io/badge/AstrBot-Plugin-blue?style=flat-square)](https://github.com/Soulter/AstrBot)
[![License: GPL-3.0](https://img.shields.io/badge/License-GPL--3.0-green?style=flat-square)](LICENSE)

支持监听群内 **B站** / **抖音** / **小红书** 链接，自动解析并下载发送视频或图集内容。无需任何命令，发链接即可触发。

---

## ✨ 特性

- 📺 **B站视频解析**：支持多种画质选择，支持多P视频批量下载
- 🎵 **抖音解析**：支持视频和图文笔记，自动下载并发送
- 📕 **小红书原图解析**：支持视频和图文笔记，可下载原图

---

## ⚙️ 配置项
在 AstrBot 管理面板的插件配置中可调整以下选项：

`v1.0.9` 起配置面板改为按平台分组折叠；如果你是从旧版升级，建议重新检查一次配置值。

### 基础设置

| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| `enable_platforms` | 勾选要启用解析的平台 | B站, 抖音, 小红书 |
| `general_settings.retry_count` | 解析失败重试次数（所有平台共用） | 3 |
| `general_settings.max_video_size_mb` | 最大视频大小限制 (MB)，超过则跳过下载或自动降画质 | 200 |
| `general_settings.reaction_emoji_enabled` | 识别链接后是否发表情回应 | ✅ 开启 |
| `general_settings.reaction_emoji_id` | 回应的表情 ID (如 128169 👍) | 128169 |
| `general_settings.merge_send_as_sender` | 合并转发显示为原发送者 | ❌ 关闭 |
| `general_settings.error_notify_mode` | 失败时群内通知模式 | `静默` |

### B站设置

| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| `bili_settings.video_quality` | 默认下载画质 | `1080P高帧率` |
| `bili_settings.allow_quality_fallback` | 超限时自动降画质 | ✅ 开启 |
| `bili_settings.merge_send` | 合并转发发送（不开启则只发视频） | ❌ 关闭 |
| `bili_settings.enable_multi_page` | 启用多P视频下载 | ✅ 开启 |
| `bili_settings.multi_page_max` | 多P最多下载数量 | 3 |
| `bili_settings.max_duration_seconds` | 最大视频时长(秒)，超过即忽略 | 300 |
| `bili_settings.cookies` | B站 Cookies 文本 | 空 |

### 抖音设置

| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| `douyin_settings.max_media` | 图集最多发送媒体数 | 99 |
| `douyin_settings.merge_send` | 视频使用合并转发 | ❌ 关闭 |

### 小红书设置

| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| `xhs_settings.max_media` | 图集最多发送媒体数 | 99 |
| `xhs_settings.merge_send` | 视频使用合并转发 | ❌ 关闭 |
| `xhs_settings.download_original` | 下载原图（通常为 JPEG） | ✅ 开启 |
| `xhs_settings.prefer_ci_png` | 优先将图片转码为 PNG | ✅ 开启 |
| `xhs_settings.concurrent_download` | 并发下载图集图片 | ✅ 开启 |
| `xhs_settings.auto_unmerge_threshold_mb` | 图片总大小超过此值时停止合并转发 (MB) | 50 |
| `xhs_settings.qq_image_size_limit_mb` | 单张图片超过此值时转为文件发送 (MB) | 30 |


---

## 使用方法
直接在群内发送包含以下链接的消息即可自动解析

---

## 📁 目录结构

```
astrbot_plugin_link_resolver/
├── main.py              # 主入口
├── metadata.yaml        # 插件元信息
├── _conf_schema.json    # 配置项定义
├── requirements.txt     # 依赖
├── core/                # 核心解析模块
│   ├── bilibili/        # B站解析
│   ├── douyin/          # 抖音解析
│   ├── xiaohongshu/     # 小红书解析
│   └── common/          # 公共工具
├── cache/               # 媒体缓存目录
└── cookies/             # Cookies 存放目录
```

---

## 🍪 Cookies 配置（可选）

填写 B 站 Cookie 可解锁更高画质（如 1080P60、4K）。

### 方式一：管理面板配置（推荐）

1. 安装浏览器插件 [Cookies txt](https://microsoftedge.microsoft.com/addons/detail/cookies-txt/dilbcaaegopfblcjdjikanigjbcbngbk?)
2. 打开 [bilibili.com](https://www.bilibili.com) 并登录
3. 点击插件的 **Load Cookies**，复制全部内容
4. 在 AstrBot 管理面板 → 插件配置 → **B站Cookies** 粘贴

![获取cookie的插件](docs/images/image.png)

![点击 Load Cookies](docs/images/image-1.png)

### 方式二：手动放置文件

将 Cookie 内容保存到 `cookies/bili_cookies.txt`（插件会自动创建目录）


---


## 📄 许可证

本项目采用 [GPL-3.0](LICENSE) 许可证。

---

## 🙏 致谢

- [astrbot_plugin_parser](https://github.com/Zhalslar/astrbot_plugin_parser)
- [XHS-Downloader](https://github.com/JoeanAmier/XHS-Downloader) — 小红书图片下载参考实现
