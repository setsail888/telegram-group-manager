# 全部功能导航

管理员统一入口：**私聊机器人 `/start` → 选择群组**。下表的路径从该群管理菜单开始；成员服务和服务器命令另有入口。

## 违禁词、反垃圾与防刷屏

| 功能 | 按钮入口 | 使用说明 |
| --- | --- | --- |
| 自定义关键词、匹配方式、文字测试 | 违禁词 | [添加与测试关键词](../moderation/keywords.md) |
| 13类内置词库、词条排除 | 违禁词 → 分类词库 | [启用分类词库](../moderation/word-packs.md) |
| 两字词、繁简、同音/拼音、拆字 | 违禁词 → 中文变体识别 | [中文变体](../moderation/variants.md) |
| 文件导入、分段导出、精确例外 | 违禁词 | [词库迁移与例外](../moderation/import-export.md) |
| 链接、伪装网址、批量提及、全局观察 | 智能反垃圾 / 链接管理 | [反垃圾设置](../moderation/anti-spam.md) |
| 域名及子域名放行 | 域名白名单 | [白名单](../moderation/anti-spam.md#域名白名单) |
| 转发与外部频道身份 | 禁止转发 / 智能反垃圾 | [转发和频道](../moderation/anti-spam.md#转发和频道身份) |
| 个人频率、相同文字、媒体连发 | 防刷屏 | [个人防刷屏](../moderation/anti-flood.md) |
| 相同图片、相册合并计数 | 图片防刷 / 防刷屏 → 媒体连发 | [图片与相册](../moderation/anti-flood.md#图片与相册) |
| 多账号同内容清理 | 防刷屏 → 跨账号重复 | [跨账号重复检测](../cross-account-flood.md) |
| 警告、禁言、踢出、封禁、仅撤回 | 防刷屏 → 惩罚 | [处罚选择](../moderation/penalties.md) |
| 图片文字审核 | 图片 OCR | [OCR部署与使用](../moderation/ocr.md) |

## 入群与成员

| 功能 | 按钮入口 | 使用说明 |
| --- | --- | --- |
| 入群答题、时限与答错次数 | 新人验证 | [新人验证](../members/verification.md) |
| 短时间大量入群保护 | 集中入群防护 | [集中入群保护](../members/raid.md) |
| 昵称限制 | 新成员限制 | [昵称与成员管理](../members/management.md) |
| 拦截其他机器人入群 | 机器人防护 | [机器人防护](../members/management.md#其他机器人入群) |
| 手动警告、禁言、移出、封禁及解除 | 成员管理 | [手动管理](../members/management.md#手动管理成员) |
| 群类型与机器人权限 | 权限检查 | [权限说明](permissions.md) |

## 群运营与成员服务

| 功能 | 按钮入口 | 使用说明 |
| --- | --- | --- |
| 欢迎语、群规与欢迎限频 | 欢迎消息 | [欢迎消息](../operations/welcome.md) |
| 多关键词、媒体及按钮回复 | 自动回复 | [自动回复](../operations/replies.md) |
| 单次、每日、每周定时发送 | 定时公告 | [定时公告](../operations/schedules.md) |
| 今日、近7天、活跃排行与规则总览 | 对应统计页面 | [统计与总览](../operations/statistics.md) |
| 命中原因、删除结果与独立日志群 | 防护记录 / 管理日志 | [记录与日志](../operations/statistics.md#防护记录) |
| 签到、积分与管理员调分 | 积分签到 | [积分与转赠](../community/points.md) |
| 虚拟奖励、订单核销与退款 | 积分商城 | [商城与订单](../community/shop.md) |
| 免费报名与开奖 | 免费抽奖 | [免费抽奖](../community/raffle.md) |
| 成员签到、订单、群规、ID入口 | 群内 `/menu` → 成员服务 | [成员按钮菜单](../community/menu.md) |

## 服务器维护与参考

- [一键安装、NAS部署](../server-one-click.md)
- [gg菜单、更新、备份与卸载](../maintenance/server.md)
- [只读网页看板](../maintenance/dashboard.md)
- [故障排查](../maintenance/troubleshooting.md)
- [全部群内命令](../commands.md)

新分类词库、中文变体与跨账号检测默认关闭。默认值说明指新群；已有群更新后保留原配置，以实际页面显示为准。
