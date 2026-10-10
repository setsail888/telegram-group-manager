# 快速开始

这页带你完成安装、添加机器人和第一次设置。已经安装的用户可直接看[功能导航](features.md)。

## 1. 创建机器人

1. 在 Telegram 打开 [@BotFather](https://t.me/BotFather)，发送 `/newbot`，按提示设置名称和用户名。
2. 保存 BotFather 提供的 Token，安装时输入。不要发到群聊或公开仓库。
3. 使用 `/setjoingroups` 允许加入群组；使用 `/setprivacy` 选择机器人并设为 Disable。

## 2. 安装到服务器

准备 Debian / Ubuntu 服务器，通过 SSH 登录，在**服务器终端**执行：

```bash
bash -o pipefail -c 'curl -fsSL https://raw.githubusercontent.com/slobys/telegram-group-manager/main/install.sh | bash'
```

按提示输入 Token，输入过程不显示字符。安装器启动容器并注册 `gg` / `GG` 快捷命令。需要 root 或 sudo 权限；缺少 curl、NAS 部署或自定义目录请看[安装部署详细教程](../server-one-click.md)。

安装完成后执行：

```bash
gg status
gg logs
```

容器显示 Running 后，还要确认日志中没有 Token、网络或 `409 Conflict` 错误。

## 3. 加入测试群并授权

1. 把机器人加入一个测试群，提升为管理员。
2. 授予删除消息权限；禁言、新人验证等还需要限制成员权限和超级群。
3. 群内发送 `/check`，检查权限；发送 `/panel` 打开管理入口。
4. 私聊机器人发送 `/start`，选择该群，打开管理菜单。

找不到群组时，先确认你是该群管理员，且机器人已经在群内收到消息。完整说明见[权限与按钮操作](permissions.md)。

## 4. 完成第一次防护配置

1. 进入 **智能反垃圾 → 仅观察内容过滤**，开启全局观察。
2. 在 **违禁词 → 分类词库** 中选择适合本群的分类，先选仅观察。
3. 按需要配置[个人防刷屏](../moderation/anti-flood.md)和[跨账号重复消息](../cross-account-flood.md)。
4. 用“测试文字规则”和[防护记录](../operations/statistics.md)检查正常聊天和广告样本。
5. 调整后，将目标分类/功能切为执行处罚或自动清理，并关闭全局观察。

::: warning 不要用管理员账号验证自动删帖
管理员豁免自动内容过滤。文字测试入口可预览规则；验证实际删帖时，应在测试群使用普通成员账号，并确认机器人权限。
:::

## 5. 日常使用

| 操作 | 入口 |
| --- | --- |
| 调整群功能 | 私聊 `/start` → 选择群组 |
| 切换中文 / English | 私聊 `/language`；群设置中的“群内提示语言”单独调整群语言 |
| 成员签到、兑换、报名 | 群内 `/menu` → 成员服务 |
| 打开服务器菜单 | SSH 终端输入 `gg` 或 `GG` |
| 更新程序 | SSH 终端输入 `gg update`，更新前自动备份 |

下一步：[全部功能导航](features.md) · [常见问题](faq.md)
