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
        shutil.copy2(PROJECT / "scripts/server-migrate.py",
                     self.project / "scripts/server-migrate.py")
        shutil.copy2(PROJECT / "scripts/server-bootstrap.sh",
                     self.project / "scripts/server-bootstrap.sh")
        shutil.copy2(PROJECT / "install.sh", self.project / "install.sh")
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
            "GROUPGUARD_BIN_DIR": str(self.base / "bin"),
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

    def fake_docker_installer_download(self, *, exit_code=0, empty=False):
        self.fake_bin("curl", """#!/usr/bin/env bash
set -eu
printf 'official-curl %s\\n' "$*" >> "$GROUPGUARD_MOCK_LOG"
while [[ "$#" -gt 0 ]]; do
  if [[ "$1" == "-o" ]]; then
    shift
    if [[ "$MOCK_DOCKER_CURL_EXIT" != "0" ]]; then
      exit "$MOCK_DOCKER_CURL_EXIT"
    fi
    if [[ "$MOCK_DOCKER_CURL_EMPTY" != "1" ]]; then
      printf '#!/bin/sh\\nexit 0\\n' > "$1"
    fi
    exit 0
  fi
  shift
done
exit 99
""")
        self.env["MOCK_DOCKER_CURL_EXIT"] = str(exit_code)
        self.env["MOCK_DOCKER_CURL_EMPTY"] = "1" if empty else "0"
        (self.base / "tmp").mkdir(exist_ok=True)
        self.env["TMPDIR"] = str(self.base / "tmp")

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

    def select_privilege_branch(self, *, root_user):
        # Force only the identity check in the isolated script, so ordinary
        # CI users exercise the real root branch too. All commands stay mocked
        # or write to this test's temporary directory; no sudo is required.
        source = (PROJECT / "scripts/server-manager.sh").read_text()
        condition = 'if [[ "${EUID}" -eq 0 ]]; then'
        self.assertEqual(source.count(condition), 1)
        source = source.replace(condition,
                                "if true; then" if root_user else "if false; then")
        (self.project / "scripts/server-manager.sh").write_text(source)
        self.log.unlink(missing_ok=True)

    def test_privileged_commands_bypass_same_named_shell_functions(self):
        self.fake_bin("install", """#!/usr/bin/env bash
set -eu
printf 'external install' >> "$GROUPGUARD_MOCK_LOG"
printf ' <%s>' "$@" >> "$GROUPGUARD_MOCK_LOG"
printf '\\n' >> "$GROUPGUARD_MOCK_LOG"
""")
        for root_user in (True, False):
            with self.subTest(root_user=root_user):
                self.select_privilege_branch(root_user=root_user)
                result = self.run_source("""
install() { echo UNEXPECTED_INSTALL_FUNCTION >&2; return 73; }
root install -m 755 -- 'source with spaces' 'destination with spaces'
""")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertNotIn("UNEXPECTED_INSTALL_FUNCTION", result.stderr)
                calls = self.log.read_text()
                self.assertIn("external install <-m> <755> <--> "
                              "<source with spaces> <destination with spaces>", calls)
                self.assertEqual("sudo install" in calls, not root_user)

    def test_install_finishes_once_for_root_and_sudo_users(self):
        self.provision_env()
        original_env = (self.project / ".env").read_bytes()
        for root_user in (True, False):
            with self.subTest(root_user=root_user):
                self.select_privilege_branch(root_user=root_user)
                launchers = self.base / f"launchers {root_user}" / "bin"
                self.env["GROUPGUARD_BIN_DIR"] = str(launchers)
                result = self.run_manager("install")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.count("一键安装 / 修复安装"), 1)
                self.assertEqual(result.stdout.count("已设置快捷命令"), 1)
                self.assertIn("部署命令完成", result.stdout)
                self.assertEqual((self.project / ".env").read_bytes(), original_env)
                self.assertNotIn(self.token, result.stdout + result.stderr)
                calls = self.log.read_text().splitlines()
                self.assertEqual(calls.count("docker compose up -d --build groupbot"), 1)
                for name in ("gg", "GG"):
                    launcher = launchers / name
                    self.assertEqual(launcher.stat().st_mode & 0o777, 0o755)
                    status = subprocess.run([str(launcher), "status"],
                        cwd=self.base / "home", env=self.env, text=True,
                        capture_output=True, timeout=10)
                    self.assertEqual(status.returncode, 0, status.stderr)
                    self.assertIn("服务状态", status.stdout)

    def test_shortcuts_repair_without_install_or_docker_for_both_users(self):
        for root_user in (True, False):
            with self.subTest(root_user=root_user):
                self.select_privilege_branch(root_user=root_user)
                launchers = self.base / f"repair {root_user}" / "bin"
                self.env["GROUPGUARD_BIN_DIR"] = str(launchers)
                for _ in range(2):
                    result = self.run_manager("shortcuts")
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertIn("已设置快捷命令", result.stdout)
                    self.assertNotIn("一键安装 / 修复安装", result.stdout)
                self.assertFalse((self.project / ".env").exists())
                self.assertNotIn("docker", self.log.read_text() if self.log.exists() else "")
                result = subprocess.run([str(launchers / "GG"), "help"],
                    cwd=self.base / "home", env=self.env, text=True,
                    capture_output=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(str(self.project / "scripts/server-manager.sh"), result.stdout)

    def test_help_does_not_mutate_system(self):
        result = self.run_manager("help")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("一键管理", result.stdout)
        self.assertFalse((self.project / ".env").exists())
        self.assertFalse(self.log.exists())

    def test_help_uses_absolute_paths_from_unrelated_working_directory(self):
        script = self.project / "scripts/server-manager.sh"
        result = subprocess.run(
            ["bash", str(script), "help"], cwd=self.base / "home",
            env=self.env, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f'bash "{script}" logs', result.stdout)
        self.assertIn(f'bash "{script}" update', result.stdout)
        self.assertNotIn("  bash scripts/server-manager.sh", result.stdout)

    def test_install_log_hint_uses_absolute_path_from_home(self):
        self.provision_env()
        script = self.project / "scripts/server-manager.sh"
        result = subprocess.run(
            ["bash", str(script), "install"], cwd=self.base / "home",
            env=self.env, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f'bash "{script}" logs', result.stderr)
        self.assertNotIn("：bash scripts/server-manager.sh logs", result.stderr)

    def test_logs_follow_hint_uses_absolute_path_from_home(self):
        script = self.project / "scripts/server-manager.sh"
        result = subprocess.run(
            ["bash", str(script), "logs"], cwd=self.base / "home",
            env=self.env, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f'bash "{script}" logs-follow', result.stdout)

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

    def test_existing_docker_without_compose_v2_installs_plugin_only(self):
        self.provision_env()
        self.fake_bin("docker", """#!/usr/bin/env bash
set -eu
printf 'docker %s\\n' "$*" >> "$GROUPGUARD_MOCK_LOG"
if [[ "$1" == "compose" && "$2" == "version" ]]; then
  test -f "$HOME/compose-installed"
fi
""")
        self.fake_bin("apt-get", """#!/usr/bin/env bash
set -eu
printf 'apt-get %s\\n' "$*" >> "$GROUPGUARD_MOCK_LOG"
[[ "$*" == *"--no-remove install docker-compose-plugin"* ]] || exit 51
touch "$HOME/compose-installed"
""")
        result = self.run_source(
            "check_os() { :; }\ninstall_docker\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.log.read_text()
        self.assertIn("apt-get -y --no-remove install docker-compose-plugin", calls)
        self.assertNotIn("official-curl", calls)
        self.assertNotIn("docker-ce docker-ce-cli", calls)

    def test_existing_docker_plugin_install_failure_does_not_replace_engine(self):
        self.provision_env()
        self.fake_bin("docker", """#!/usr/bin/env bash
if [[ "$1" == "compose" && "$2" == "version" ]]; then exit 1; fi
exit 0
""")
        self.fake_bin("apt-get", """#!/usr/bin/env bash
printf 'apt-get %s\\n' "$*" >> "$GROUPGUARD_MOCK_LOG"
exit 100
""")
        result = self.run_source(
            "check_os() { :; }\ninstall_docker\n")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("补装 Docker Compose V2", result.stderr)
        self.assertIn("退出码 100", result.stderr)
        self.assertNotIn("docker-ce docker-ce-cli", self.log.read_text())

    def test_official_download_fails_fast_without_running_shell(self):
        self.provision_env()
        self.fake_docker_installer_download(exit_code=60)
        result = self.run_source("""
docker_available() { return 1; }
docker_daemon_binary_present() { return 1; }
check_os() { :; }
root() { echo UNEXPECTED_ROOT >> "$GROUPGUARD_MOCK_LOG"; }
install_docker
echo 'THIS MUST NOT PRINT'
""")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("下载 Docker 官方安装脚本", result.stderr)
        self.assertIn("退出码 60", result.stderr)
        self.assertNotIn("UNEXPECTED_ROOT", self.log.read_text())
        self.assertNotIn("THIS MUST NOT PRINT", result.stdout)
        self.assertEqual(list((self.base / "tmp").iterdir()), [])

    def test_official_download_empty_does_not_run(self):
        self.fake_docker_installer_download(empty=True)
        result = self.run_source("""
docker_available() { return 1; }
docker_daemon_binary_present() { return 1; }
check_os() { :; }
root() { echo UNEXPECTED_ROOT >> "$GROUPGUARD_MOCK_LOG"; }
install_docker
""")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Docker 脚本为空", result.stderr)
        self.assertNotIn("UNEXPECTED_ROOT", self.log.read_text())
        self.assertEqual(list((self.base / "tmp").iterdir()), [])

    def test_official_installer_exit_error_stops_without_compose_or_bot(self):
        self.fake_docker_installer_download()
        result = self.run_source("""
docker_available() { return 1; }
docker_daemon_binary_present() { return 1; }
check_os() { :; }
root() {
  printf 'root %s\\n' "$*" >> "$GROUPGUARD_MOCK_LOG"
  if [[ "$1" == "sh" ]]; then return 100; fi
}
install_docker
echo 'THIS MUST NOT PRINT'
""")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("执行 Docker 官方快捷安装脚本", result.stderr)
        self.assertIn("退出码 100", result.stderr)
        self.assertNotIn("docker compose up", self.log.read_text())
        self.assertEqual(list((self.base / "tmp").iterdir()), [])
        self.assertNotIn("THIS MUST NOT PRINT", result.stdout)

    def test_docker_daemon_detected_without_client_is_not_reinstalled(self):
        self.fake_docker_installer_download()
        result = self.run_source("""
docker_available() { return 1; }
docker_daemon_binary_present() { return 0; }
check_os() { :; }
install_docker
""")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("检测到已有 dockerd", result.stderr)
        self.assertFalse(self.log.exists())

    def test_docker_official_install_simulated_success_and_temp_cleanup(self):
        self.fake_docker_installer_download()
        result = self.run_source("""
docker_available() { [[ -f "$HOME/docker-installed" ]]; }
docker_daemon_binary_present() { return 1; }
check_os() { :; }
root() {
  printf 'root %s\\n' "$*" >> "$GROUPGUARD_MOCK_LOG"
  if [[ "$1" == "sh" ]]; then
    touch "$HOME/docker-installed"
  fi
  return 0
}
install_docker
""")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("下载 Docker 官方安装脚本",
                      result.stdout + result.stderr)
        self.assertIn("执行 Docker 官方快捷安装脚本",
                      result.stdout + result.stderr)
        self.assertIn("Docker Engine、Compose V2 和守护进程检查通过",
                      result.stdout)
        self.assertIn("curl --proto =https --tlsv1.2", self.log.read_text())
        self.assertIn("root sh ", self.log.read_text())
        self.assertEqual(list((self.base / "tmp").iterdir()), [])

    def test_menu_launches_install_in_isolated_bash(self):
        source = (PROJECT / "scripts/server-manager.sh").read_text()
        self.assertIn('bash "${APP_DIR}/scripts/server-manager.sh" install', source)
        self.assertNotIn('1) (install) || warn', source)

    def test_migration_prepare_never_configures_a_token_or_starts_the_bot(self):
        result = self.run_source('''
install_docker() { printf 'DOCKER_READY\\n'; }
migration_tools() { printf 'TOOLS_READY\\n'; }
install_shortcuts() { printf 'SHORTCUTS_READY\\n'; }
ensure_env() { echo UNEXPECTED_TOKEN_PROMPT; return 99; }
dc() { echo UNEXPECTED_CONTAINER_START; return 99; }
prepare
''')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("DOCKER_READY", result.stdout)
        self.assertIn("SHORTCUTS_READY", result.stdout)
        self.assertNotIn("UNEXPECTED", result.stdout)
        self.assertFalse((self.project/".env").exists())

    def test_local_installer_forwards_prepare_and_compatibility_entry_preserves_arguments(self):
        (self.project/"scripts/server-manager.sh").write_text('''#!/usr/bin/env bash
printf 'MODE=%s\\n' "$1"
''')
        for entry in ("install.sh", "scripts/server-bootstrap.sh"):
            result = subprocess.run(["bash", entry, "prepare"], cwd=self.project, env=self.env,
                                    text=True, capture_output=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("MODE=prepare", result.stdout)

    def test_migration_commands_keep_file_paths_with_spaces_as_single_arguments(self):
        result = self.run_source('''
migration_tools() { :; }
python3() { printf '<%s>\\n' "$@"; }
migrate_action import '/tmp/backup with spaces.ggm'
''')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("<import>\n</tmp/backup with spaces.ggm>", result.stdout)

    def test_migration_menu_is_discoverable(self):
        result = self.run_source('tty_line() { REPLY=0; }\nmenu\n')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("8. 服务器迁移", result.stdout)

    def test_public_prepare_clones_without_token_configuration_or_container_start(self):
        self.env["GROUPGUARD_MOCK_SOURCE"] = str(self.project)
        self.fake_bin("git", '''#!/usr/bin/env bash
set -eu
[[ "$1" == clone ]] || exit 3
dest="${@: -1}"
mkdir -p "$dest/.git"
cp -R "$GROUPGUARD_MOCK_SOURCE"/. "$dest/"
''')
        result = subprocess.run(["bash", "-s", "--", "prepare"],
            input=(self.project/"install.sh").read_text(), cwd=self.base/"home", env=self.env,
            text=True, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        installed = self.base/"home/groupguard"
        self.assertTrue((self.base/"bin/gg").exists())
        self.assertFalse((installed/".env").exists())
        self.assertNotIn("compose up", self.log.read_text())
        self.assertIn("环境准备完成", result.stdout)

    def test_compact_menu_has_no_ocr_dashboard_or_manual_backup(self):
        result = self.run_source('tty_line() { REPLY=0; }\nmenu\n')
        self.assertEqual(result.returncode, 0, result.stderr)
        for text in ("2. 更新机器人", "gg update", "6. 启动 / 停止 / 重启服务", "7. 卸载"):
            self.assertIn(text, result.stdout)
        for text in ("OCR 图片识别开关", "只读网页看板", "8. 备份数据", "9.", "10."):
            self.assertNotIn(text, result.stdout)
        self.assertFalse(self.log.exists())

    def test_menu_update_runs_update_then_executes_new_menu(self):
        result = self.run_source("""
tty_line() { REPLY=2; }
bash() {
  [[ "$1" == "$APP_DIR/scripts/server-manager.sh" && "$2" == update ]] || return 7
  printf 'UPDATE_DISPATCHED\\n'
  cat > scripts/server-manager.sh <<'NEW_MANAGER'
#!/usr/bin/env bash
printf 'RELOADED:%s\\n' "$1"
NEW_MANAGER
}
menu
""")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("UPDATE_DISPATCHED", result.stdout)
        self.assertIn("RELOADED:menu", result.stdout)

    def test_menu_update_failure_keeps_menu_available(self):
        result = self.run_source("""
step=0
tty_line() { if (( step == 0 )); then REPLY=2; else REPLY=0; fi; step=$((step+1)); }
pause() { :; }
bash() { return 42; }
menu
""")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("更新未成功", result.stderr)
        self.assertEqual(result.stdout.count("2. 更新机器人"), 2)

    def test_shortcuts_work_from_other_directory_and_preserve_arguments(self):
        self.provision_env()
        result = self.run_manager("shortcuts")
        self.assertEqual(result.returncode, 0, result.stderr)
        for name in ("gg", "GG"):
            launcher = self.base / "bin" / name
            self.assertEqual(launcher.stat().st_mode & 0o777, 0o755)
            result = subprocess.run([str(launcher), "status"], cwd=self.base / "home",
                env=self.env, text=True, capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("服务状态", result.stdout)
        self.assertEqual(sum(line.startswith("docker compose --profile dashboard ps")
                             for line in self.log.read_text().splitlines()), 2)
        result = subprocess.run([str(launcher), "unknown argument"], cwd=self.base / "home",
            env=self.env, text=True, capture_output=True, timeout=10)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("未知操作：unknown argument", result.stderr)

    def test_shortcut_registration_is_repeatable_with_spaces_in_paths(self):
        self.project = self.project.rename(self.base / "app with spaces")
        self.env["GROUPGUARD_BIN_DIR"] = str(self.base / "launchers with spaces")
        for _ in range(2):
            result = self.run_manager("shortcuts")
            self.assertEqual(result.returncode, 0, result.stderr)
        result = subprocess.run([str(self.base / "launchers with spaces/GG"), "help"],
            cwd=self.base / "home", env=self.env, text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(str(self.project / "scripts/server-manager.sh"), result.stdout)
        self.assertFalse((self.project / ".env").exists())

    def test_shortcuts_never_overwrite_unrelated_commands_or_symlinks(self):
        command = self.base / "bin/GG"
        command.write_text("other program\n")
        result = self.run_manager("shortcuts")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("不会覆盖", result.stderr)
        self.assertEqual(command.read_text(), "other program\n")
        self.assertFalse((self.base / "bin/gg").exists())
        command.unlink()
        target = self.base / "another-tool"
        target.write_text("# GroupGuard managed launcher\n")
        command.symlink_to(target)
        result = self.run_manager("shortcuts")
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(command.is_symlink())
        self.assertFalse((self.base / "bin/gg").exists())

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
[[ "$*" == *"raw.githubusercontent.com/slobys/telegram-group-manager/main/install.sh"* ]] || exit 3
cat "$GROUPGUARD_MOCK_SOURCE/install.sh"
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
            if line.startswith("bash -o pipefail -c 'curl"))
        result = subprocess.run(
            ["bash", "-c", command], cwd=self.project, env=self.env,
            text=True, capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        actions = self.log.read_text()
        self.assertIn("curl -fsSL https://raw.githubusercontent.com/slobys/telegram-group-manager", actions)
        self.assertIn("git clone --depth 1 --branch main https://github.com/slobys/telegram-group-manager.git", actions)
        self.assertIn("docker compose up -d --build groupbot", actions)
        self.assertTrue((self.base / "bin/gg").exists())
        self.assertTrue((self.base / "bin/GG").exists())
        self.assertNotIn("gh auth", actions)
        self.assertTrue((self.base / "home/groupguard/.env").exists())
        self.assertNotIn(self.token, result.stdout + result.stderr)

    def test_public_and_docs_commands_are_identical(self):
        readme = next(
            line for line in (PROJECT / "README.md").read_text().splitlines()
            if line.startswith("bash -o pipefail -c 'curl"))
        docs = next(
            line for line in (PROJECT / "docs/server-one-click.md").read_text().splitlines()
            if line.startswith("bash -o pipefail -c 'curl"))
        self.assertEqual(readme, docs)
        self.assertLess(len(readme), 130)
        self.assertNotIn("gh auth", readme)
        self.assertIn("raw.githubusercontent.com/slobys/telegram-group-manager", readme)

    def test_public_one_command_bootstraps_without_git(self):
        """Simulate Debian SSH with curl/coreutils present but no Git."""
        self.env["GROUPGUARD_MOCK_SOURCE"] = str(self.project)
        self.env["GROUPGUARD_MOCK_TOKEN"] = self.token
        self.env["GROUPGUARD_MOCK_BIN"] = str(self.base / "bin")
        self.env["PATH"] = str(self.base / "bin")
        # Provide core utilities, deliberately NOT Git.
        for name in ("bash", "uname", "dirname", "pwd", "cat", "cp",
                     "chmod", "mkdir", "grep", "id", "mktemp", "install", "rm"):
            target = shutil.which(name)
            self.assertIsNotNone(target)
            (self.base / "bin" / name).symlink_to(target)
        self.fake_bin("curl", """#!/usr/bin/env bash
set -eu
printf 'curl %s\\n' "$*" >> "$GROUPGUARD_MOCK_LOG"
cat "$GROUPGUARD_MOCK_SOURCE/install.sh"
""")
        self.fake_bin("apt-get", """#!/usr/bin/env bash
set -eu
printf 'apt-get %s\\n' "$*" >> "$GROUPGUARD_MOCK_LOG"
[[ "$1" == "install" ]] || exit 0
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
            if line.startswith("bash -o pipefail -c 'curl"))
        result = subprocess.run(
            [str(self.base / "bin/bash"), "-c", command],
            cwd=self.project, env=self.env, text=True,
            capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.log.read_text()
        self.assertIn("apt-get install -y git ca-certificates", calls)
        self.assertIn("git clone --depth 1 --branch main https://github.com/slobys/telegram-group-manager.git", calls)
        self.assertIn("docker compose up -d --build groupbot", calls)
        self.assertNotIn("gh auth", calls)
        self.assertNotIn(self.token, result.stdout + result.stderr)

    def test_public_download_failure_is_not_reported_as_success(self):
        command = next(line for line in (PROJECT / "README.md").read_text().splitlines()
                       if line.startswith("bash -o pipefail -c 'curl"))
        self.fake_bin("curl", "#!/usr/bin/env bash\nexit 22\n")
        result = subprocess.run(["bash", "-c", command], cwd=self.base / "home",
            env=self.env, text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 22)
        self.assertFalse((self.base / "home/groupguard").exists())
        self.assertFalse(self.log.exists())
        empty_bin = self.base / "empty-bin"
        empty_bin.mkdir()
        (empty_bin / "bash").symlink_to(shutil.which("bash"))
        result = subprocess.run([shutil.which("bash"), "-c", command], cwd=self.base / "home",
            env={**self.env, "PATH": str(empty_bin)}, text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 127)
        self.assertIn("curl", result.stderr)
        self.assertFalse((self.base / "home/groupguard").exists())

    def test_local_install_retains_repair_behavior(self):
        self.provision_env()
        result = subprocess.run(["bash", str(self.project / "install.sh")],
            cwd=self.base / "home", env=self.env, text=True, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("一键安装 / 修复安装", result.stdout)
        self.assertFalse((self.base / "home/groupguard").exists())
        self.assertTrue((self.base / "bin/gg").exists())

    def test_online_entry_upgrades_old_install_then_registers_shortcuts(self):
        installed = self.base / "home/groupguard"
        shutil.copytree(self.project, installed)
        (installed / ".git").mkdir()
        (installed / ".env").write_text(f"BOT_TOKEN={self.token}\n")
        old_manager = installed / "scripts/server-manager.sh"
        old_manager.write_text("""#!/usr/bin/env bash
set -eu
[[ "$1" == update ]] || exit 8
printf 'OLD_MANAGER_UPDATE\\n' >> "$GROUPGUARD_MOCK_LOG"
cp "$GROUPGUARD_MOCK_SOURCE/scripts/server-manager.sh" "$0.new"
mv -f -- "$0.new" "$0"
""")
        self.env["GROUPGUARD_MOCK_SOURCE"] = str(self.project)
        self.fake_bin("git", "#!/usr/bin/env bash\necho https://github.com/slobys/telegram-group-manager.git\n")
        # Feed the downloaded entry through stdin exactly as the public command.
        result = subprocess.run(["bash"], input=(self.project / "install.sh").read_text(),
            cwd=self.base / "home", env=self.env, text=True, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("OLD_MANAGER_UPDATE", self.log.read_text())
        self.assertTrue((self.base / "bin/gg").exists())
        self.assertTrue((self.base / "bin/GG").exists())
        self.assertIn(self.token, (installed / ".env").read_text())
        self.assertNotIn(self.token, result.stdout + result.stderr)

    def test_original_bootstrap_url_still_delegates_to_public_entry(self):
        self.env["GROUPGUARD_MOCK_SOURCE"] = str(self.project)
        self.fake_bin("curl", """#!/usr/bin/env bash
set -eu
[[ "$*" == *"/main/install.sh"* ]] || exit 3
while [[ "$1" != -o ]]; do shift; done
cp "$GROUPGUARD_MOCK_SOURCE/install.sh" "$2"
""")
        self.fake_bin("git", """#!/usr/bin/env bash
set -eu
[[ "$1" == clone ]] || exit 3
dest="${@: -1}"
mkdir -p "$dest"
cp -R "$GROUPGUARD_MOCK_SOURCE"/. "$dest/"
printf 'BOT_TOKEN=%s\\n' "$GROUPGUARD_MOCK_TOKEN" > "$dest/.env"
""")
        self.env["GROUPGUARD_MOCK_TOKEN"] = self.token
        result = subprocess.run(["bash"], input=(self.project / "scripts/server-bootstrap.sh").read_text(),
            cwd=self.base / "home", env=self.env, text=True, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.base / "bin/GG").exists())
        self.assertIn("docker compose up -d --build groupbot", self.log.read_text())


if __name__ == "__main__":
    unittest.main()
