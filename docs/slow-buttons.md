# GroupGuard：Telegram 管理按钮点击很慢，如何排查？

如果私聊点击「防刷屏、违禁词、返回」等按钮要等待多秒，通常与 **Telegram Bot API 网络请求耗时叠加**有关，不是 Telegram 按钮本身只能这么慢。

## 本项目已优化的代码问题

- **移除重复权限查询**：每次管理按钮入口仍必须通过 Telegram 最新的 getChatMember 核实操作人是否为该群管理员；核实后同一次点击进入页面，不再重复发第二次同样的请求。下一次点击仍会重新检查身份，不能通过缓存旧权限绕过验证。
- **限制非轮询请求卡住整个机器人**：getChatMember、answerCallbackQuery、editMessageText 等普通请求超时时间从 35 秒降为 15 秒；getUpdates 长轮询仍保留独立超时。
- **避免重复发菜单**：相同页面重复编辑时，Telegram 的 message is not modified 会被正确当成正常结果，不再发送另一条重复菜单。
- **优先展示新页面**：有独立管理日志群时，原来部分按钮先向日志群发记录再更新页面。现在先更新面板，再继续发送审计日志；不会牺牲日志功能。
- **加入耗时日志**：普通 Telegram API 请求或完整按钮处理超过 2.5 秒，会在服务日志记录耗时（仅方法名/秒数，不包含 Bot Token、按钮参数、消息或用户 ID）。getUpdates 正常等待 25 秒不算慢请求。

注意：这只能减少代码本身的重复等待。**如果服务器到 api.telegram.org 的单次 HTTPS 往返就需要数秒，用户仍会感到延迟**；这需要检查服务器出口网络、代理、DNS 等因素。

## 1. 在服务器更新并查看日志

~~~bash
bash ~/groupguard/scripts/server-manager.sh update
bash ~/groupguard/scripts/server-manager.sh logs-follow
~~~

保留日志窗口，再在 Telegram 私聊点击几个不同的功能按钮。如果日志显示类似：

~~~text
Telegram API 响应较慢: getChatMember 耗时 4.20 秒
Telegram 管理按钮处理耗时 7.10 秒；请检查上方的 API 响应慢记录及服务器网络
~~~

说明瓶颈发生在服务器访问 Telegram API 或处理中。上面是**示例日志**，不是你服务器的实测数字。

如果只有 editMessageText 请求很慢，重点查看 Telegram API 网络。如果日志中持续出现网络连接中断、HTTP 429 或频繁重试，也可能拖慢单线程消息队列。

## 2. 单独测量服务器连接 Telegram API 的速度

下面的命令在已运行的 **GroupGuard 容器**里调用一次安全的 getMe 查询，并显示耗时。Token 直接读取容器环境变量，**不会显示到屏幕或日志**。

~~~bash
docker exec groupguard-groupbot-1 python -c 'import os,time; from app.api import TelegramAPI; t=time.monotonic(); TelegramAPI(os.environ["BOT_TOKEN"], os.environ.get("API_BASE","https://api.telegram.org")).call("getMe"); print("Telegram getMe 耗时：%.2f 秒"%(time.monotonic()-t))'
~~~

如果你使用了非默认 Compose 项目名称，可先用以下命令查看真实容器名：

~~~bash
docker ps --format '{{.Names}}'
~~~

getMe 只能测**一次 API 请求的网络及处理时间**，不能代表完整面板操作的所有耗时。建议相隔几秒运行两三次，看是否经常出现明显波动。

另一种不需要 Token 的 HTTPS 连接检查：

~~~bash
curl -sS -o /dev/null --connect-timeout 5 --max-time 15 \
  -w 'HTTP=%{http_code} DNS=%{time_namelookup}s TCP=%{time_connect}s TLS=%{time_appconnect}s 首字节=%{time_starttransfer}s\n' \
  https://api.telegram.org/
~~~

这里的 HTTP 状态码不一定是 200，重点看 DNS、TCP、TLS、首字节耗时。

## 3. 查看容器资源

~~~bash
docker stats --no-stream groupguard-groupbot-1
docker logs --since 10m groupguard-groupbot-1 2>&1 | grep -E 'API 响应较慢|管理按钮处理耗时|Telegram 连接出现问题|未成功' || true
~~~

若 CPU、内存占用明显高，先观察是否开启了本地 OCR、是否有大量排队消息。OCR 是可选功能，不必为了排查管理面板而开启。

## 4. 不建议的排查做法

- **不要直接关闭管理员校验**：那会让已被撤销权限的人继续修改群设置。
- 不要反复将 POLL_TIMEOUT=25 改成 1，正常长轮询会在收到更新时立即返回，缩短此设置**不能替代优化每次 API 请求**。
- 不要把 .env、BOT_TOKEN、带 Token 的 Bot API URL 或服务器密码发到群聊或截图中。
- 不要同时用同一个 Bot Token 启动两套 GroupGuard；Telegram 的 409 Conflict 会干扰收取更新。

把**慢请求日志里的 API 方法及耗时数字**发给维护者，即可更准确区分是网络瓶颈还是机器人处理流程问题。
