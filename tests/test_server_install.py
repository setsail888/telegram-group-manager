"""Offline mock-only checks: never install Docker or call GitHub."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


PROJECT = Path(__file__).resolve().parents[1]


class InstallerScriptTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.project = self.base / "app"
        (self.project / "scripts").mkdir(parents=True)
        (self.base / "bin").mkdir()
        (self.base / "home").mkdir()
        shutil.copy2(PROJECT / "scripts/server-manager.sh",
                     self.project / "scripts/server-manager.sh")
        shutil.copy2(PROJECT / "scripts/server-bootstrap.sh",
                     self.project / "scripts/server-bootstrap.sh")
        shutil.copy2(PROJECT / "config.sample",
                     self.project / "config.sample")
        shutil.copy2(PROJECT / "compose.yaml",
                     self.project / "compose.yaml")
        self.log = self.base / "calls.log"
        self.env = dict(os.environ)
        self.env.update({
            "HOME": str(self.base / "home"),
            "PATH": f"{self.base}/bin:{os.environ.get('PATH', '')}",
            "GROUPGUARD_BACKUPS": str(self.base / "backups"),
            "GROUPGUARD_MOCK_LOG": str(self.log),
        })
        self.fake_bin("docker", """#!/usr/bin/env bash
set -eu
printf 'docker %s\\n' "$*" >> "$GROUPGUARD_MOCK_LOG"
if [[ "$1" == "compose" && "$2" == "version" ]]; then
  echo "Docker Compose version v2.test"
elif [[ "$1" == "compose" && "$2" == "ps" ]]; then
  echo "fake-container-id"
elif [[ "$1" == "compose" && "$2" == "cp" ]]; then
  printf 'SQLite format 3' > "${@: -1}"
fi
""")
        self.fake_bin("sudo", """#!/usr/bin/env bash
set -eu
printf 'sudo %s\\n' "$*" >> "$GROUPGUARD_MOCK_LOG"
if [[ "$1" == "chown" ]]; then
  exit 0
fi
exec "$@"
""")
        self.token = "123456789:" + "A" * 35

    def tearDown(self):
        self.temporary.cleanup()

    def fake_bin(self, name, contents):
        path = self.base / "bin" / name
        path.write_text(contents, encoding="utf-8")
        path.chmod(0o755)

    def run_manager(self, command="help"):
        return subprocess.run(
            ["bash", "scripts/server-manager.sh", command],
            cwd=self.project, env=self.env, text=True,
            capture_output=True, timeout=15)

    def run_source(self, code):
        return subprocess.run(
            ["bash", "-c", "source scripts/server-manager.sh help >/dev/null\n" + code],
            cwd=self.project, env=self.env, text=True,
            capture_output=True, timeout=15)

    def provision_env(self):
        result = self.run_source(
            f'env_put BOT_TOKEN "{self.token}"\n'
            'env_put OCR_ENABLED 0\n')
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_help_does_not_mutate_system(self):
        result = self.run_manager("help")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("一键管理", result.stdout)
        self.assertFalse((self.project / ".env").exists())
        self.assertFalse(self.log.exists())

    def test_token_saved_safely_and_preserves_other_keys(self):
        result = self.run_source(
            f'env_put BOT_TOKEN "{self.token}"\n'
            'env_put OCR_ENABLED 1\n'
            'env_put OCR_ENABLED 0\n'
            'printf "OCR=%s\\n" "$(env_get OCR_ENABLED)"\n')
        self.assertEqual(result.returncode, 0, result.stderr)
        content = (self.project / ".env").read_text()
        self.assertEqual(content.count("OCR_ENABLED="), 1)
        self.assertIn("OCR_ENABLED=0", content)
        self.assertIn("WITH_OCR=0", content)
        self.assertIn("BOT_TOKEN=" + self.token, content)
        self.assertEqual((self.project / ".env").stat().st_mode & 0o777, 0o600)
        self.assertNotIn(self.token, result.stdout + result.stderr)
        self.assertEqual(result.stdout.strip().splitlines()[-1], "OCR=0")

    def test_token_change_recreates_existing_container(self):
        self.provision_env()
        changed = "123456789:" + "B" * 35
        result = self.run_source(
            f'tty_secret() {{ REPLY="{changed}"; }}\n'
            'configure_action\n')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("BOT_TOKEN=" + changed,
                      (self.project / ".env").read_text())
        self.assertIn("docker compose up -d --force-recreate groupbot",
                      self.log.read_text())
        self.assertNotIn(changed, result.stdout + result.stderr)

    def test_env_line_break_injection_rejected(self):
        result = self.run_source(
            "env_put OCR_ENABLED $'1\\nWITH_OCR=1'\n")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.project / ".env").exists())

    def test_install_uses_existing_docker_without_apt(self):
        self.provision_env()
        result = self.run_manager("install")
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.log.read_text()
        self.assertIn("docker compose version", calls)
        self.assertIn("docker compose config -q", calls)
        self.assertIn("docker compose up -d --build groupbot", calls)
        self.assertNotIn("apt-get", calls)
        self.assertNotIn(self.token, result.stdout + result.stderr + calls)

    def test_existing_docker_without_compose_v2_is_not_overwritten(self):
        self.provision_env()
        self.fake_bin("docker", """#!/usr/bin/env bash
