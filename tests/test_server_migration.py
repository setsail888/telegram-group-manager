"""Real SQLite/encryption with isolated fake Docker volumes and failure injection."""
from contextlib import closing, redirect_stdout
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tarfile
import tempfile
import time
import unittest
from unittest.mock import patch

from app.economy import Economy
from app.store import Store


PROJECT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("gg_migration", PROJECT/"scripts/server-migrate.py")
migration = importlib.util.module_from_spec(spec)
spec.loader.exec_module(migration)
PASSWORD = "test-migration-password-2026"
TOKEN = "123456789:"+"A"*35


def config(token=TOKEN):
    return (f"BOT_TOKEN={token}\nDB_PATH=/app/data/bot.sqlite3\nTZ=Asia/Shanghai\n"
            "WITH_OCR=0\nOCR_ENABLED=0\nOWNER_IDS=1,2\n").encode()


def seed(data, cid=-1001, points=123):
    data.mkdir(parents=True, exist_ok=True)
    store = Store(str(data/"bot.sqlite3"))
    store.ensure_chat(cid, "迁移测试群")
    store.set(cid, "ocr_mode", "review")
    store.set_ui_language(1, "fr")
    store.set_ui_language(cid, "en")
    store.add_filter(cid, "word", "违禁词测试", "违禁词测试")
    store.change_points(cid, 2, points, 1)
    store.set(cid, "points_enabled", True)
    economy = Economy(store)
    economy.add_item(cid, 1, "迁移奖励", "保留订单", 10, 5)
    item = economy.items(cid)[0]
    economy.purchase(cid, 2, item["id"], 50)
    store.save_offset(987654)
    store.close()
    return data


class FakeDeployment:
    def __init__(self, project, data, running=True, fail=None):
        self.project, self.data = project, data
        self.states = {"groupbot": running, "dashboard": running}
        self.calls, self.fail = [], fail
        self.active_config = (project/".env").read_bytes() if (project/".env").exists() else None

    def running(self):
        return dict(self.states)

    def stop(self):
        self.calls.append("stop")
        self.states = dict.fromkeys(self.states, False)

    def resume(self, states):
        self.calls.append("resume")
        self.states.update(states)

    def ensure_container(self):
        self.calls.append("ensure")

    def copy_data(self, destination):
        self.calls.append("copy")
        shutil.copytree(self.data, destination)

    def dc(self, *args):
        self.calls.append(args)
        if args[:2] == ("create", "--force-recreate"):
            self.active_config = (self.project/".env").read_bytes()

    def replace_data(self, incoming):
        self.calls.append("replace")
        # Only the test's named temporary volume can be recursively replaced.
        assert self.data.resolve().parent == self.project.resolve().parent
        shutil.rmtree(self.data)
        shutil.copytree(incoming, self.data)
        if self.fail == "replace":
            self.fail = None
            raise migration.MigrationError("injected copy failure")
        if self.fail == "verify":
            self.fail = None
            (self.data/"extra.txt").write_text("changed")

    def start_import(self):
        self.calls.append("start")
        self.active_config = (self.project/".env").read_bytes()
        self.states["groupbot"] = True
        if self.fail == "start":
            self.fail = None
            raise migration.MigrationError("injected startup failure after recreate")


