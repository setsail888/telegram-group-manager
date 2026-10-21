#!/usr/bin/env bash
# Compatibility entry for the original public bootstrap URL.
set +x
set -Eeuo pipefail
umask 077

if [[ -n "${BASH_SOURCE[0]:-}" && -f "${BASH_SOURCE[0]}" ]]; then
  source_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
  if [[ -f "${source_dir}/install.sh" && -f "${source_dir}/compose.yaml" ]]; then
    exec bash "${source_dir}/install.sh" "$@"
  fi
fi

command -v curl >/dev/null 2>&1 || {
  printf '需要 curl，请先安装 curl 和 ca-certificates。\n' >&2
  exit 1
}
installer="$(mktemp "${TMPDIR:-/tmp}/groupguard-install.XXXXXXXX.sh")"
trap 'rm -f -- "$installer"' EXIT
curl --proto '=https' --tlsv1.2 --connect-timeout 20 --max-time 120 \
  -fsSL https://raw.githubusercontent.com/slobys/telegram-group-manager/main/install.sh -o "${installer}"
[[ -s "${installer}" ]] || { printf '下载安装脚本为空，已停止。\n' >&2; exit 1; }
bash "${installer}" "$@"
