# 群内命令与功能用法

优先私聊机器人 `/start` 使用按钮设置；本页供需要群内命令的管理员和成员查阅。各群数据独立，管理命令会核验真实管理员身份。防护操作见 [按钮指南](button-management.md)、[词库指南](word-library.md) 和 [跨账号防刷屏](cross-account-flood.md)。

## 命令速查

| 场景 | 命令 |
| --- | --- |
| 查看管理 | /panel、/settings、/check、/id、/stats、/statweek、/top7 |
| 界面语言 | 私聊 /language；群内 /language 提供私聊入口，详见[语言切换](guide/languages.md) |
| 外链/刷屏/转发 | /links on、/links off、/flood on、/forward on |
| 新人验证 | /captcha on（仅超群） |
| 其他机器人入群拦截 | /bots on、/bots off（默认关闭） |
| 欢迎语 | /welcome on、/setwelcome 欢迎 {name}！ |
| 违禁词 | /addword 广告词、/delword 广告词、/words |
| 违禁词批量处理 | /addwords 词1,词2,词3、/delwords 词1,词2 |
| 违禁词测试和例外 | /wordtest 文字、/wordexcept 违禁词 \| 完整允许文本 |
| 高级反垃圾 | /spam on、/wordloose on、/mediaflood on、/guard |
| 可选自动禁言 | /automute on、/automute off（仅超群，默认关闭） |
| 新人昵称限制 | /nameguard on、/nameguard off、/guardset name_length_limit 40 |
| 精确调整阈值 | /guardset flood_limit 7（推荐私聊机器人通过按钮调整） |
| 域名白名单 | /allowdomain example.com、/deldomain example.com、/domains |
| 群规 | /setrules 请文明交流、/rules |
| 自动回复 | /addreply 帮助,教程 \| 请联系管理员、/delreply 帮助,教程、/replies |
| 静默封禁 | 回复违规消息，发送 /dban（删消息+封禁） |
| 其他处罚 | 回复用户消息 /warn、/unwarn、/mute 60、/unmute、/ban、/kick |
| 解除封禁 | /unban 数字用户ID |
| 日志 | /setlog 负数日志群ID、/setlog off |
| 定时公告 | /schedule 创建、/schedule list 查看、/schedule pause/del 编号 |
| 群员签到 | /checkin 每日签到、/points 查自己积分、/pointtop 查看榜单 |
| 积分管理 | /pointmode on、/pointrate 5、回复成员 /pointadd 20、/pointtake 20、/pointlog |
| 图片刷屏拦截 | /imageflood on、/imagelimit 3、/imagewindow 60 |
| 积分转赠 | 回复接收者发送 /transfer 20，再点击确认按钮 |
| 虚拟积分商城 | /shop、/buy 编号、/myorders |
| 商城管理 | /shopadd 名称 \| 单价 \| 库存 \| 说明、/shopremove 编号 |
| 订单核销/退款 | /shoporders、/shopdone 订单号、/shoprefund 订单号 |
| 免费抽奖 | /raffle 查看、/joinraffle 报名 |
| 免费抽奖管理 | /rafflestart 活动名称、/raffledraw、/rafflecancel、/rafflehistory |
| 图片 OCR | /ocr off、/ocr review、/ocr delete、/ocrconfidence 80 |

## 签到积分


出于安全考虑，**默认关闭**。群管理员先发送以下指令：

~~~text
/pointmode on
/pointrate 5
~~~

开启后，成员在群里发送 /checkin，每个北京时间自然日可以签到一次。基础奖励默认为 **5 积分**；连续第二天签到多 1 积分、第三天多 2 积分，连续奖励最多多 6 积分。中断后连续天数归零重算。

成员可发送 /points 查询自己的积分，或 /pointtop 查看本群前 10 名。管理员回复真实成员的消息发送 /pointadd 20 或 /pointtake 20 即可调整积分。积分调整成功时**不会在群里额外播报处罚式提示**，但会保存在本地积分流水中；/pointlog 可查看最近记录。

积分按群组、成员 ID 单独存储，暂停积分功能不会清空已有余额。单次管理员调整不超过 10000 分，不可扣成负数。积分只能用于同群虚拟积分转赠及管理员手动处理的群内虚拟奖励兑换，**没有提现、现金兑换、自动发货或支付功能**。

## 同群积分转赠

积分系统必须先由管理员通过 /pointmode on 开启。群成员**回复另一名成员的普通消息**，发送：

~~~text
/transfer 20
~~~

机器人生成带有「确认转账 / 取消」按钮的消息，仅发起人可点击。**5 分钟未确认则失效；确认前不会扣分。** 点击确认时会重新核实双方群成员身份、余额及转账编号，原子扣减发送人的积分并增加接收人的积分。每笔转赠有唯一记录，重复点击和消息重放不会重复扣除。每次最多转赠 1000 分，不能转给自己、机器人或其他群的成员。**积分转赠不是现金或加密货币转账。**