if [[ "$1" == "compose" && "$2" == "version" ]]; then exit 1; fi
exit 0
""")
        result = self.run_manager("install")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("缺少 Compose V2", result.stderr)
        self.assertFalse(self.log.exists())

    def test_docker_apt_index_failure_stops_with_reason(self):
        self.provision_env()
        result = self.run_source("""
docker_available() { return 1; }
check_os() {
  ID=ubuntu
  DOCKER_APT_SUITE=noble
  DOCKER_APT_ARCH=amd64
}
root() {
  printf 'MOCK ROOT: %s\\n' "$*" >&2
  return 100
}
install_docker
echo 'THIS MUST NOT PRINT'
""")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("1/7 更新系统 apt 软件包索引", result.stdout + result.stderr)
        self.assertIn("退出码 100", result.stderr)
        self.assertIn("不要反复选择安装", result.stderr)
        self.assertNotIn("THIS MUST NOT PRINT", result.stdout)
        self.assertNotIn("Docker Engine 和 Compose V2 的软件包安装已成功",
                         result.stdout)

    def test_docker_failed_install_never_starts_groupbot(self):
        result = self.run_source("""
docker_available() { return 1; }
check_os() {
  ID=debian
  DOCKER_APT_SUITE=bookworm
  DOCKER_APT_ARCH=amd64
}
root() { return 100; }
(install) || warn "安装失败，等待人工检查 apt 错误"
echo "SHELL_CAN_RETURN_TO_MENU"
""")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("1/7 更新系统 apt 软件包索引", result.stderr)
        self.assertIn("退出码 100", result.stderr)
        self.assertIn("安装失败，等待人工检查 apt 错误", result.stderr)
        self.assertIn("SHELL_CAN_RETURN_TO_MENU", result.stdout)
        self.assertFalse((self.project / ".env").exists())
        self.assertFalse(self.log.exists())

    def test_docker_gpg_download_failure_stops_with_reason(self):
        self.provision_env()
        result = self.run_source("""
docker_available() { return 1; }
check_os() {
  ID=debian
  DOCKER_APT_SUITE=trixie
  DOCKER_APT_ARCH=amd64
}
root() {
  printf 'MOCK ROOT: %s\\n' "$*" >&2
  if [[ "$1" == curl ]]; then return 60; fi
  return 0
}
install_docker
echo 'THIS MUST NOT PRINT'
""")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("4/7 从 Docker 官网下载 GPG 密钥", result.stdout + result.stderr)
        self.assertIn("退出码 60", result.stderr)
        self.assertNotIn("THIS MUST NOT PRINT", result.stdout)

    def test_docker_package_install_failure_does_not_claim_success(self):
        self.provision_env()
        result = self.run_source("""
docker_available() { return 1; }
check_os() {
  ID=ubuntu
  DOCKER_APT_SUITE=noble
  DOCKER_APT_ARCH=amd64
}
root() {
  if [[ "$1" == tee ]]; then cat >/dev/null; return 0; fi
  if [[ "$*" == *"docker-ce docker-ce-cli"* ]]; then return 100; fi
  return 0
}
install_docker
echo 'THIS MUST NOT PRINT'
""")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("7/7 安装 Docker Engine、Buildx 与 Compose V2",
                      result.stdout + result.stderr)
        self.assertIn("退出码 100", result.stderr)
        self.assertNotIn("Docker Engine 和 Compose V2 的软件包安装已成功",
                         result.stdout)
        self.assertNotIn("THIS MUST NOT PRINT", result.stdout)

    def test_docker_clean_install_simulated_completes_all_steps(self):
        self.provision_env()
        result = self.run_source("""
