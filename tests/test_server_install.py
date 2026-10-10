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

    def test_update_backs_up_before_private_git_pull(self):
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

    def test_readme_one_command_private_repo_bootstrap_offline(self):
        # Exact command displayed to users; all GitHub and Docker actions mocked.
        self.env["GROUPGUARD_MOCK_SOURCE"] = str(self.project)
        self.env["GROUPGUARD_MOCK_TOKEN"] = self.token
        self.fake_bin("gh", """#!/usr/bin/env bash
set -eu
printf 'gh %s\\n' "$*" >> "$GROUPGUARD_MOCK_LOG"
if [[ "$1" == "auth" ]]; then
  exit 0
elif [[ "$1" == "api" ]]; then
  cat "$GROUPGUARD_MOCK_SOURCE/scripts/server-bootstrap.sh"
elif [[ "$1" == "repo" && "$2" == "clone" ]]; then
  dest="${@: -1}"
  mkdir -p "$dest"
  cp -R "$GROUPGUARD_MOCK_SOURCE"/. "$dest/"
  printf 'BOT_TOKEN=%s\\n' "$GROUPGUARD_MOCK_TOKEN" > "$dest/.env"
  chmod 600 "$dest/.env"
else
  exit 3
fi
""")
        command = next(
            line for line in (PROJECT / "README.md").read_text().splitlines()
            if line.startswith("bash -c 'set -euo pipefail;"))
        result = subprocess.run(
            ["bash", "-c", command], cwd=self.project, env=self.env,
            text=True, capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        actions = self.log.read_text()
        self.assertIn("gh api -H Accept: application/vnd.github.raw+json", actions)
        self.assertIn("gh repo clone slobys/telegram-group-manager", actions)
        self.assertIn("docker compose up -d --build groupbot", actions)
        self.assertTrue((self.base / "home/groupguard/.env").exists())
        self.assertNotIn(self.token, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
