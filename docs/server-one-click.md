# GroupGuard｜云服务器一键部署详细教程

适用于 **Debian 12/13、Ubuntu 22.04/24.04 等由 Docker 官方 apt 软件源支持的版本**，支持 amd64 和 arm64。项目源码位于 GitHub **公开仓库** [slobys/telegram-group-manager](https://github.com/slobys/telegram-group-manager)。

> 重要：仓库已公开，**无需安装 GitHub CLI，也无需登录 GitHub 账号**。部署过程中只需准备 Telegram BotFather Token，安装器将使用本地隐藏输入，并保存到权限为 600 的 .env。

## 一、准备工作

- 一台能访问 GitHub、Docker Hub 和 Telegram API 的 Linux 云服务器，带 SSH 登录及 root 或 sudo 权限。
- 推荐普通版 **1 vCPU、512 MB 可用内存、1 GB 可用硬盘**；OCR 版建议 **2 vCPU、2 GB 内存**，构建镜像还需额外临时空间。
- 无需 GitHub 账号，但服务器必须能够通过 HTTPS 访问 github.com、raw.githubusercontent.com。
- 一枚通过 Telegram 官方 @BotFather 创建的 Bot Token。

**仅需开放 SSH 管理端口**（通常为 22，具体依服务器配置）。不要为机器人额外开放 80、443 或 8765；Telegram 长轮询不需要入站端口。

## 二、连接服务器

Windows PowerShell 示例：

~~~powershell
ssh root@你的服务器IP
~~~

如果服务器不允许 root SSH，使用有 sudo 权限的普通账号：

~~~powershell
ssh ubuntu@你的服务器IP
~~~

下面命令**在服务器 SSH 会话内部执行**，不是在 Windows 电脑本地执行。

## 三、一键安装：复制一条命令

~~~bash
bash -c 'set -euo pipefail; command -v curl >/dev/null 2>&1 || { if [ "$(id -u)" -eq 0 ]; then apt-get update && apt-get install -y curl ca-certificates; else sudo apt-get update && sudo apt-get install -y curl ca-certificates; fi; }; curl -fsSL https://raw.githubusercontent.com/slobys/telegram-group-manager/main/scripts/server-bootstrap.sh | bash'
~~~

安装器依次完成：

1. 如果缺少 curl，会自动通过 apt 安装 curl 和证书；启动脚本还会检测并安装缺失的 Git。
2. 直接从公开 GitHub 仓库下载启动脚本，无需 GitHub 登录或授权码。
3. 使用 git clone 克隆项目到当前 SSH 用户的 **~/groupguard**（已有同名安装时检查远程地址，不会覆盖其他项目）。
4. 若没有 Docker，按 Docker 官方 apt 仓库流程安装 Docker Engine、Buildx、Compose V2；若已有 Docker 和 Compose V2，则保留现有环境。已有 Docker 但缺少 Compose V2 时**不会替换或卸载原有 Docker**。
5. 首次生成权限为 600 的 ~/groupguard/.env，隐藏输入 BotFather Token 并检查格式。
6. 校验 Compose 配置，构建容器，启动 groupbot 并显示运行状态。

默认是**轻量普通版**：不会安装 OCR、不启动网页看板，也不会自动改防火墙和其他 Docker 服务。

### GitHub 访问失败怎么办？

不需要 GitHub 授权。确认服务器能够访问 GitHub 和公开安装脚本：

~~~bash
curl -I https://raw.githubusercontent.com/slobys/telegram-group-manager/main/scripts/server-bootstrap.sh
git ls-remote https://github.com/slobys/telegram-group-manager.git HEAD
~~~

如果无法访问，检查服务器的 DNS、HTTPS 出站连接、防火墙和代理，或稍后重试。也可以先下载并人工检查脚本内容，再执行；不要在不信任的服务器上运行远程脚本。

### 不想运行在线引导脚本？可手动克隆后运行

~~~bash
git clone https://github.com/slobys/telegram-group-manager.git ~/groupguard
cd ~/groupguard
bash install.sh
~~~

这与一键安装入口调用的是同一管理器。

## 四、BotFather 设置

在 Telegram 中打开 [官方 @BotFather](https://t.me/BotFather)：

1. 输入 /newbot 创建一个**独立的新机器人**，设置显示名称和以 bot 结尾的用户名。
2. 获得 Bot Token，只粘贴到安装器的**隐藏输入框**中，不要发到群聊、视频截图和 GitHub。
3. 使用 /setjoingroups 允许加入群组。
4. 使用 /setprivacy 选择该机器人并设为 Disable，让机器人能处理普通群消息。管理员机器人通常能读取更多群消息，修改后可能需要重新邀请才生效。

先将机器人添加到**私人测试群**并提升为管理员。建议仅授予「删除消息」「限制/封禁用户」，不要授予不必要的添加管理员等权限。

测试群里依次发送：

~~~text
/check
/panel
~~~

随后私聊**新机器人**发送 /start 打开中文私聊管理菜单。

## 五、日常中文管理菜单

在服务器 SSH 中执行：

~~~bash
bash ~/groupguard/scripts/server-manager.sh
~~~

菜单包含安装/修复、GitHub 更新（先备份）、查看状态、日志、修改 Bot Token、本地 OCR、网页看板、备份、启动/停止/重启和卸载。

也支持直接运行：

~~~bash
bash ~/groupguard/scripts/server-manager.sh status
bash ~/groupguard/scripts/server-manager.sh logs
bash ~/groupguard/scripts/server-manager.sh logs-follow
bash ~/groupguard/scripts/server-manager.sh backup
bash ~/groupguard/scripts/server-manager.sh update
~~~

持续日志模式按 Ctrl+C 退出。

## 六、网页管理面板（可选）

菜单输入 **7 → 1**，第一次会为看板生成一枚独立随机密钥，仅显示一次。建议保存到密码管理器，不要当成 BotFather Token。

看板只绑定服务器 **127.0.0.1:8765**，没有对公网开放。用 Windows PowerShell 建立 SSH 隧道：

~~~powershell
ssh -N -L 8765:127.0.0.1:8765 ubuntu@你的服务器IP
~~~

把 ubuntu 替换成实际 SSH 用户，然后在 Windows 浏览器打开 http://127.0.0.1:8765，输入该看板独立密钥。网页只读，可查看群统计、积分、订单、定时任务与免费抽奖，不能编辑规则或封禁成员。

## 七、图片 OCR（可选）

菜单输入 **6 → 1**，安装器会修改 WITH_OCR=1 和 OCR_ENABLED=1，并构建 Tesseract 中文 OCR 镜像。普通版和 OCR 版都不需要域名。

先在 Telegram 中创建私人管理日志群，让机器人拥有发消息权限，并回到测试群执行：

~~~text
/setlog -100实际日志群ID
/addword 私聊加微信
/ocr review
/ocrconfidence 80
~~~

**review 只向日志群报告疑似命中，不会自动删除图片。** 尝试足够多真实图片、确认误判率后，才考虑 /ocr delete。随时可以 /ocr off，也可以通过菜单 **6 → 2** 恢复轻量版镜像。

注意：OCR 识别图片中部分文字，不识别二维码或广告意图；可能跳过大图片或队列拥塞的消息。

## 八、升级与备份

在服务器执行：

~~~bash
bash ~/groupguard/scripts/server-manager.sh update
~~~

安装器会先暂停机器人、将 SQLite 数据库导出到 **~/groupguard-backups**，成功后重新启动原容器，再以 fast-forward 方式拉取公开 GitHub 仓库 main 分支的最新代码，重新构建并启动。**更新同样无需 GitHub 登录。**

备份目录默认权限为 700，备份文件权限为 600，放在仓库目录之外。升级保留本地 .env、Docker 命名卷、积分、订单和群设置；如果源码有本地改动，会停止更新，**不会强制覆盖**。如果自动备份失败，则**不会继续升级**。

也可以单独执行：

~~~bash
bash ~/groupguard/scripts/server-manager.sh backup
~~~

> 如果系统级别的 Docker 或服务器磁盘出现故障，仅本机备份不够。重要数据库建议另行复制到 NAS 或其他安全位置。

## 九、卸载

运行管理菜单，选择 **10**。

- 选择 **1**：删除 GroupGuard 容器，**保留** Docker 命名卷、源码、.env 和备份。
- 选择 **2**：危险操作。必须再手动输入完整确认短语 DELETE GROUPGUARD DATA，才删除该项目 Docker 命名卷。

卸载**不会**卸载服务器 Docker Engine、Git、curl 或清理其他 Docker 项目。

## 十、故障检查

### Docker 安装失败及重复提示

旧版菜单如果反复显示“未发现 Docker，准备按照 Docker 官方 apt 软件源进行安装”，说明 Docker 没有安装成功。旧版使用的 apt-get -qq 不会显示正常安装进度，而且菜单对失败的处理不够明确。**先按 Ctrl+C 停止，升级修复后的脚本再运行**：

~~~bash
cd ~/groupguard
git pull --ff-only origin main
bash scripts/server-manager.sh install
~~~

修复后会依次显示 **1/7 更新系统 apt → 2/7 安装 HTTPS/证书 → 3/7 创建密钥目录 → 4/7 Docker GPG 密钥 → 5/7 Docker 软件源 → 6/7 更新 Docker apt 索引 → 7/7 安装 Docker Engine/Compose**。失败时显示错误步骤及退出码，并立即停止；不要反复进入菜单。

如果再次失败，复制以下自检命令的输出（**请先隐藏服务器 IP、代理密码等敏感内容**）：

~~~bash
cat /etc/os-release
uname -m
command -v docker || true
sudo apt-get update
curl -I --connect-timeout 10 --max-time 20 https://download.docker.com/
~~~

对于已经安装 Docker CLI 但缺少 Compose V2 的服务器，安装器会主动停止，不会自动替换已有 Docker。Docker 官方安装页：[Ubuntu](https://docs.docker.com/engine/install/ubuntu/) / [Debian](https://docs.docker.com/engine/install/debian/)。

如果服务器上 Docker 官方源配置已经损坏，请先查看报错及 /etc/apt/sources.list.d/docker.sources，不要直接删除其他正在使用的 Docker 数据或强制运行 apt purge。

| 症状 | 可能原因与排查 |
|---|---|
| GitHub 下载失败 | 检查公开仓库 URL、DNS、HTTPS 出站连接，以及服务器能否访问 github.com 与 raw.githubusercontent.com |
| Docker 反复显示安装开始 | 旧脚本未中止 apt 错误；按 Ctrl+C 退出，按上面命令拉取修复版并查看具体失败步骤 |
| Token 验证失败 | Token 格式不对；重新运行菜单 5 输入，输入时内容不会显示 |
| Docker 构建失败 | 服务器无法访问 Docker Hub、磁盘空间不足或 Docker 官方 apt 仓库暂不可用 |
| Telegram getMe 失败 | 服务器出口无法访问 api.telegram.org 或 Bot Token 错误 |
| 群广告未被删除 | 检查机器人管理员权限、Telegram privacy 设置，及群内 /links on |
| 菜单不显示群 | 先邀请机器人加入测试群，发送 /panel，再私聊 /start |
| 409 Conflict | 同一个 Bot Token 在其他进程中轮询；每个 Token 只启动一个 groupbot |
| OCR 不生效 | 菜单 6 → 1、群日志 /setlog、/ocr review、关键词长度与语言包 |
| 已有 Docker 但缺少 Compose | 为保护原有项目，安装器会停止；按 [Docker 官方文档](https://docs.docker.com/engine/install/) 补充 Compose V2 |

## 安全原则

安装器不做以下操作：不要求公开 SSH 密钥、不上传 Bot Token、不打开新的公网端口、不自动删除其他容器、不覆盖其他 Git 仓库、不关闭已有防火墙；不进行自动提现、现实商品支付或收费抽奖。公开源码不意味着任何人能够取得你服务器中的 Token 或数据库，但服务器 SSH 权限与配置仍须妥善保护。