docker_available() { [[ -f "$HOME/docker-installed" ]]; }
check_os() {
  ID=ubuntu
  DOCKER_APT_SUITE=noble
  DOCKER_APT_ARCH=amd64
}
root() {
  if [[ "$1" == tee ]]; then cat >/dev/null; return 0; fi
  if [[ "$*" == *"docker-ce docker-ce-cli"* ]]; then
    touch "$HOME/docker-installed"
  fi
  return 0
}
install_docker
""")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("7/7 安装 Docker Engine、Buildx 与 Compose V2",
                      result.stdout + result.stderr)
        self.assertIn("Docker Engine、Compose V2 和守护进程检查通过",
                      result.stdout)

    def test_menu_launches_install_in_isolated_bash(self):
        source = (PROJECT / "scripts/server-manager.sh").read_text()
        self.assertIn('bash "${APP_DIR}/scripts/server-manager.sh" install', source)
        self.assertNotIn('1) (install) || warn', source)

    def test_uninstall_default_never_purges_volume(self):
        self.provision_env()
        result = self.run_source(
            'tty_line() { REPLY="1"; }\n'
            'uninstall_menu\n')
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.log.read_text()
        self.assertIn("docker compose --profile dashboard down", calls)
        self.assertNotIn("down -v", calls)

    def test_uninstall_purge_requires_exact_confirmation(self):
        self.provision_env()
        result = self.run_source(
            'i=0\n'
            'tty_line() { '
            'if (( i == 0 )); then REPLY=2; else REPLY=WRONG; fi; '
            'i=$((i+1)); }\n'
            'uninstall_menu\n')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.log.exists() and "down -v" in self.log.read_text())

    def test_backup_stops_and_recovers_running_service(self):
        self.provision_env()
        result = self.run_manager("backup")
        self.assertEqual(result.returncode, 0, result.stderr)
        files = list((self.base / "backups").glob("*.sqlite3"))
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0].stat().st_mode & 0o777, 0o600)
        calls = self.log.read_text()
        self.assertIn("docker compose stop groupbot", calls)
        self.assertIn("docker compose start groupbot", calls)
        self.assertLess(calls.index("docker compose stop groupbot"),
                        calls.index("docker compose start groupbot"))

    def test_backup_respects_configured_sqlite_path(self):
        self.provision_env()
        result = self.run_source(
            'env_put DB_PATH "/app/data/custom.sqlite3"\n')
        self.assertEqual(result.returncode, 0, result.stderr)
        result = self.run_manager("backup")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("docker compose cp groupbot:/app/data/custom.sqlite3",
                      self.log.read_text())

    def test_backup_refuses_database_outside_volume(self):
        self.provision_env()
        result = self.run_source('env_put DB_PATH "/etc/passwd"\n')
        self.assertEqual(result.returncode, 0, result.stderr)
        result = self.run_manager("backup")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("DB_PATH 不在 /app/data/", result.stderr)
        self.assertNotIn("docker compose stop groupbot", self.log.read_text())

    def test_backup_from_retained_volume_after_compose_down(self):
        self.provision_env()
        self.fake_bin("docker", """#!/usr/bin/env bash
set -eu
printf 'docker %s\\n' "$*" >> "$GROUPGUARD_MOCK_LOG"
if [[ "$1" == "compose" && "$2" == "ps" ]]; then
  if [[ "$*" == *"-a -q"* && -f "$HOME/container-created" ]]; then
    echo "container-after-create"
  fi
elif [[ "$1" == "compose" && "$2" == "create" ]]; then
  touch "$HOME/container-created"
elif [[ "$1" == "compose" && "$2" == "cp" ]]; then
  printf 'SQLite format 3' > "${@: -1}"
fi
""")
        result = self.run_manager("backup")
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.log.read_text()
        self.assertIn("docker compose create groupbot", calls)
        self.assertIn("docker compose cp groupbot:/app/data/bot.sqlite3", calls)
        self.assertNotIn("docker compose start groupbot", calls)
        self.assertEqual(len(list((self.base / "backups").glob("*.sqlite3"))), 1)

    def test_update_backs_up_before_public_git_pull(self):
        self.provision_env()
        (self.project / ".git").mkdir()
        script = """
