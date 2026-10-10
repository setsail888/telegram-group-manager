#!/usr/bin/env bash
# GroupGuard Chinese server management: Debian/Ubuntu + Docker Compose V2.
# Do not run this script with "curl | sudo bash". Run as your SSH account;
# elevated privileges are only requested for apt, systemctl and Docker.
set +x  # Never echo BotFather or dashboard secrets, even if caller used bash -x.
set -Eeuo pipefail
umask 077

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
APP_DIR="$(cd -- "${SCRIPT_DIR}/.." && pwd -P)"
ENV_FILE="${APP_DIR}/.env"
BACKUP_DIR="${GROUPGUARD_BACKUPS:-${HOME}/groupguard-backups}"
SERVICE="groupbot"

info() { printf '\n\033[1;36m[GroupGuard]\033[0m %s\n' "$*"; }
warn() { printf '\n\033[1;33m[注意]\033[0m %s\n' "$*" >&2; }
die() { printf '\n\033[1;31m[错误]\033[0m %s\n' "$*" >&2; exit 1; }

[[ "${BASH_VERSINFO[0]}" -ge 4 ]] || die "需要 Bash 4 或更高版本"
[[ "$(uname -s)" == "Linux" ]] || die "此脚本仅支持 Linux"
[[ -f "${APP_DIR}/compose.yaml" && -f "${APP_DIR}/config.sample" ]] ||
  die "安装目录不完整：${APP_DIR}"

root() {
  if [[ "${EUID}" -eq 0 ]]; then
    # Match sudo's external-command lookup. A plain "$@" resolves `install`
    # to our deployment function, recursively rebuilding during gg setup.
    command "$@"
  else
    command -v sudo >/dev/null 2>&1 || die "此操作需要 sudo，请先为当前 SSH 用户配置 sudo"
    sudo "$@"
  fi
}

# Do not depend on shell errexit here: the interactive menu historically ran
# (install) || warn, which suppresses Bash -e throughout the called function.
# Every fallible installation step must fail explicitly, with visible output.
docker_available() {
  command -v docker >/dev/null 2>&1
}

docker_daemon_binary_present() {
  command -v dockerd >/dev/null 2>&1
}

docker_step() {
  local stage="$1" rc
  shift
  # stderr keeps progress visible when a command (e.g. tee) redirects stdout.
  info "Docker 安装步骤：${stage}" >&2
  if "$@"; then
    return 0
  else
    rc="$?"
    die "Docker 安装失败：${stage}（退出码 ${rc}）。请查看上方的具体报错（apt 软件源、网络、权限或软件包冲突），不要反复选择安装。"
  fi
}

dc() {
  (cd "${APP_DIR}" && root docker compose "$@")
}

tty_line() {
  local label="$1"
  local result=""
  [[ -r /dev/tty ]] || die "需要交互终端；请通过 SSH 直接运行此安装脚本"
  printf '%s' "${label}" > /dev/tty
  IFS= read -r result < /dev/tty || return 1
  REPLY="${result}"
}

tty_secret() {
  local label="$1"
  local result=""
  [[ -r /dev/tty ]] || die "需要交互终端输入密钥"
  printf '%s' "${label}" > /dev/tty
  IFS= read -r -s result < /dev/tty || return 1
  printf '\n' > /dev/tty
  REPLY="${result}"
}

pause() {
  tty_line $'\n按回车返回菜单...' || :
}

env_get() {
  local name="$1"
  [[ -f "${ENV_FILE}" ]] || return 0
  local line
  while IFS= read -r line || [[ -n "${line}" ]]; do
    if [[ "${line}" == "${name}="* ]]; then
      printf '%s' "${line#*=}"
      return 0
    fi
  done < "${ENV_FILE}"
}

