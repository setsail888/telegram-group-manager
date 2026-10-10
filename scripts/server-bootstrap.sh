#!/usr/bin/env bash
# GroupGuard first-run bootstrap for private GitHub repositories.
# Run as the ordinary SSH user, never as root via sudo bash.
set +x
set -Eeuo pipefail
umask 077

readonly REPO="slobys/telegram-group-manager"
INSTALL_DIR="${GROUPGUARD_DIR:-${HOME}/groupguard}"

say() { printf '\n\033[1;36m[GroupGuard]\033[0m %s\n' "$*"; }
fail() { printf '\n[GroupGuard] 错误：%s\n' "$*" >&2; exit 1; }

[[ "${BASH_VERSINFO[0]}" -ge 4 ]] || fail "需要 Bash 4 或更高版本"
[[ "$(uname -s)" == "Linux" ]] || fail "仅支持 Linux 服务器"
command -v gh >/dev/null 2>&1 || fail "请先安装 GitHub CLI (gh)"
command -v git >/dev/null 2>&1 || fail "请先安装 Git"

if ! gh auth status >/dev/null 2>&1; then
  say "GitHub 私有仓库需要进行一次账号授权，不会要求你在聊天里发送 Token。"
  gh auth login --hostname github.com --git-protocol https --web ||
    fail "GitHub 登录失败，请先执行 gh auth login"
fi
gh auth setup-git >/dev/null

if [[ -e "${INSTALL_DIR}" && ! -d "${INSTALL_DIR}/.git" ]]; then
  fail "${INSTALL_DIR} 已存在但并非本项目 Git 仓库；不会覆盖，请另选 GROUPGUARD_DIR"
fi

if [[ -d "${INSTALL_DIR}/.git" ]]; then
  if ! git -C "${INSTALL_DIR}" remote get-url origin 2>/dev/null |
       grep -Eq '^((https://github\.com/)|(git@github\.com:))slobys/telegram-group-manager(\.git)?$'; then
    fail "目标目录是其他 Git 仓库，禁止覆盖"
  fi
  say "发现已安装的 GroupGuard，将运行修复安装；更新源码请使用管理菜单第 2 项。"
else
  say "开始克隆私有仓库 ${REPO} 到 ${INSTALL_DIR}"
  gh repo clone "${REPO}" "${INSTALL_DIR}" ||
    fail "克隆失败。确认当前 GitHub 账号具有 ${REPO} 访问权限"
fi

[[ -f "${INSTALL_DIR}/scripts/server-manager.sh" ]] ||
  fail "下载的仓库缺少安装管理脚本"
say "即将进入中文部署菜单"
exec bash "${INSTALL_DIR}/scripts/server-manager.sh" install
