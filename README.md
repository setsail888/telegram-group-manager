# 群卫士 GroupGuard｜Telegram 中文群管理机器人

这是一个**全新独立开发**的自托管机器人，参考 We Group Bot 的公开功能设计，不包含第三方私有源码，也**不是** @WeGroupCloneBot 的官方克隆。当前为可运行、可扩展的 **MVP 第四轮迭代版**，保留前几轮的 SQLite 数据及功能。

## 🚀 云服务器一键部署（Debian / Ubuntu）

不需要域名、SSL、网页服务器或开放端口。**GitHub 仓库已公开**：全新 Linux 云服务器无需注册或登录 GitHub，可自动安装缺失的 curl、Git 和 Docker Compose，隐藏输入 Bot Token 后构建/启动容器，并通过中文菜单进行更新、备份、启停、OCR 和网页看板管理。

在服务器 SSH 终端**直接粘贴下面整段命令**（不要在 Windows PowerShell 本地执行）：

~~~bash
bash -c 'set -euo pipefail; command -v curl >/dev/null 2>&1 || { if [ "$(id -u)" -eq 0 ]; then apt-get update && apt-get install -y curl ca-certificates; else sudo apt-get update && sudo apt-get install -y curl ca-certificates; fi; }; curl -fsSL https://raw.githubusercontent.com/slobys/telegram-group-manager/main/scripts/server-bootstrap.sh | bash'
~~~

**无需 GitHub 登录或授权码。** 上面的命令从公开仓库下载引导脚本，并克隆项目；首次仍需自己提供 Telegram BotFather Token，输入时不会显示在终端。建议先查看 [公开的启动脚本](scripts/server-bootstrap.sh) 再执行网络脚本。**请使用独立、可信的云服务器，保护 SSH 与 Docker 管理权限。**

成功后，在 SSH 中随时调出中文菜单：

~~~bash
bash ~/groupguard/scripts/server-manager.sh
~~~

**如果之前安装时一直重复显示“未发现 Docker”**：先按 Ctrl+C 退出旧菜单，再通过 SSH 执行下面三行恢复安装（无需清空目录，不会覆盖 .env 或数据库）：

~~~bash
cd ~/groupguard
git pull --ff-only origin main
bash scripts/server-manager.sh install
~~~