env_put() {
  local key="$1" value="$2" line="" found=0 tempfile
  [[ "${key}" =~ ^[A-Z][A-Z0-9_]*$ ]] || die "配置字段名不合法"
  [[ "${value}" != *$'\n'* && "${value}" != *$'\r'* ]] ||
    die "配置值不允许换行"
  [[ -f "${ENV_FILE}" ]] || {
    cp -- "${APP_DIR}/config.sample" "${ENV_FILE}"
    chmod 600 "${ENV_FILE}"
  }
  tempfile="$(mktemp "${APP_DIR}/.env.tmp.XXXXXX")" ||
    die "无法创建临时配置文件"
  chmod 600 "${tempfile}"
  while IFS= read -r line || [[ -n "${line}" ]]; do
    if [[ "${line}" == "${key}="* ]]; then
      if [[ "${found}" == "0" ]]; then
        printf '%s=%s\n' "${key}" "${value}" >> "${tempfile}"
        found=1
      fi
    else
      printf '%s\n' "${line}" >> "${tempfile}"
    fi
  done < "${ENV_FILE}"
  if [[ "${found}" == "0" ]]; then
    printf '%s=%s\n' "${key}" "${value}" >> "${tempfile}"
  fi
  mv -f -- "${tempfile}" "${ENV_FILE}"
  chmod 600 "${ENV_FILE}"
}

token_ready() {
  local token
  token="$(env_get BOT_TOKEN)"
  [[ "${token}" =~ ^[0-9]{6,15}:[A-Za-z0-9_-]{20,}$ ]]
}

configure_token() {
  local token
  info "Bot Token 仅保存到本机 .env（权限 600），不会打印到屏幕或 GitHub。"
  tty_secret "粘贴 @BotFather 提供的完整 Token： " ||
    die "没有收到 Token"
  token="${REPLY}"
  if [[ ! "${token}" =~ ^[0-9]{6,15}:[A-Za-z0-9_-]{20,}$ ]]; then
    die "Token 格式无效，应类似 数字:字母数字；本次没有保存。"
  fi
  env_put BOT_TOKEN "${token}"
  unset token REPLY
  info "Bot Token 已安全写入 .env"
}

configure_action() {
  configure_token
  if command -v docker >/dev/null 2>&1; then
    if [[ -n "$(dc ps -a -q groupbot)" ]]; then
      info "检测到已经部署的机器人，应用新 Token 并重新创建容器"
      dc up -d --force-recreate groupbot
      info "新配置已生效，请查看日志确认 Telegram 认证成功"
    fi
  fi
}

check_os() {
  [[ -f /etc/os-release ]] || die "无法识别 Linux 发行版"
  # shellcheck source=/dev/null
  source /etc/os-release
  case "${ID}" in
    debian|ubuntu) ;;
    *) die "自动安装 Docker 仅支持 Debian / Ubuntu；检测到 ${ID}" ;;
  esac
  local arch
  command -v dpkg >/dev/null 2>&1 ||
    die "找不到 dpkg，暂不支持该系统自动安装 Docker"
  arch="$(dpkg --print-architecture)" ||
    die "无法确定 CPU 包架构"
  [[ "${arch}" == amd64 || "${arch}" == arm64 ]] ||
    die "不支持自动安装 Docker 的架构：${arch}"
  info "检测到 ${PRETTY_NAME:-${ID}}（${arch}）"
}

install_docker_official() (
  # Download first, then execute: a failed curl cannot become a successful
  # empty pipe. This uses Docker's official convenience installer, not
  # a repo/apt configuration reimplementation. Intended for clean servers.
  set -Eeuo pipefail
  local script
  command -v curl >/dev/null 2>&1 ||
    die "服务器没有 curl，请先运行 apt-get install -y curl ca-certificates"
  command -v mktemp >/dev/null 2>&1 ||
    die "服务器没有 mktemp，无法安全下载 Docker 安装脚本"
  script="$(mktemp "${TMPDIR:-/tmp}/groupguard-get-docker.XXXXXXXX.sh")" ||
    die "临时文件创建失败，检查 /tmp 权限或磁盘空间"
  trap 'rm -f -- "$script"' EXIT
  info "开始快捷安装 Docker（官方 https://get.docker.com）"
  docker_step "下载 Docker 官方安装脚本" \
    curl --proto '=https' --tlsv1.2 --connect-timeout 20 --max-time 120 \
      -fsSL https://get.docker.com -o "${script}"
  [[ -s "${script}" ]] || die "下载到的 Docker 脚本为空，不会执行"
  docker_step "执行 Docker 官方快捷安装脚本" root sh "${script}"
)

