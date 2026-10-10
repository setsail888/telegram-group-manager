# 群卫士 GroupGuard

自托管的 Telegram 中文群管理机器人。通过私聊按钮配置违禁词、反垃圾、防刷屏、新人验证和成员管理，也支持积分、自动回复、公告与免费抽奖。使用 SQLite 保存数据，通过长轮询连接 Telegram，无需域名或开放入站端口。

## 安装到服务器

使用 Debian / Ubuntu，通过 SSH 登录服务器后执行。需要 root 或 sudo 权限，并能访问 GitHub、Docker Hub 和 Telegram；缺少 `curl` 时先按 [部署指南](docs/server-one-click.md#三一键安装复制一条命令) 安装。

```bash
bash -o pipefail -c 'curl -fsSL https://raw.githubusercontent.com/slobys/telegram-group-manager/main/install.sh | bash'
```

安装器自动准备 Git、Docker 和 Compose，提示隐藏输入 BotFather Token，然后构建并启动机器人。已有安装再次执行这条命令，会先备份数据库，再更新 main 并补齐快捷命令。无需登录 GitHub；可先查看 [安装脚本](install.sh)。

安装目录默认 `~/groupguard`，备份默认 `~/groupguard-backups`。普通版建议至少 1 vCPU、512 MB 可用内存、1 GB 可用磁盘空间。NAS、手动部署和故障处理见 [服务器部署指南](docs/server-one-click.md)。

旧版若在容器显示 `Running` 后反复从头安装，按 [重复安装恢复步骤](docs/server-one-click.md#容器已-running却反复进入安装) 更新源码并补齐快捷命令。

## 打开菜单与更新

安装成功后，在任意目录输入以下命令打开中文菜单；`GG` 同样可用：

```bash
gg
```

| 序号 | 操作 |
| --- | --- |
| 1 | 安装 / 修复部署 |
| 2 | 更新机器人：先备份，再拉取 main、重建并启动 |
| 3 | 查看运行状态 |
| 4 | 查看最近日志 |
| 5 | 修改 BotFather Token |
| 6 | 启动 / 停止 / 重启服务 |
| 7 | 卸载，默认保留数据 |
| 0 | 退出 |

也可直接更新，无需进入菜单：

```bash
gg update
```

更新失败会显示原因；备份失败或源码有本地改动时停止更新。升级保留 `.env`、群配置、积分和订单。旧版还没有 `gg` 时，重新运行上面的安装命令即可；[更多维护命令](docs/server-one-click.md#五日常中文管理菜单) 放在部署指南中。

## 在 Telegram 中开始使用

1. 向 [@BotFather](https://t.me/BotFather) 创建机器人，将 Token 填入安装器
2. 把机器人加入测试群，授予删除消息和限制成员权限
3. 私聊机器人发送 `/start`，选择群组，通过按钮设置；群内 `/check` 可检查权限，`/panel` 可打开管理入口

新防护建议先观察，再启用处罚。**跨账号重复检测、分类词库和中文变体识别默认关闭**，更新代码不会替你开启。

| 功能 | 支持内容 |
| --- | --- |
| 违禁词 | 自定义规则、13类词库、繁简/定向同音/拆字、搜索、导入导出、精确例外 |
| 智能反垃圾 | 伪装链接、隐藏链接、批量提及、域名白名单、观察模式与防护记录 |
| 防刷屏 | 个人频率、重复文字/图片、媒体连发、跨账号同内容清理；警告/禁言/踢出/封禁/仅撤回 |
| 入群与成员 | 新人验证、集中入群保护、昵称限制、机器人拦截、手动成员管理 |
| 群运营 | 欢迎语、群规、自动回复、定时公告、统计与活跃排行 |
| 成员服务 | 签到积分、确认转赠、虚拟奖励兑换、订单核销退款、免费抽奖 |
| 可选扩展 | 本地图片 OCR、只读网页看板，配置方法见部署指南 |

内容过滤采用本地规则，可能误判或漏判；OCR 只识别部分文字，不识别广告意图。管理员豁免自动过滤。真实群权限、网络和图片质量仍需在测试群验证。

## 使用文档

| 文档 | 内容 |
| --- | --- |
| [服务器部署与维护](docs/server-one-click.md) | 安装、旧版升级、gg/GG、备份恢复准备、OCR、看板和排障 |
| [中文按钮操作](docs/button-management.md) | 管理员与成员各项功能入口、表单、预览确认 |
| [词库与中文变体](docs/word-library.md) | 启用分类、短词/同音识别、例外、导入格式及来源 |
| [跨账号防刷屏与惩罚](docs/cross-account-flood.md) | 检测阈值、清理范围、五种惩罚和时长 |
| [群内命令与功能用法](docs/commands.md) | 命令速查、积分、商城、抽奖、公告和自动回复 |
| [管理按钮延迟排查](docs/slow-buttons.md) | 权限请求、网络与服务器耗时诊断 |
| [功能检查与测试记录](docs/function-audit-2026-10-10.md) | 检查结果、修复范围和验证边界 |

Token 仅保存在服务器本地，请勿上传 `.env` 或数据库。卸载默认保留数据；删除数据需要额外确认。独立开发的项目，功能参考公开资料，不是其他机器人的官方版本。

## 本地开发验证

使用 Python 3.12 或更高版本；完整测试包含 Linux 部署脚本测试，请在 Linux / WSL 中运行：

```bash
python3 -m pip install -r requirements.txt
python3 -m unittest discover -s tests -v
```