git() {
  case "$*" in
    *"remote get-url origin"*) echo "https://github.com/slobys/telegram-group-manager.git" ;;
    *"status --porcelain"*) : ;;
    *"pull --ff-only origin main"*) printf "git pulled\\\\n" >> "$GROUPGUARD_MOCK_LOG" ;;
    *"rev-parse --short HEAD"*) echo abc123 ;;
    *) return 2 ;;
  esac
}
update
"""
        result = self.run_source(script)
        self.assertEqual(result.returncode, 0, result.stderr)
        actions = self.log.read_text()
        self.assertIn("git pulled", actions)
        self.assertIn("docker compose cp groupbot:/app/data/bot.sqlite3", actions)
        self.assertLess(actions.index("docker compose cp"), actions.index("git pulled"))
        self.assertLess(actions.index("git pulled"),
                        actions.index("docker compose up -d --build groupbot"))

    def test_update_refuses_unexpected_remote(self):
        self.provision_env()
        (self.project / ".git").mkdir()
        script = """
git() { echo "https://github.com/attacker/other.git"; }
update
"""
        result = self.run_source(script)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("不是预期的", result.stderr)
        self.assertFalse(self.log.exists())

    def test_update_refuses_modified_tracked_files(self):
        self.provision_env()
        (self.project / ".git").mkdir()
        script = """
git() {
  case "$*" in
    *"remote get-url origin"*) echo "https://github.com/slobys/telegram-group-manager.git" ;;
    *"status --porcelain"*) echo " M app/store.py" ;;
    *) return 2 ;;
  esac
}
update
"""
        result = self.run_source(script)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("本地源码改动", result.stderr)
        self.assertFalse(self.log.exists())

    def test_update_aborts_if_sqlite_backup_fails(self):
        self.provision_env()
        (self.project / ".git").mkdir()
        self.fake_bin("docker", """#!/usr/bin/env bash
printf 'docker %s\\n' "$*" >> "$GROUPGUARD_MOCK_LOG"
if [[ "$1" == "compose" && "$2" == "ps" ]]; then echo "running"; fi
if [[ "$1" == "compose" && "$2" == "cp" ]]; then exit 1; fi
exit 0
""")
        script = """
git() {
  case "$*" in
    *"remote get-url origin"*) echo "https://github.com/slobys/telegram-group-manager.git" ;;
    *"status --porcelain"*) : ;;
    *"pull --ff-only origin main"*) echo UNEXPECTED_PULL >> "$GROUPGUARD_MOCK_LOG" ;;
  esac
}
update
"""
        result = self.run_source(script)
        self.assertNotEqual(result.returncode, 0)
        calls = self.log.read_text()
        self.assertIn("docker compose cp", calls)
        self.assertNotIn("UNEXPECTED_PULL", calls)

    def test_readme_one_command_public_repo_bootstrap_offline(self):
        # Exact command displayed to users; public curl/git and Docker mocked.
        self.env["GROUPGUARD_MOCK_SOURCE"] = str(self.project)
        self.env["GROUPGUARD_MOCK_TOKEN"] = self.token
        self.fake_bin("curl", """#!/usr/bin/env bash
set -eu
printf 'curl %s\\n' "$*" >> "$GROUPGUARD_MOCK_LOG"
[[ "$*" == *"raw.githubusercontent.com/slobys/telegram-group-manager/main/scripts/server-bootstrap.sh"* ]] || exit 3
cat "$GROUPGUARD_MOCK_SOURCE/scripts/server-bootstrap.sh"
""")
        self.fake_bin("git", """#!/usr/bin/env bash