install_docker() {
  if docker_available; then
    info "检测到 Docker，跳过 Docker Engine 安装，不修改已有容器"
  else
    check_os
    if docker_daemon_binary_present; then
      die "检测到已有 dockerd，但未找到 docker 客户端；为保护已有容器，请先检查 Docker 安装状态"
    fi
    install_docker_official
    docker_available ||
      die "官方安装脚本已退出，但仍找不到 docker 命令，请检查上方安装日志"
  fi
  if ! root docker compose version >/dev/null 2>&1; then
    # A standalone docker-compose executable is not Compose V2 CLI plugin.
    # Install only the plugin; --no-remove prevents apt removing existing
    # packages. We never replace an existing Docker Engine.
    check_os
    command -v apt-get >/dev/null 2>&1 ||
      die "Docker 已安装但没有 Compose V2，且当前系统不能运行 apt-get"
    docker_step "补装 Docker Compose V2 插件（不卸载已有软件）" \
      root env DEBIAN_FRONTEND=noninteractive \
      apt-get -y --no-remove install docker-compose-plugin
    root docker compose version >/dev/null 2>&1 ||
      die "Compose V2 插件安装后仍不可用；请查看 apt 的错误输出"
  fi
  if ! root docker info >/dev/null 2>&1; then
    if command -v systemctl >/dev/null 2>&1; then
      docker_step "启动并设置 Docker 服务开机自启" root systemctl enable --now docker
    fi
  fi
  root docker info >/dev/null 2>&1 ||
    die "Docker 客户端已经安装，但守护进程不可用。请检查：systemctl status docker 和 journalctl -u docker -n 50 --no-pager"
  info "✅ Docker Engine、Compose V2 和守护进程检查通过"
}

ensure_env() {
  if [[ ! -f "${ENV_FILE}" ]]; then
    cp -- "${APP_DIR}/config.sample" "${ENV_FILE}"
    chmod 600 "${ENV_FILE}"
    info "已生成 ${ENV_FILE}"
  fi
  chmod 600 "${ENV_FILE}"
  if ! token_ready; then
    configure_token
  else
    info "原有 Bot Token 已配置，直接沿用（不会展示内容）。"
  fi
}

check_config() {
  [[ -f "${ENV_FILE}" ]] || die "缺少 .env；请先执行 install 或 configure"
  token_ready || die "Bot Token 尚未正确配置"
  dc config -q || die "Docker Compose 配置检查失败"
}