@unittest.skipUnless(shutil.which("openssl"), "OpenSSL is needed for real package encryption")
class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.project = self.root/"project"
        self.project.mkdir()
        shutil.copy2(PROJECT/"config.sample", self.project/"config.sample")
        (self.project/".env").write_bytes(config())
        self.data = seed(self.root/"volume")
        (self.data/"extra.txt").write_text("额外持久化文件", encoding="utf-8")
        self.env = patch.dict(os.environ, {"GROUPGUARD_BACKUPS": str(self.root/"backups")})
        self.env.start()
        self.output = io.StringIO()
        self.quiet = redirect_stdout(self.output)
        self.quiet.__enter__()

    def tearDown(self):
        self.quiet.__exit__(None, None, None)
        self.env.stop()
        self.temporary.cleanup()

    def package(self):
        package = self.root/"export.ggm"
        migration.export_data(self.project, FakeDeployment(self.project, self.data), package, PASSWORD)
        return package

    def payload(self):
        work = self.root/"payload-work"
        work.mkdir()
        data = self.root/"copy"
        shutil.copytree(self.data, data)
        return migration.make_payload(data, config(), work)[0]

    def target(self, populated=True, config_exists=True, fail=None):
        project = self.root/"target"
        project.mkdir()
        shutil.copy2(PROJECT/"config.sample", project/"config.sample")
        if config_exists:
            (project/".env").write_bytes(config("987654321:"+"B"*35))
        data = self.root/"target-volume"
        seed(data, cid=-2001, points=88) if populated else data.mkdir()
        return project, FakeDeployment(project, data, running=populated, fail=fail)

    def assert_restored(self, project, deployment, old_config):
        self.assertEqual((project/".env").read_bytes(), old_config)
        self.assertEqual(deployment.active_config, old_config)
        self.assertTrue(deployment.states["groupbot"])
        with closing(sqlite3.connect(deployment.data/"bot.sqlite3")) as db:
            self.assertEqual(db.execute("SELECT chat_id FROM chats").fetchall(), [(-2001,)])
            self.assertEqual(db.execute("SELECT balance FROM point_balances").fetchone()[0], 78)

    def test_complete_roundtrip_preserves_settings_keywords_points_orders_languages_and_offset(self):
        package = self.package()
        project, deployment = self.target()
        migration.import_data(project, deployment, package, PASSWORD, confirm=lambda _: "MIGRATE")
        self.assertEqual((project/".env").read_bytes(), config())
        self.assertTrue(deployment.states["groupbot"])
        self.assertFalse(deployment.states["dashboard"])
        store = Store(str(deployment.data/"bot.sqlite3"))
        try:
            self.assertEqual(store.chat(-1001)["title"], "迁移测试群")
            self.assertIsNone(store.chat(-2001))
            self.assertEqual(store.settings(-1001)["ocr_mode"], "review")
            self.assertEqual(store.ui_language(1), "fr")
            self.assertEqual(store.ui_language(-1001), "en")
            self.assertEqual(store.filter_count(-1001, "word"), 1)
            self.assertEqual(store.point_balance(-1001, 2)["balance"], 113)
            self.assertEqual(len(Economy(store).orders(-1001)), 1)
            self.assertEqual(store.offset(), 987654)
        finally:
            store.close()
        self.assertEqual((deployment.data/"extra.txt").read_text(encoding="utf-8"), "额外持久化文件")
        self.assertNotIn(TOKEN, self.output.getvalue())
        self.assertNotIn(TOKEN.encode(), package.read_bytes())
        self.assertEqual(len(list((self.root/"backups").glob("*before-import*.ggm"))), 1)

    def test_successful_export_keeps_source_stopped_and_original_data_unchanged(self):
        original = (self.data/"bot.sqlite3").read_bytes()
        deployment = FakeDeployment(self.project, self.data)
        migration.export_data(self.project, deployment, self.root/"ok.ggm", PASSWORD)
        self.assertFalse(any(deployment.states.values()))
        self.assertEqual((self.data/"bot.sqlite3").read_bytes(), original)
        self.assertNotIn("resume", deployment.calls)

    def test_failed_export_restores_original_service_state(self):
        deployment = FakeDeployment(self.project, self.data)
        with patch.object(migration, "encrypt_package", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                migration.export_data(self.project, deployment, self.root/"bad.ggm", PASSWORD)
        self.assertTrue(all(deployment.states.values()))
        self.assertFalse((self.root/"bad.ggm").exists())

    def test_fresh_target_does_not_start_until_after_data_is_imported(self):
        package = self.package()
        project, deployment = self.target(populated=False, config_exists=False)
        migration.import_data(project, deployment, package, PASSWORD, confirm=lambda _: "MIGRATE")
        self.assertLess(deployment.calls.index("replace"), deployment.calls.index("start"))
        self.assertEqual((project/".env").read_bytes(), config())
        self.assertTrue((deployment.data/"bot.sqlite3").exists())

    def test_wrong_password_leaves_target_untouched(self):
        package = self.package()
        project, deployment = self.target()
        old = (project/".env").read_bytes()
        with self.assertRaisesRegex(migration.MigrationError, "密码错误"):
            migration.import_data(project, deployment, package, "incorrect-password-123", confirm=lambda _: "MIGRATE")
        self.assertFalse(deployment.calls)
        self.assert_restored(project, deployment, old)

    def test_cancelled_import_leaves_configuration_data_and_running_state_untouched(self):
        package = self.package()
        project, deployment = self.target()
        old = (project/".env").read_bytes()
        with self.assertRaisesRegex(migration.MigrationError, "已取消"):
            migration.import_data(project, deployment, package, PASSWORD, confirm=lambda _: "no")
        self.assertFalse(deployment.calls)
        self.assert_restored(project, deployment, old)

    def test_copy_failure_restores_original_data_and_config(self):
        package = self.package()
        project, deployment = self.target(fail="replace")
        old = (project/".env").read_bytes()
        with self.assertRaisesRegex(migration.MigrationError, "copy failure"):
            migration.import_data(project, deployment, package, PASSWORD, confirm=lambda _: "MIGRATE")
        self.assert_restored(project, deployment, old)

    def test_start_failure_recreates_original_container_environment_before_restarting(self):
        package = self.package()
        project, deployment = self.target(fail="start")
        old = (project/".env").read_bytes()
        with self.assertRaisesRegex(migration.MigrationError, "startup failure"):
            migration.import_data(project, deployment, package, PASSWORD, confirm=lambda _: "MIGRATE")
        self.assert_restored(project, deployment, old)
        self.assertIn(("create", "--force-recreate", "groupbot"), deployment.calls)

    def test_volume_copy_verification_failure_does_not_start_import_and_rolls_back(self):
        package = self.package()
        project, deployment = self.target(fail="verify")
        old = (project/".env").read_bytes()
        with self.assertRaisesRegex(migration.MigrationError, "文件校验失败"):
            migration.import_data(project, deployment, package, PASSWORD, confirm=lambda _: "MIGRATE")
        self.assertNotIn("start", deployment.calls)
        self.assert_restored(project, deployment, old)

    def test_orphan_history_without_config_is_not_overwritten(self):
        package = self.package()
        project, deployment = self.target(config_exists=False)
        original = (deployment.data/"bot.sqlite3").read_bytes()
        with self.assertRaisesRegex(migration.MigrationError, "缺少原.env"):
            migration.import_data(project, deployment, package, PASSWORD, confirm=lambda _: "MIGRATE")
        self.assertEqual((deployment.data/"bot.sqlite3").read_bytes(), original)
        self.assertFalse((project/".env").exists())
        self.assertTrue(deployment.states["groupbot"])

    def test_tampering_header_ciphertext_or_tag_is_rejected_before_decryption(self):
        package = self.package()
        original = package.read_bytes()
        for n, position in enumerate((len(migration.MAGIC)+1, len(original)//2, len(original)-1)):
            tampered = bytearray(original)
            tampered[position] ^= 1
            package.write_bytes(tampered)
            work = self.root/f"tampered-{n}"
            work.mkdir()
            with self.assertRaisesRegex(migration.MigrationError, "密码错误"):
                migration.decrypt_package(package, work, PASSWORD)
            self.assertFalse((work/"decrypted.tar.gz").exists())

    def test_existing_package_is_never_overwritten(self):
        package = self.package()
        original = package.read_bytes()
        with self.assertRaisesRegex(migration.MigrationError, "不会覆盖"):
            migration.export_data(self.project, FakeDeployment(self.project, self.data), package, PASSWORD)
        self.assertEqual(package.read_bytes(), original)

    def test_wal_commits_are_included_in_snapshot(self):
        live = self.root/"live"
        live.mkdir()
        store = Store(str(live/"bot.sqlite3"))
        try:
            store.ensure_chat(-3456, "WAL中的最新群")
            store.change_points(-3456, 3, 777, 1)
            self.assertTrue((live/"bot.sqlite3-wal").stat().st_size)
            copied = self.root/"wal-copy"
            shutil.copytree(live, copied)
            work = self.root/"wal-work"
            work.mkdir()
            migration.make_payload(copied, config(), work)
            with closing(sqlite3.connect(copied/"bot.sqlite3")) as db:
                self.assertEqual(db.execute("SELECT balance FROM point_balances").fetchone()[0], 777)
            self.assertFalse((copied/"bot.sqlite3-wal").exists())
        finally:
            store.close()

    def test_unsafe_archive_entries_are_rejected_without_writing_outside_destination(self):
        for index, (name, kind) in enumerate((("../escape", tarfile.REGTYPE), ("/absolute", tarfile.REGTYPE),
                                             ("data/link", tarfile.SYMTYPE), ("data/hard", tarfile.LNKTYPE),
                                             ("data/../escape", tarfile.REGTYPE), ("unknown.txt", tarfile.REGTYPE))):
            archive = self.root/f"bad-{index}.tar.gz"
            with tarfile.open(archive, "w:gz") as tar:
                entry = tarfile.TarInfo(name)
                entry.type, entry.linkname = kind, "../../escape"
                tar.addfile(entry, io.BytesIO())
            with self.assertRaises(migration.MigrationError):
                migration.unpack_payload(archive, self.root/f"extract-{index}")
        self.assertFalse((self.root/"escape").exists())

    def test_duplicate_archive_entries_are_rejected(self):
        archive = self.root/"duplicate.tar.gz"
        with tarfile.open(archive, "w:gz") as tar:
            for _ in range(2):
                tar.addfile(tarfile.TarInfo("manifest.json"), io.BytesIO())
        with self.assertRaisesRegex(migration.MigrationError, "重复"):
            migration.unpack_payload(archive, self.root/"extract")

    def test_failed_target_backup_does_not_overwrite_target_and_restores_running_state(self):
        package = self.package()
        project, deployment = self.target()
        old = (project/".env").read_bytes()
        with patch.object(migration, "encrypt_package", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                migration.import_data(project, deployment, package, PASSWORD, confirm=lambda _: "MIGRATE")
        self.assertNotIn("replace", deployment.calls)
        self.assert_restored(project, deployment, old)

    def test_modified_payload_hash_is_rejected(self):
        payload = self.payload()
        modified = self.root/"modified.tar.gz"
        with tarfile.open(payload, "r:gz") as source, tarfile.open(modified, "w:gz") as target:
            for member in source:
                body = source.extractfile(member).read()
                if member.name == "data/extra.txt":
                    body += b"changed"
                member.size = len(body)
                target.addfile(member, io.BytesIO(body))
        with self.assertRaisesRegex(migration.MigrationError, "文件校验失败"):
            migration.unpack_payload(modified, self.root/"bad-hash")

    @unittest.skipIf(os.name == "nt", "flock is a Linux server facility")
    def test_parallel_migration_is_rejected(self):
        with migration.migration_lock(self.project):
            with self.assertRaisesRegex(migration.MigrationError, "已有迁移任务"):
                with migration.migration_lock(self.project):
                    self.fail("second lock acquired")


class MigrationValidationTests(unittest.TestCase):
    @unittest.skipIf(os.name == "nt", "PTY input is a Linux server facility")
    def test_real_tty_confirmation_and_hidden_password_prompts(self):
        import pty
        import select
        import signal
        pid, fd = pty.fork()
        if pid == 0:
            try:
                assert migration.ask("CONFIRM:") == "MIGRATE"
                assert migration.ask_password(confirm=True) == PASSWORD
                os.write(1, b"\nTTY_FLOW_OK\n")
                os._exit(0)
            except Exception:
                os._exit(1)
        output, step, ended = b"", 0, False
        prompts = [(b"CONFIRM:", b"MIGRATE\n"),
                   ("迁移包密码（至少12字符，不显示）：".encode(), PASSWORD.encode()+b"\n"),
                   ("再次输入迁移包密码：".encode(), PASSWORD.encode()+b"\n")]
        deadline = time.monotonic()+10
        try:
            while time.monotonic() < deadline:
                if not select.select([fd], [], [], 0.1)[0]:
                    continue
                try:
                    chunk = os.read(fd, 4096)
                except OSError:
                    ended = True
                    break
                if not chunk:
                    ended = True
                    break
                output += chunk
                if step < len(prompts) and prompts[step][0] in output:
                    os.write(fd, prompts[step][1])
                    step += 1
            if not ended:
                os.kill(pid, signal.SIGKILL)
            _, status = os.waitpid(pid, 0)
            self.assertEqual(status, 0)
            self.assertIn(b"TTY_FLOW_OK", output)
            self.assertNotIn(PASSWORD.encode(), output)
        finally:
            os.close(fd)

    def test_password_prompt_refuses_echoing_fallback_without_tty(self):
        with patch("builtins.open", side_effect=OSError("no tty")):
            with patch.object(migration.getpass, "getpass") as prompt:
                with self.assertRaisesRegex(migration.MigrationError, "交互SSH终端"):
                    migration.ask_password(confirm=True)
                prompt.assert_not_called()

    def test_config_rejects_interpolation_external_db_duplicate_keys_and_compose_overrides(self):
        for suffix in (b"COMPOSE_FILE=/tmp/other.yaml\n", b"BOT_TOKEN=duplicate\n", b"CUSTOM=${HOME}\n",
                       b"DOCKER_HOST=tcp://other:2375\n"):
            with self.assertRaises(migration.MigrationError):
                migration.parse_config(config()+suffix)
        with self.assertRaises(migration.MigrationError):
            migration.parse_config(config().replace(b"/app/data/bot.sqlite3", b"/etc/secret.sqlite3"))

    def test_newer_source_revision_is_rejected(self):
        source = {"commit": "a"*40, "timestamp": 200}
        with patch.object(migration, "revision", return_value={"commit": "b"*40, "timestamp": 100}):
            with self.assertRaisesRegex(migration.MigrationError, "源码早于"):
                migration.check_revision(PROJECT, source)

    def test_password_rules_and_no_command_line_password(self):
        for value in ("short", "a"*12+"\n", "a"*12+"\x00"):
            with self.assertRaises(migration.MigrationError):
                migration.password_bytes(value)
        with tempfile.TemporaryDirectory() as directory:
            source, target = Path(directory)/"input", Path(directory)/"output"
            source.write_bytes(b"public fixture")
            def fake_run(args, **kwargs):
                if "-help" not in args:
                    self.assertNotIn(PASSWORD, " ".join(args))
                    self.assertEqual(kwargs["input"], PASSWORD.encode()+b"\n")
                    target.write_bytes(b"encrypted fixture")
                return subprocess_result()
            with patch.object(migration.subprocess, "run", side_effect=fake_run):
                migration.openssl_file(source, target, PASSWORD.encode())


def subprocess_result():
    from subprocess import CompletedProcess
    return CompletedProcess([], 0, b"", b"")


if __name__ == "__main__":
    unittest.main()
