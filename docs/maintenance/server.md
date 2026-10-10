# gg菜单、升级与备份

以下命令在安装机器人的**服务器 SSH 终端**执行。`gg` 和 `GG` 都可打开菜单，默认安装目录为 `~/groupguard`。

## 中文菜单

| 序号 | 功能 |
| --- | --- |
| 1 | 安装 / 修复部署 |
| 2 | 更新机器人：自动备份后拉取main、重建并启动 |
| 3 | 查看运行状态 |
| 4 | 查看最近日志 |
| 5 | 修改BotFather Token |
| 6 | 启动 / 停止 / 重启服务 |
| 7 | 卸载，默认保留数据 |
| 0 | 退出 |

主菜单不再列出OCR、看板与手动备份，它们保留命令入口。

## 常用命令

```bash
gg
gg status
gg logs
gg logs-follow
```

持续日志按Ctrl+C退出。Token变更用菜单5，按提示隐藏输入；更新代码不需要重新设置Token。

## 更新与备份

```bash
gg update
```

自动短暂停止机器人，将SQLite导出到 `~/groupguard-backups`，恢复旧容器后，快进拉取GitHub main，再重建启动。备份失败或源码存在本地改动时停止，不强制覆盖。

单独备份：

```bash
gg backup
```

备份目录权限700，文件600，位于仓库之外。备份保留群设置、积分、订单和活动等数据库内容；`.env` 中Token需另行安全保存。也应将重要备份复制到服务器之外，避免同盘故障。

备份文件不是一键恢复操作；恢复前需核对目标数据卷、当前服务和配置。现有脚本未提供 `gg restore`，不要直接覆盖运行中的数据库。

## 旧版没有gg / GG

重新执行[在线安装命令](../server-one-click.md#三一键安装复制一条命令)，已有安装会备份更新并注册入口。源码已更新时，可单独修复：

```bash
bash ~/groupguard/scripts/server-manager.sh shortcuts
```

自定义安装路径请替换；完整路径 `bash ~/groupguard/scripts/server-manager.sh` 仍可打开菜单。若遇安装循环，先看[专用恢复步骤](troubleshooting.md#容器running后安装不停重复)。

## 卸载

菜单7默认移除本项目容器、保留数据卷和源码。删除数据需输入完整确认短语；不可撤销前请备份。

不会卸载系统Docker或删除其他项目。OCR与看板分别见[OCR说明](../moderation/ocr.md)和[只读看板](dashboard.md)。