install_shortcuts() (
  local launcher_dir="${GROUPGUARD_BIN_DIR:-/usr/local/bin}" name launcher
  [[ "${launcher_dir}" == /* ]] || die "快捷命令目录必须是绝对路径"
  for name in gg GG; do
    if [[ -e "${launcher_dir}/${name}" || -L "${launcher_dir}/${name}" ]]; then
      [[ -f "${launcher_dir}/${name}" && ! -L "${launcher_dir}/${name}" ]] &&
        grep -Fxq '# GroupGuard managed launcher' "${launcher_dir}/${name}" ||
        die "${launcher_dir}/${name} 已存在且不是本项目快捷命令，不会覆盖"
    fi
  done
  launcher="$(mktemp "${TMPDIR:-/tmp}/groupguard-launcher.XXXXXXXX")" ||
    die "无法创建快捷命令临时文件"
  trap 'rm -f -- "$launcher"' EXIT
  {
    printf '#!/usr/bin/env bash\n# GroupGuard managed launcher\n'
    printf 'exec bash %q "$@"\n' "${APP_DIR}/scripts/server-manager.sh"
  } > "${launcher}"
  [[ -d "${launcher_dir}" ]] || root install -d -m 755 -- "${launcher_dir}" ||
    die "无法创建快捷命令目录"
  for name in gg GG; do
    root install -m 755 -- "${launcher}" "${launcher_dir}/${name}" ||
      die "无法写入快捷命令 ${launcher_dir}/${name}"
  done
  info "已设置快捷命令：gg 或 GG 打开菜单，gg update 更新机器人（无需重新登录）"
)

show_status() {
  info "服务状态："
  dc --profile dashboard ps || :
  info "如看不到 groupbot，检查 Docker 构建和网络连接。"
}

show_logs() {
  info "机器人最近 80 行日志："
  dc logs --tail=80 groupbot || :
  info "持续观察：bash \"${APP_DIR}/scripts/server-manager.sh\" logs-follow"
}

backup() {
  check_config
  mkdir -p -- "${BACKUP_DIR}"
  chmod 700 "${BACKUP_DIR}"
  local container was_running=0 timestamp dest dbpath
  dbpath="$(env_get DB_PATH)"
  dbpath="${dbpath:-/app/data/bot.sqlite3}"
  if [[ ! "${dbpath}" =~ ^/app/data/[A-Za-z0-9_.-]+$ ]]; then
    die "DB_PATH 不在 /app/data/ 下或路径不安全；无法保证备份完整性，已停止"
  fi
  container="$(dc ps -a -q groupbot)"
  if [[ -z "${container}" ]]; then
    info "未找到 groupbot 容器，尝试使用现有镜像创建未启动的容器，以检查历史数据卷"
    dc create groupbot ||
      { warn "未找到可用于备份的既有镜像，请先安装并启动机器人"; return 1; }
    container="$(dc ps -a -q groupbot)"
    if [[ -z "${container}" ]]; then
      warn "仍未找到 groupbot 容器，无法备份数据库"
      return 1
    fi
  fi
  if [[ -n "$(dc ps -q groupbot)" ]]; then
    was_running=1
    info "短暂停止机器人，确保 SQLite 数据一致"
    dc stop groupbot
  fi
  timestamp="$(date +%Y%m%d-%H%M%S-%N)"
  dest="${BACKUP_DIR}/groupguard-${timestamp}.sqlite3"
  [[ ! -e "${dest}" ]] || die "备份路径已存在，为防止覆盖已取消备份"
  local ok=0
  if dc cp "groupbot:${dbpath}" "${dest}" &&
     [[ -s "${dest}" ]] &&
     [[ "$(head -c 15 "${dest}")" == "SQLite format 3" ]]; then
    root chown "$(id -u):$(id -g)" "${dest}"
    chmod 600 "${dest}"
    ok=1
    info "数据库已备份到 ${dest}"
  else
    warn "导出失败或备份不是有效 SQLite 文件；为防止覆盖数据，已拒绝继续更新"
    root rm -f -- "${dest}"
  fi
  if [[ "${was_running}" == "1" ]]; then
    dc start groupbot || warn "备份后恢复机器人失败，请检查日志"
  fi
  [[ "${ok}" == "1" ]]
}

install() {
  info "GroupGuard 一键安装 / 修复安装"
  [[ -f "${APP_DIR}/compose.yaml" ]] || die "缺少 Compose 配置"
  install_docker
  ensure_env
  check_config
  info "开始构建并启动中文 Telegram 管理机器人"
  dc up -d --build groupbot
  install_shortcuts || warn "部署已完成，但快捷命令未安装；可运行 bash \"${APP_DIR}/scripts/server-manager.sh\" shortcuts 重试"
  info "部署命令完成，以下是当前容器状态："
  dc ps
  info "接下来：私聊 @BotFather 创建并配置机器人，加入测试群设为管理员，发送 /check 和 /panel"
  warn "首次启动请查看日志确认 Telegram 连接正常：bash \"${APP_DIR}/scripts/server-manager.sh\" logs"
}

update() {
  command -v git >/dev/null 2>&1 || die "git 未安装"
  [[ -d "${APP_DIR}/.git" ]] || die "安装目录不是 Git 仓库"
  local upstream
  upstream="$(git -C "${APP_DIR}" remote get-url origin)" ||
    die "仓库缺少 origin 远程地址"
  if [[ ! "${upstream}" =~ ^((https://github\.com/)|(git@github\.com:))slobys/telegram-group-manager(\.git)?$ ]]; then
    die "远程仓库不是预期的 slobys/telegram-group-manager，已取消更新"
  fi
  if [[ -n "$(git -C "${APP_DIR}" status --porcelain)" ]]; then
    die "发现本地源码改动，更新已停止。请先备份和处理本地文件，避免覆盖"
  fi
  check_config
  info "更新前先执行数据库备份（已部署时）"
  backup || die "备份不成功，已取消更新；首次安装请改用 install"
  info "拉取 GitHub 公开仓库最新 main 分支，无需账号登录"
  GIT_TERMINAL_PROMPT=0 git -C "${APP_DIR}" pull --ff-only origin main ||
    die "更新失败；请检查公开 GitHub 仓库的网络连接和本地源码状态"
  dc config -q
  dc up -d --build groupbot
  # Load the newly pulled script, rather than the function definitions that
  # were already parsed before Git updated this file.
  bash "${APP_DIR}/scripts/server-manager.sh" shortcuts ||
    warn "机器人已更新，但快捷命令未安装；可运行管理脚本 shortcuts 重试"
  info "更新成功，当前 Git 提交：$(git -C "${APP_DIR}" rev-parse --short HEAD)"
  dc ps
}

dashboard_menu() {
  check_config
  tty_line "网页看板：1 启动 / 2 停止 / 0 返回： " || return
  case "${REPLY}" in
    1)
      local secret
      secret="$(env_get DASHBOARD_TOKEN)"
      if [[ "${#secret}" -lt 32 ]]; then
        command -v openssl >/dev/null 2>&1 ||
          die "缺少 openssl，请安装后再创建看板随机密钥"
        secret="$(openssl rand -hex 32)"
        env_put DASHBOARD_TOKEN "${secret}"
        warn "看板专用密钥只显示一次，请保存到安全的位置："
        printf 'DASHBOARD_TOKEN=%s\n' "${secret}" > /dev/tty
        unset secret
      fi
      dc --profile dashboard up -d --build dashboard
      info "只读网页看板已启动；只绑定服务器 127.0.0.1:8765"
      info "在自己的电脑创建 SSH 隧道：ssh -N -L 8765:127.0.0.1:8765 用户@服务器IP"
      info "再用本机浏览器访问 http://127.0.0.1:8765"
      ;;
    2) dc --profile dashboard stop dashboard; info "看板已停止" ;;
    0) return ;;
    *) warn "未知选项" ;;
  esac
}

ocr_menu() {
  check_config
  tty_line "可选本地 OCR：1 安装并启用 / 2 关闭并换回轻量版 / 0 返回： " || return
  case "${REPLY}" in
    1)
      env_put WITH_OCR 1
      env_put OCR_ENABLED 1
      dc up -d --build groupbot
      info "已安装可选 Tesseract 中文 OCR。接下来先设置独立日志群，再发送 /ocr review"
      warn "OCR 仅识别文字关键词；图片误判与漏判都可能发生"
      ;;
    2)
      env_put OCR_ENABLED 0
      env_put WITH_OCR 0
      dc up -d --build groupbot
      info "OCR 已关闭；机器人改用不含 Tesseract 的普通镜像"
      ;;
    0) return ;;
    *) warn "未知选项" ;;
  esac
}

stop_start_menu() {
  check_config
  tty_line "服务控制：1 启动 / 2 停止 / 3 重启 / 0 返回： " || return
  case "${REPLY}" in
    1) dc up -d groupbot ;;
    2) dc stop groupbot ;;
    3) dc restart groupbot ;;
    0) return ;;
    *) warn "未知选项" ;;
  esac
}

uninstall_menu() {
  check_config
  warn "仅卸载本项目的 Docker 服务，不会卸载系统 Docker 引擎或其他项目"
  tty_line "选择：1 停止并删除容器（保留数据） / 2 连同积分数据库一起删除 / 0 取消： " || return
  case "${REPLY}" in
    1)
      dc --profile dashboard down
      info "容器已移除；项目源码、.env、命名卷和备份均保留"
      ;;
    2)
      warn "危险：这将永久删除本项目命名卷中的群规则、积分、订单和抽奖记录！"
      tty_line "若已备份并确认，输入 DELETE GROUPGUARD DATA： " || return
      if [[ "${REPLY}" != "DELETE GROUPGUARD DATA" ]]; then
        info "已取消清除数据"
        return
      fi
      dc --profile dashboard down -v
      info "已移除 GroupGuard 服务和数据卷；不会清理源码、.env 或独立备份"
      ;;
    0) return ;;
    *) warn "未知选项" ;;
  esac
}

help_menu() {
  cat <<HELP

GroupGuard 服务器一键管理

  gg / GG                                               中文交互菜单
  gg update                                             先备份再更新 main 并重建容器
  gg shortcuts                                          安装 / 修复 gg 和 GG 快捷命令

  bash "${APP_DIR}/scripts/server-manager.sh"               中文交互菜单
  bash "${APP_DIR}/scripts/server-manager.sh" install       安装或修复部署
  bash "${APP_DIR}/scripts/server-manager.sh" update        先备份再从 GitHub 更新
  bash "${APP_DIR}/scripts/server-manager.sh" backup        备份 SQLite（保留历史）
  bash "${APP_DIR}/scripts/server-manager.sh" configure     修改 Bot Token
  bash "${APP_DIR}/scripts/server-manager.sh" status        查看容器状态
  bash "${APP_DIR}/scripts/server-manager.sh" logs          最近 80 行日志
  bash "${APP_DIR}/scripts/server-manager.sh" logs-follow   实时日志（Ctrl+C 退出）

重要：本项目不需要服务器域名、SSL 证书和任何对外开放端口。
网页看板只绑定 127.0.0.1:8765，必须通过 SSH 隧道访问。
代码仓库已公开；首次安装和自动更新均无需 GitHub 账号。
默认不启用 OCR；默认不自动启动网页看板。
HELP
}

menu() {
  while :; do
    printf '\n'
    printf '%s\n' '======================================================'
    printf '%s\n' '  🛡️  GroupGuard 群卫士｜云服务器一键管理'
    printf '%s\n' '======================================================'
    printf '%s\n' '  1. 安装 / 修复部署'
    printf '%s\n' '  2. 更新机器人（gg update，先备份再更新 main）'
    printf '%s\n' '  3. 查看运行状态'
    printf '%s\n' '  4. 查看最近日志'
    printf '%s\n' '  5. 修改 BotFather Token'
    printf '%s\n' '  6. 启动 / 停止 / 重启服务'
    printf '%s\n' '  7. 卸载（默认保留积分和群配置）'
    printf '%s\n' '  0. 退出'
    printf '%s\n' '======================================================'
    tty_line '请输入序号： ' || break
    case "${REPLY}" in
      # New Bash process: (install) || warn disables errexit inside install.
      1) bash "${APP_DIR}/scripts/server-manager.sh" install ||
           warn "安装未成功，请查看上方红色错误和 apt/curl 输出，排查原因后再重试" ;;
      2)
        if bash "${APP_DIR}/scripts/server-manager.sh" update; then
          exec bash "${APP_DIR}/scripts/server-manager.sh" menu
        else
          warn "更新未成功，保留原有数据；请查看上方具体错误后再重试"
        fi
        ;;
      3) (show_status) || : ;;
      4) (show_logs) || : ;;
      5) (configure_action) || : ;;
      6) (stop_start_menu) || : ;;
      7) (uninstall_menu) || : ;;
      0) return ;;
      *) warn "请输入菜单中的有效序号" ;;
    esac
    pause
  done
}

case "${1:-menu}" in
  install) install ;;
  update) update ;;
  shortcuts) install_shortcuts ;;
  configure) configure_action ;;
  status) show_status ;;
  logs) show_logs ;;
  logs-follow) dc logs -f --tail=80 groupbot ;;
  backup) backup ;;
  dashboard) dashboard_menu ;;
  ocr) ocr_menu ;;
  service) stop_start_menu ;;
  uninstall) uninstall_menu ;;
  help|-h|--help) help_menu ;;
  menu) menu ;;
  *) help_menu; die "未知操作：${1}" ;;
esac
