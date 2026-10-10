# 故障排查

先确认出问题的是服务器、机器人连接，还是某个群的设置。查看日志时隐藏Token、代理密码等敏感信息。

## 容器Running后安装不停重复

旧版root用户注册 `gg/GG` 时存在命令重名，导致反复进入安装；已修复。先Ctrl+C停止，然后在服务器执行：

```bash
cd ~/groupguard &&
git pull --ff-only origin main &&
bash scripts/server-manager.sh shortcuts &&
gg status
```

自定义安装路径替换第一行。这只更新源码、修复入口并查看状态，不重建容器或改写Token、数据库。Git失败时先处理原因，不要强制覆盖本地源码。

## 机器人没有响应

```bash
gg status
gg logs
```

| 日志或现象 | 下一步 |
| --- | --- |
| 没有运行中的groupbot | 查看构建错误，再通过菜单启动 |
| Token相关错误 | 用菜单5重新设置有效Token |
| Telegram网络超时 | 检查服务器DNS、HTTPS出口与代理 |
| 409 Conflict | 同一个Token在别处轮询，停止重复实例 |
| 只有某群无响应 | 检查机器人是否在群内、群类型、消息读取及管理权限 |

容器Running不等于已正常连接Telegram，需结合日志与实际测试消息判断。

## 更新失败

- **数据库备份失败**：查看磁盘、数据卷和原容器状态，先解决备份问题。
- **有本地源码改动**：先保存并处理改动，更新器不会强制覆盖。
- **无法快进**：检查分支状态，不要直接重置或删除配置来重装。
- **GitHub访问失败**：检查GitHub及raw.githubusercontent.com的DNS和HTTPS出站连接。

## Docker尚未安装成功

如果提示“未发现Docker”且有apt或下载错误，和“容器Running后重复”不是同一个问题。查看[Docker安装失败说明](../server-one-click.md#docker-安装失败及重复提示)，解决报错后再安装。

## 菜单或管理按钮很慢

重新发送 `/start` 获取最新菜单，检查网络和容器资源。详细测速及耗时日志见[管理按钮延迟排查](../slow-buttons.md)。不要把Token直接拼进可公开的测速命令。

## 命中了却没删消息

检查目标账号是不是管理员、总开关是否开启、分类是否仅观察、全局观察是否开启，以及删除权限。防护记录可区分观察、删除失败、批量请求和成员处罚结果。

## OCR或看板无法使用

OCR需服务器引擎和群模式同时开启，见[OCR教程](../moderation/ocr.md)。看板只绑定服务器本地，需SSH隧道及独立密钥，见[看板教程](dashboard.md)。
