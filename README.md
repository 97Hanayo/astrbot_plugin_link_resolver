# 🔗 Link Resolver

[![AstrBot Plugin](https://img.shields.io/badge/AstrBot-Plugin-blue?style=flat-square)](https://github.com/Soulter/AstrBot)
[![License: GPL-3.0](https://img.shields.io/badge/License-GPL--3.0-green?style=flat-square)](LICENSE)

支持监听群内 **B站** / **抖音** / **小红书** 链接，自动解析并下载发送视频或图集内容。无需任何命令，发链接即可触发。

---

## ✨ 特性

- 📺 **B站视频解析**：支持多种画质选择，支持多P视频批量下载
- 🎵 **抖音解析**：支持视频和图文笔记，自动下载并发送
- 📕 **小红书原图解析**：支持视频和图文笔记，可下载原图
- 🚦 **群过滤(黑/白名单)**：按群号控制哪些群启用解析，私聊不受影响

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
| `general_settings.reaction_emoji_id` | 旧版单一 ID(仅当 list 为空时生效) | 128169 |
| `general_settings.reaction_emoji_list` | 回应表情 ID 列表(0~5个)，空=不回应 | `[147]` |
| `general_settings.reaction_emoji_strategy` | `随机` 抽1个 / `顺序循环` 全部依次回应(每个 0.5s) | `随机` |
| `general_settings.merge_send_as_sender` | 合并转发显示为原发送者 | ❌ 关闭 |
| `general_settings.error_notify_mode` | 失败时群内通知模式 | `静默` |

### 群过滤设置

按群号限制哪些群启用解析。私聊不受过滤影响，始终放行。

| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| `group_filter.mode` | 过滤模式：`黑名单`(列表内禁用)/ `白名单`(仅列表内启用) | `黑名单` |
| `group_filter.group_list` | 群号列表(QQ 群号,纯数字字符串) | `[]` |

**示例**：

```jsonc
"group_filter": {
    "mode": "黑名单",
    "group_list": ["123456789", "987654321"]   // 这两个群不解析
}
```

```jsonc
"group_filter": {
    "mode": "白名单",
    "group_list": ["123456789"]   // 只有这个群解析
}
```

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

---

## 📚 常用 emoji_id 速查

`reaction_emoji_list` 字段填入的 ID 取自 QQ 机器人官方文档。完整列表: https://bot.q.qq.com/wiki/develop/api-v2/openapi/emoji/model.html

### 系统表情 (type=1)

| ID  | 含义     | ID   | 含义    | ID  | 含义     |
| --- | -------- | ---- | ------- | --- | -------- |
| 4   | 得意     | 96   | 冷汗    | 171 | 茶       |
| 5   | 流泪     | 97   | 擦汗    | 173 | 泪奔     |
| 8   | 睡       | 98   | 抠鼻    | 174 | 无奈     |
| 9   | 大哭     | 99   | 鼓掌    | 175 | 卖萌     |
| 10  | 尴尬     | 100  | 糗大了  | 176 | 小纠结   |
| 12  | 调皮     | 101  | 坏笑    | 179 | doge     |
| 14  | 微笑     | 102  | 左哼哼  | 180 | 惊喜     |
| 16  | 酷       | 103  | 右哼哼  | 181 | 骚扰     |
| 21  | 可爱     | 104  | 哈欠    | 182 | 笑哭     |
| 23  | 傲慢     | 106  | 委屈    | 183 | 我最美   |
| 24  | 饥饿     | 109  | 左亲亲  | 201 | 点赞     |
| 25  | 困       | 111  | 可怜    | 214 | 啵啵     |
| 26  | 惊恐     | 116  | 示爱    | 222 | 抱抱     |
| 27  | 流汗     | 118  | 抱拳    | 264 | 捂脸     |
| 28  | 憨笑     | 120  | 拳头    | 271 | 吃瓜     |
| 29  | 悠闲     | 122  | 爱你    | 272 | 呵呵哒   |
| 30  | 奋斗     | 123  | NO      | 277 | 汪汪     |
| 32  | 疑问     | 124  | OK      | 305 | 右亲亲   |
| 33  | 嘘       | 125  | 转圈    | 314 | 仔细分析 |
| 34  | 晕       | 129  | 挥手    | 315 | 加油     |
| 38  | 敲打     | 144  | 喝彩    | 319 | 比心     |
| 39  | 再见     | 147  | 棒棒糖  | 320 | 庆祝     |
| 41  | 发抖     | 53   | 蛋糕    | 322 | 拒绝     |
| 42  | 爱情     | 60   | 咖啡    | 324 | 吃糖     |
| 43  | 跳跳     | 63   | 玫瑰    | 326 | 生气     |
| 49  | 拥抱     | 66   | 爱心    | -   | -        |
| 74  | 太阳     | 75   | 月亮    | -   | -        |
| 76  | 赞       | 78   | 握手    | -   | -        |
| 79  | 胜利     | 85   | 飞吻    | -   | -        |
| 89  | 西瓜     | -    | -       | -   | -        |

### Emoji 表情 (type=2)

填写 unicode codepoint 十进制即可 (例如 💩=128169, 👍=128077):

| ID       | 含义   | ID       | 含义   |
| -------- | ------ | -------- | ------ |
| 128076   | 👌 好的 | 128170   | 💪 肌肉 |
| 128077   | 👍 厉害 | 128235   | 📫 邮箱 |
| 128079   | 👏 鼓掌 | 128293   | 🔥 火   |
| 128147   | ❤️ 爱心 | 128513   | 😁 呲牙 |
| 128157   | 💝 礼物 | 128514   | 😂 激动 |
| 128164   | 💤 睡觉 | 128516   | 😄 高兴 |
| 128166   | 💦 水   | 128522   | 😊 嘿嘿 |
| 128168   | 💨 吹气 | 128527   | 😏 哼哼 |
| 128169   | 💩 粑粑 | 128532   | 😔 失落 |
| 128538   | 😚 亲亲 | 128557   | 😭 大哭 |