## 虚拟奖励积分商城

群管理员上架一项需要人工处理的群内虚拟奖励：

~~~text
/shopadd VIP群内头衔 | 30 | 5 | 管理员核实后手动设置群头衔
/shop
~~~

成员发送 /shop 查看库存和价格，用 /buy 商品编号 兑换，系统立即扣除积分并生成**待核销订单**；积分不足或库存售罄时不会扣分。管理员查看、核销和退款：

~~~text
/shoporders
/shopdone 1
/shoprefund 1
/shopremove 1
~~~

**/shopdone 只记录“已经人工完成发放”**，不会自动赋予群成员权限。/shoprefund 对尚未核销的订单退还积分并恢复库存；已核销及已退款的订单不能重复退款。每群最多 30 个同时上架奖励。商城不处理人民币、现实商品付款或自动发货。

## 免费群抽奖

~~~text
# 管理员创建
/rafflestart 周末免费群内小活动
# 成员免费报名，一人一次
/joinraffle
# 管理员开奖或取消
/raffledraw
/rafflecancel
/rafflehistory
~~~

每群同一时间只有一个开放活动。开奖使用 Python secrets 随机选择一名中奖成员，结果保存到本地 SQLite，**不提供重抽已开奖活动的命令**。管理员需自行核实中奖者资格和履行活动承诺。收到成员退群事件后，会取消其在未开奖活动中的报名。本功能不收取积分、现金，也不提供投注或现金兑奖。

## 定时公告


所有计划时间都以配置文件中的 **TZ=Asia/Shanghai** 为准（默认北京时间）。群管理员可以直接在群内发送：

~~~text
/schedule daily 09:00 | 早安通知 | 大家早上好，欢迎交流！
/schedule weekly mon 19:30 | 每周提醒 | 请大家遵守群规。
/schedule once 2027-01-20 15:00 | 活动通知 | 今天下午开始活动。
/schedule list
/schedule pause 1
/schedule resume 1
/schedule del 1
~~~

也可以私聊机器人 /start → 选择群组 → 「📅 定时公告」，通过按钮查看任务详情、暂停/恢复、确认删除。每天/每周固定发送的时间可能相对设置时间晚几十秒。最多保存 20 条任务/群。

如果 Telegram API 暂时不可用，会按 1、2、4 分钟等间隔退避重试。NAS 离线太久后，**超过原计划 6 小时**的消息会跳过，而不是突然在群内集中补发。已失去管理员身份的任务创建者，其任务会暂停。机器人在 Telegram 成功接收发送请求、但尚未写入数据库时异常退出，极少数情况下可能重复发送，因此不适合作为交易、账单等严格一次性通知。

活跃排行只统计未被拦截的普通文字/说明消息，不记录消息正文；最多保存最近约 30 天按日统计的用户 ID 和消息数，默认只允许群管理员查询排行。

## 自动回复：多关键词、按钮与媒体

管理员可以在群内发送：

~~~text
/addreply 帮助,教程,使用说明 | 请先阅读群规与帮助文档。
/addreply 官网,网站 | 访问下方按钮 | 官网 - https://example.com
/addreply 电报,群链接 | 点击以下按钮 | Telegram - https://t.me/example && 帮助 - https://example.com/help
/replies
/delreply 帮助,教程,使用说明
~~~

- 单条规则支持 1-8 个关键词，以逗号、顿号或分号分隔；匹配任何一个关键词即触发。删除时填写**同一组关键词**，顺序可以不同。
- 按钮写在第二个竖线后面，格式为「按钮文字 - HTTPS网址」，多个按钮通过 && 分隔。最多 3 个按钮，不允许明文 HTTP、账号密码或脚本 URL。
- 如需发送媒体：管理员先找到群内已有的图片、视频、动图或文件，**回复那条媒体消息**，发送 /addreply 关键词 | 文字说明；机器人将保存该媒体的 Telegram file_id，下次匹配时复用文件并附带说明。媒体与按钮可以同时使用。
- 所有自动回复规则按群组独立保存，并且有每位成员每条规则约 60 秒的触发冷却。旧版纯文字规则在升级后仍可使用。媒体功能需要机器人能读取被回复的消息，且复用的 file_id 仍然有效。

**设置独立日志群**：创建私人群/频道，把机器人设为管理员；自己也需要是该群管理员。在日志群发送 /id 查看负数 ID，再回原群发送 /setlog -100...。禁止把原群直接设为日志目标，避免向所有人公开处罚日志。管理员自己发送的管理命令仍保留在群内。

群内危险命令与私聊设置按钮都会重新校验真实 Telegram 管理员身份，群友不能通过伪造按钮或命令越权。