set -eu
printf 'git %s\\n' "$*" >> "$GROUPGUARD_MOCK_LOG"
[[ "$1" == "clone" ]] || exit 3
dest="${@: -1}"
mkdir -p "$dest"
cp -R "$GROUPGUARD_MOCK_SOURCE"/. "$dest/"
printf 'BOT_TOKEN=%s\\n' "$GROUPGUARD_MOCK_TOKEN" > "$dest/.env"
chmod 600 "$dest/.env"
""")
        command = next(
            line for line in (PROJECT / "README.md").read_text().splitlines()
            if line.startswith("bash -c 'set -euo pipefail;"))
        result = subprocess.run(
            ["bash", "-c", command], cwd=self.project, env=self.env,
            text=True, capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        actions = self.log.read_text()
        self.assertIn("curl -fsSL https://raw.githubusercontent.com/slobys/telegram-group-manager", actions)
        self.assertIn("git clone --depth 1 --branch main https://github.com/slobys/telegram-group-manager.git", actions)
        self.assertIn("docker compose up -d --build groupbot", actions)
        self.assertNotIn("gh auth", actions)
        self.assertTrue((self.base / "home/groupguard/.env").exists())
        self.assertNotIn(self.token, result.stdout + result.stderr)

    def test_public_and_docs_commands_are_identical(self):
        readme = next(
            line for line in (PROJECT / "README.md").read_text().splitlines()
            if line.startswith("bash -c 'set -euo pipefail;"))
        docs = next(
            line for line in (PROJECT / "docs/server-one-click.md").read_text().splitlines()
            if line.startswith("bash -c 'set -euo pipefail;"))
        self.assertEqual(readme, docs)
        self.assertNotIn("gh auth", readme)
        self.assertIn("raw.githubusercontent.com/slobys/telegram-group-manager", readme)

    def test_public_one_command_bootstraps_without_curl_or_git(self):
        """Simulate a fresh Debian SSH server with no curl/git in PATH."""
        self.env["GROUPGUARD_MOCK_SOURCE"] = str(self.project)
        self.env["GROUPGUARD_MOCK_TOKEN"] = self.token
        self.env["GROUPGUARD_MOCK_BIN"] = str(self.base / "bin")
        self.env["PATH"] = str(self.base / "bin")
        # Provide only the other core utilities, deliberately NOT curl or git.
        for name in ("bash", "uname", "dirname", "pwd", "cat", "cp",
                     "chmod", "mkdir", "grep", "id"):
            target = shutil.which(name)
            self.assertIsNotNone(target)
            (self.base / "bin" / name).symlink_to(target)
        self.fake_bin("apt-get", """#!/usr/bin/env bash
set -eu
printf 'apt-get %s\\n' "$*" >> "$GROUPGUARD_MOCK_LOG"
[[ "$1" == "install" ]] || exit 0
if [[ "$*" == *"curl"* ]]; then
  cat > "$GROUPGUARD_MOCK_BIN/curl" <<'MOCK_CURL'
#!/usr/bin/env bash
set -eu
printf 'curl %s\\n' "$*" >> "$GROUPGUARD_MOCK_LOG"
cat "$GROUPGUARD_MOCK_SOURCE/scripts/server-bootstrap.sh"
MOCK_CURL
  chmod +x "$GROUPGUARD_MOCK_BIN/curl"
fi
if [[ "$*" == *"git"* ]]; then
  cat > "$GROUPGUARD_MOCK_BIN/git" <<'MOCK_GIT'
#!/usr/bin/env bash
set -eu
printf 'git %s\\n' "$*" >> "$GROUPGUARD_MOCK_LOG"
[[ "$1" == "clone" ]] || exit 3
dest="${@: -1}"
mkdir -p "$dest"
cp -R "$GROUPGUARD_MOCK_SOURCE"/. "$dest/"
printf 'BOT_TOKEN=%s\\n' "$GROUPGUARD_MOCK_TOKEN" > "$dest/.env"
chmod 600 "$dest/.env"
MOCK_GIT
  chmod +x "$GROUPGUARD_MOCK_BIN/git"
fi
""")
        command = next(
            line for line in (PROJECT / "README.md").read_text().splitlines()
            if line.startswith("bash -c 'set -euo pipefail;"))
        result = subprocess.run(
            [str(self.base / "bin/bash"), "-c", command],
            cwd=self.project, env=self.env, text=True,
            capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.log.read_text()
        self.assertIn("apt-get install -y curl ca-certificates", calls)
        self.assertIn("apt-get install -y git ca-certificates", calls)
        self.assertIn("git clone --depth 1 --branch main https://github.com/slobys/telegram-group-manager.git", calls)
        self.assertIn("docker compose up -d --build groupbot", calls)
        self.assertNotIn("gh auth", calls)
        self.assertNotIn(self.token, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
