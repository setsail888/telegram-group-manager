#!/usr/bin/env bash
# Public bootstrap and local repair entry. Run as the SSH account.
set +x
set -Eeuo pipefail
umask 077

say() { printf '\n[GroupGuard] %s\n' "$*"; }
fail() { printf '\n[GroupGuard] 错误：%s\n' "$*" >&2; exit 1; }

[[ "${BASH_VERSINFO[0]}" -ge 4 ]] || fail "需要 Bash 4 或更高版本"
[[ "$(uname -s)" == "Linux" ]] || fail "仅支持 Linux 服务器"
MODE="${1:-install}"
[[ "$#" -le 1 && ( "${MODE}" == "install" || "${MODE}" == "prepare" ) ]] ||
  fail "用法：bash install.sh [install|prepare]；prepare仅准备迁移环境，不启动机器人"

# bash install.sh in a checkout retains its original repair behavior.
# A download piped into Bash has no BASH_SOURCE and uses the public bootstrap.
if [[ -n "${BASH_SOURCE[0]:-}" && -f "${BASH_SOURCE[0]}" ]]; then
  local_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
  if [[ -f "${local_dir}/scripts/server-manager.sh" && -f "${local_dir}/compose.yaml" ]]; then
    exec bash "${local_dir}/scripts/server-manager.sh" "${MODE}"
  fi
fi

readonly REPO="https://github.com/slobys/telegram-group-manager.git"
INSTALL_DIR="${GROUPGUARD_DIR:-${HOME}/groupguard}"

if ! command -v git >/dev/null 2>&1; then
  say "尚未安装 Git，准备在 Debian / Ubuntu 上自动安装"
  [[ -r /etc/os-release ]] || fail "无法识别操作系统，请手动安装 Git"
  # shellcheck source=/dev/null
  source /etc/os-release
  case "${ID}" in
    debian|ubuntu) ;;
    *) fail "此发行版尚不支持自动安装 Git，请先自行安装" ;;
  esac
  if [[ "${EUID}" -eq 0 ]]; then
    apt-get update && apt-get install -y git ca-certificates || fail "安装 Git 失败，请检查软件源"
  else
    command -v sudo >/dev/null 2>&1 || fail "缺少 sudo，无法安装 Git"
    sudo apt-get update && sudo apt-get install -y git ca-certificates || fail "安装 Git 失败，请检查 sudo 权限和软件源"
  fi
fi
command -v git >/dev/null 2>&1 || fail "Git 未安装成功"

if [[ -e "${INSTALL_DIR}" && ! -d "${INSTALL_DIR}/.git" ]]; then
  fail "${INSTALL_DIR} 已存在但并非本项目 Git 仓库；不会覆盖，请另选 GROUPGUARD_DIR"
fi

if [[ -d "${INSTALL_DIR}/.git" ]]; then
  if ! git -C "${INSTALL_DIR}" remote get-url origin 2>/dev/null |
       grep -Eq '^((https://github\.com/)|(git@github\.com:))slobys/telegram-group-manager(\.git)?$'; then
    fail "目标目录是其他 Git 仓库，禁止覆盖"
  fi
  if [[ "${MODE}" == "prepare" ]]; then
    [[ -z "$(git -C "${INSTALL_DIR}" status --porcelain)" ]] || fail "发现本地源码改动，未更新或启动机器人"
    GIT_TERMINAL_PROMPT=0 git -C "${INSTALL_DIR}" pull --ff-only origin main || fail "迁移环境源码更新失败"
    exec bash "${INSTALL_DIR}/scripts/server-manager.sh" prepare
  fi
  say "发现已安装的 GroupGuard，先备份数据库并更新 main"
  bash "${INSTALL_DIR}/scripts/server-manager.sh" update || fail "更新失败，请查看上方错误；原有配置和数据保留"
  # Older managers did not register shortcuts. Invoke the newly pulled file.
  exec bash "${INSTALL_DIR}/scripts/server-manager.sh" shortcuts
fi

say "从公开 GitHub 仓库安装到 ${INSTALL_DIR}，无需 GitHub 登录"
GIT_TERMINAL_PROMPT=0 git clone --depth 1 --branch main "${REPO}" "${INSTALL_DIR}" ||
  fail "克隆失败，请检查 GitHub 网络连接或重试"
[[ -f "${INSTALL_DIR}/scripts/server-manager.sh" ]] || fail "下载的仓库缺少安装管理脚本"
exec bash "${INSTALL_DIR}/scripts/server-manager.sh" "${MODE}"