新版 Docker 安装会显示分步骤执行和真正的 apt/curl 错误；失败会立即停止并显示退出码。请勿反复输入菜单序号 1，也不要为了排查而卸载服务器已有的 Docker。进一步排查详见 [Docker 安装失败](docs/server-one-click.md#docker-安装失败及重复提示)。

安装目录默认为 **~/groupguard**；数据库在 Docker 命名卷；备份默认在 **~/groupguard-backups**。**卸载默认保留数据，删除积分数据库必须再输入完整二次确认口令。**

完整安装说明、BotFather 设置、日志排障和手动克隆方式见 [服务器一键部署指南](docs/server-one-click.md)。公开仓库的源代码不包含你服务器里的真实 .env、Bot Token 和数据库；**仍然不要把密钥上传到 GitHub**。

## 已实现的功能

- 🇨🇳 中文私聊按钮管理：/start 自动列出你有管理员身份的群，按群单独开关。
- 🛡️ 自动清理：文字/图片说明中的违禁词、普通链接、隐藏文字链接、域名；支持域名白名单、转发拦截、短时间刷屏和重复消息拦截。
- 🧩 新人验证：超群新成员可按按钮回答数学题，默认关闭；默认 5 分钟超时，届时尽力移出并允许重新加入；机器人离线时不会自动提前解除限制。
- 🤖 可选拦截新加入的第三方机器人，**默认关闭**；启用后也会拦截管理员邀请进来的其他机器人。
- ⚖️ 管理员回复处罚：警告、撤销警告、禁言、解禁、封禁、踢出、解除封禁和 /dban 静默删广告并封禁。
- 📅 **定时公告**：单次、每日和每周任务，独立群组设置、私聊按钮查看/暂停/删除，机器人重启后任务仍在。
- 🏆 **群组分析**：当天与最近 7 天统计、活跃用户排行（显示 Telegram 数字 ID），详细活跃记录仅保留 30 天。
- 💬 **自动回复增强**：一个回复支持 1-8 个关键词、最多 3 个 HTTPS 链接按钮；也可复用管理员回复的图片、视频、动画或文件（Telegram file_id）。
- 🪙 **群签到积分**：可选开启的每日签到、连续签到奖励、排行榜、管理员加减积分、永久流水审计；多群独立、不允许负积分。
- 🔄 **确认后转赠积分**：回复成员发起转赠，5 分钟内须本人点击确认；每次最多 1000 分，跨群/重复点击不能多扣。
- 🎁 **虚拟积分商城**：管理员上架虚拟奖励与库存，群员通过群内积分兑换，后台有待核销订单、手动核销和退款，全部留有积分流水。
- 🎉 **免费群活动抽奖**：无需现金或积分报名，每人限报一次，随机选择一名中奖者并保存结果，不支持付费报名与重抽。
- 🖼️ **重复图片拦截**：可选开启，针对同一成员短时间内重复发送完全相同的 Telegram 图片进行清理（默认关闭）。
- 🔎 **本地图片 OCR**：可选安装 Tesseract 中文识别，通过长词命中规则先审查到私人日志；管理员可明确选择高可信自动删除，默认完全关闭。
- 🖥️ **中文只读网页看板**：独立访问密钥，查看群组统计、定时公告、积分榜和功能开关；Docker profile 可选启动，默认只允许 NAS 本机访问。
- 📝 群规、欢迎语、关键词自动回复、今日消息/删除/加入/处罚统计。
- 🔒 默认不在群内公开播报成功的处罚；**不自动删除管理员的管理命令**。可把日志发送到管理员共同管理的另一私密群/频道。
- 💾 SQLite 持久保存各群配置和验证状态；Bot API 长轮询，**不需要公网 IP、域名、SSL 证书或入站端口**。

> 注意：新商城和抽奖是独立实现的简化版，并非其他机器人的私有功能复刻。OCR **不识别二维码或图片是否具有广告意图**，只识别部分可见文字，可能漏判或误判；定时消息仍为纯文本。经过本地测试不等于已完成真实 Telegram 群实测。

## NAS 服务器要求

不使用 OCR 时建议最少 **1 vCPU、512 MB 可用内存、1 GB 可用磁盘空间**；需要 Docker + Docker Compose v2、NAS 能通过 HTTPS 访问 Telegram Bot API。没有额外的 Python 包依赖。启用 OCR 建议 **2 vCPU、2 GB 内存**，可选镜像需安装 Tesseract 中文语言包。对几千人的群先观察 CPU、内存及繁忙时段。

## 安装与首次部署

GitHub 项目地址：https://github.com/slobys/telegram-group-manager

**新服务器**（已经安装 Git、Docker + Docker Compose v2，也可以手动克隆）：

~~~bash
git clone https://github.com/slobys/telegram-group-manager.git
cd telegram-group-manager
cp config.sample .env
chmod 600 .env
nano .env
docker compose config -q
docker compose up -d --build groupbot
docker compose logs --tail=80 groupbot
~~~

在 .env 文件中填写自己通过 @BotFather 创建的 **BOT_TOKEN**；不需要公网域名、SSL 证书或额外开放服务器入站端口。**不要把 .env、数据库或 Token 上传到 GitHub**。如使用 NAS 上已有 WebCodex 工作区，可直接在原项目目录部署，无需重新克隆或覆盖已有数据。

NAS WebCodex 已开发项目默认位于：

    /home/naiyou/webcodex/projects/workspace/telegram-group-manager

1. 私聊 **@BotFather**，用 /newbot 创建自己的机器人并保存 Token。
2. 在 BotFather 的 Bot Settings → Group Privacy 中可以关闭 Privacy Mode；机器人为群管理员时 Telegram 通常也会给它完整群消息。若更改隐私设置后仍收不到消息，请移除再重新邀请机器人。
3. 将机器人加入目标群并**设置为管理员**，至少开启「删除消息」和「封禁用户/限制成员」权限。
4. 对于 NAS **已有本地源码但尚未配置 Token** 的情况，在 NAS SSH 中运行：

~~~bash
cd /home/naiyou/webcodex/projects/workspace/telegram-group-manager
cp config.sample .env
nano .env
~~~

将 BOT_TOKEN= 后面的占位内容替换为新 Token，保存后运行：

~~~bash
chmod 600 .env
docker compose up -d --build
docker compose logs --tail=50 groupbot
~~~

看到「机器人已启动」后，私聊你新建的机器人发送 /start，或在群里发送 /panel。数据默认放在 Docker 命名卷 bot_data 里。

如果 NAS 无法访问 Telegram，请先解决 NAS 到 api.telegram.org 的 HTTPS 连通性。Python urllib 支持标准 HTTPS_PROXY 环境变量（HTTP CONNECT 代理），但不原生支持 SOCKS5。不要把 Token 或代理密钥写入公开仓库与视频截图。

## 常用命令（群内）

| 场景 | 命令 |
| --- | --- |
| 查看管理 | /panel、/settings、/check、/id、/stats、/statweek、/top7 |
| 外链/刷屏/转发 | /links on、/links off、/flood on、/forward on |
| 新人验证 | /captcha on（仅超群） |
| 其他机器人入群拦截 | /bots on、/bots off（默认关闭） |
| 欢迎语 | /welcome on、/setwelcome 欢迎 {name}！ |
| 违禁词 | /addword 广告词、/delword 广告词、/words |
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

### 签到积分怎么用

出于安全考虑，**默认关闭**。群管理员先发送以下指令：

~~~text
/pointmode on
/pointrate 5
~~~

开启后，成员在群里发送 /checkin，每个北京时间自然日可以签到一次。基础奖励默认为 **5 积分**；连续第二天签到多 1 积分、第三天多 2 积分，连续奖励最多多 6 积分。中断后连续天数归零重算。

成员可发送 /points 查询自己的积分，或 /pointtop 查看本群前 10 名。管理员回复真实成员的消息发送 /pointadd 20 或 /pointtake 20 即可调整积分。积分调整成功时**不会在群里额外播报处罚式提示**，但会保存在本地积分流水中；/pointlog 可查看最近记录。

积分按群组、成员 ID 单独存储，暂停积分功能不会清空已有余额。单次管理员调整不超过 10000 分，不可扣成负数。积分只能用于同群虚拟积分转赠及管理员手动处理的群内虚拟奖励兑换，**没有提现、现金兑换、自动发货或支付功能**。

### 新功能一：同群积分转赠

积分系统必须先由管理员通过 /pointmode on 开启。群成员**回复另一名成员的普通消息**，发送：

~~~text
/transfer 20
~~~

机器人生成带有「确认转账 / 取消」按钮的消息，仅发起人可点击。**5 分钟未确认则失效；确认前不会扣分。** 点击确认时会重新核实双方群成员身份、余额及转账编号，原子扣减发送人的积分并增加接收人的积分。每笔转赠有唯一记录，重复点击和消息重放不会重复扣除。每次最多转赠 1000 分，不能转给自己、机器人或其他群的成员。**积分转赠不是现金或加密货币转账。**

### 新功能二：虚拟奖励积分商城

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

### 新功能三：免费群抽奖（不收费、不消耗积分）

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

### 新功能四：可选本地图片 OCR（默认完全关闭）

**普通版不安装 OCR 引擎，禁用时也不会下载群里的图片做 OCR。** 如需在测试群启用，在 NAS 项目已有的 .env 中追加或修改：

~~~text
WITH_OCR=1
OCR_ENABLED=1
~~~

然后重新构建带中文识别语言包的镜像：

~~~bash
cd /home/naiyou/webcodex/projects/workspace/telegram-group-manager
docker compose up -d --build groupbot
~~~

先按已有说明设置独立私人日志群，使用 /setlog 负数群ID。再添加**至少四字、具有明显区分度的关键词**，在测试群开启只记录模式：

~~~text
/addword 私聊加微信
/ocr review
/ocrconfidence 80
~~~

**review 模式仅向私人管理日志报告疑似图片广告，不会自动删除**，也不会持久保存图片或 OCR 识别全文。确认在测试群误判风险可接受以后，管理员才可以明确发送 /ocr delete，允许在命中词组且可信度达到门槛时自动删除；会在删除前再次核验用户并豁免管理员。随时可用 /ocr off 关闭。建议在正式群长期优先使用 review。

OCR 只处理 Telegram 照片，文件限制 3 MB，单工作线程及 8 个候选任务上限，单张 OCR 最多约 8 秒；处理队列满或识别失败时直接跳过，不保证扫描所有图片。它**不识别二维码、图像场景或广告意图**，重新编码的图片和背景复杂的文字可能漏判。识别完全使用本地 Tesseract，不会上传到第三方 OCR 供应商；仍需经 Telegram Bot API 下载被审核图片。建议启用 OCR 的 NAS 至少有 2 vCPU、2 GB 可用内存。

### 重复图片刷屏防护

~~~text
/imageflood on
/imagelimit 3
/imagewindow 60
~~~

示例：同一群组的同一个普通成员在 60 秒内第三次发送 Telegram 标识为同一个文件的照片时，删除第三条及同窗口后续重复图片（机器人必须有删除消息权限）。**默认关闭**。仅使用 Telegram 图片的 file_unique_id 字段，不下载原始图片，也不识别图片内容。不同成员、不同群的相同照片不会互相连坐；管理员的消息始终豁免自动清理。请先在测试群体验。

### 中文网页看板（可选，默认不启动）

网页用于查看统计、积分商城待核销订单、免费抽奖记录及 OCR 命中数量，不提供封禁成员、删除消息、积分扣除或修改机器人设置等操作。面板数据库卷以只读方式挂载。面板密钥与 Telegram BotFather Token **必须分开**。在 NAS SSH 中执行：

~~~bash
cd /home/naiyou/webcodex/projects/workspace/telegram-group-manager
python3 -c "import secrets; print(secrets.token_hex(32))"
nano .env
~~~

在现有 .env 文件**追加或修改** DASHBOARD_TOKEN，填写刚才生成的随机字符串；保留原有 BOT_TOKEN，勿在公开截图显示任何密钥。保存后：

~~~bash
chmod 600 .env
docker compose --profile dashboard up -d --build dashboard
docker compose --profile dashboard ps
~~~

**默认只绑定 NAS 的 127.0.0.1:8765 端口**，不要改成 0.0.0.0 或开放公网端口。在自己的电脑上建立 SSH 隧道（保持终端打开，账号和 NAS_IP 按实际填写）：

~~~bash
ssh -N -L 8765:127.0.0.1:8765 naiyou@NAS_IP
~~~

然后在本地浏览器打开 http://127.0.0.1:8765，输入 **DASHBOARD_TOKEN** 登录。会话默认最多 12 小时，服务端也会验证过期时间。网页有拒绝跨域来源 Host、禁用脚本、只读数据库、登录失败限速和基本安全响应头等防护。

网页访问密钥拥有**查看该机器人所有已登记群组数据**的权限，不按 Telegram 单个群管理员身份划分；因此只提供给可信 NAS 管理员，不应放在公共网站。当前基于本地 HTTP + SSH 隧道，不是面向公网的 HTTPS 管理系统。

关闭网页服务：

~~~bash
docker compose --profile dashboard stop dashboard
~~~

不使用 --profile dashboard 直接启动 groupbot 时，并不会启动网页服务。

### 定时公告怎么用

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

### 自动回复：多关键词、按钮与媒体

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

## 更新、停止与备份

~~~bash
# 新代码更新后重新构建启动
docker compose up -d --build
docker compose ps
docker compose logs -f --tail=100 groupbot
# 停止服务，保留数据库
docker compose down
~~~

**第四阶段升级前建议先备份数据库**，尤其是已经存在积分流水的群。备份数据前先停止机器人，避免复制 SQLite 的不完整写入：

~~~bash
docker compose stop groupbot
docker compose cp groupbot:/app/data/bot.sqlite3 ./groupbot-backup.sqlite3
docker compose start groupbot
~~~

卸载时先 docker compose down，再根据实际卷名称手动删除命名卷。**不要随意使用 docker compose down -v**，这会把所有群设置和统计数据一起删除。

## 权限与限制

- 默认：链接拦截开启，防刷屏开启，转发拦截关闭，新人验证关闭，欢迎语开启。先在测试群验证效果，再替换现有管理机器人。
- 刷屏默认阈值：10 秒内超过 7 条，或 45 秒内连续 3 次相同文本。
- 机器人缺少群管理员权限、Telegram API 无法访问或自身离线时，过滤与验证超时清退可能暂时无法执行；超时处理会在恢复运行后尝试补处理。
- 数学四选一只是基础验证，不是高强度 CAPTCHA，也无法识别所有图片广告。
- 启用验证前务必在测试超群试用，并确认机器人有「限制成员」权限。超时移出依赖机器人在线或恢复后的补处理；如果机器人离线，新人会保持受限直到恢复处理，不会自行解禁。
- 各群设置、警告和统计保存在本机 SQLite。真实 Token 未预置，也不会自动加入或操作你的真实群组。
- 本地逻辑测试无需 Token：

~~~bash
python3 -m unittest discover -s tests -v
~~~
