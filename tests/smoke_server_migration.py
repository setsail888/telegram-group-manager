"""Explicit real-Docker smoke test; every bot is a sleep-only test container.

Runs in CI on Linux. Never connects to Telegram or uses a real Bot Token.
"""
import importlib.util
import json
import os
from pathlib import Path
import secrets
import shutil
import tempfile


PROJECT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("migration", PROJECT/"scripts/server-migrate.py")
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
PASSWORD = "ci-only-migration-password"


def configuration(bot_id):
    return (f"BOT_TOKEN={bot_id}:"+"A"*35+"\nDB_PATH=/app/data/bot.sqlite3\nWITH_OCR=0\nOCR_ENABLED=0\n").encode()


def prepare(base, suffix, bot_id=None):
    project = base/f"ggmig-{suffix}-{secrets.token_hex(5)}"
    project.mkdir()
    for name in ("Dockerfile", "requirements.txt", "config.sample"):
        shutil.copy2(PROJECT/name, project/name)
    shutil.copytree(PROJECT/"app", project/"app", ignore=shutil.ignore_patterns("__pycache__"))
    compose = (PROJECT/"compose.yaml").read_text(encoding="utf-8")
    compose = compose.replace("    restart: unless-stopped", "    command: [python, -c, 'import time; time.sleep(600)']\n    restart: unless-stopped", 1)
    (project/"compose.yaml").write_text(compose, encoding="utf-8")
    if bot_id:
        m.write_config(project/".env", configuration(bot_id))
    return project, m.Deployment(project)


def seed(deployment, cid, points):
    # Abrupt exec-process exit deliberately leaves committed WAL pages behind.
    # The service's only process sleeps; it does not poll or send to Telegram.
    code = """
import os, sys
from app.store import Store
from app.economy import Economy
cid, amount = map(int, sys.argv[1:])
store = Store(os.environ['DB_PATH'])
store.ensure_chat(cid, 'real Docker migration fixture')
store.set(cid, 'points_enabled', True)
store.set(cid, 'ocr_mode', 'review')
store.set_ui_language(1, 'fr')
store.add_filter(cid, 'word', 'migration keyword', 'migration keyword')
store.change_points(cid, 2, amount, 1)
economy = Economy(store)
item = economy.add_item(cid, 1, 'reward', 'migration fixture', 10, 5)
economy.purchase(cid, 2, item, 1)
store.save_offset(123456)
os._exit(0)
"""
    deployment.dc("exec", "-T", "groupbot", "python", "-c", code, str(cid), str(points))


def read_state(deployment):
    code = """
import json, os, sqlite3
db = sqlite3.connect(os.environ['DB_PATH'])
state = {'chats': db.execute('SELECT chat_id FROM chats ORDER BY chat_id').fetchall(),
         'points': db.execute('SELECT chat_id,user_id,balance FROM point_balances ORDER BY chat_id,user_id').fetchall(),
         'orders': db.execute('SELECT COUNT(*) FROM shop_orders').fetchone()[0],
         'words': db.execute('SELECT COUNT(*) FROM filters').fetchone()[0],
         'languages': db.execute('SELECT owner_id,locale FROM ui_languages ORDER BY owner_id').fetchall(),
         'offset': db.execute("SELECT value FROM meta WHERE key='last_update_id'").fetchone()[0],
         'bot_id': os.environ['BOT_TOKEN'].split(':')[0],
         'owner': os.stat(os.environ['DB_PATH']).st_uid}
db.close()
print(json.dumps(state))
"""
    return json.loads(deployment.dc("exec", "-T", "groupbot", "python", "-c", code, capture=True))


class FailAfterStart(m.Deployment):
    def start_import(self):
        super().start_import()
        raise m.MigrationError("injected failure after real container recreation")


def main():
    deployments = []
    with tempfile.TemporaryDirectory(prefix="gg migration smoke ") as directory:
        base = Path(directory)
        os.environ["GROUPGUARD_BACKUPS"] = str(base/"backups")
        try:
            a, source = prepare(base, "a", "123456789")
            deployments.append(source)
            source.dc("up", "-d", "--build", "groupbot")
            seed(source, -1001, 123)
            original = read_state(source)
            source.dc("exec", "-T", "groupbot", "python", "-c", """
import os, pathlib, sqlite3
db = sqlite3.connect(os.environ['DB_PATH'])
db.execute('PRAGMA journal_mode=WAL')
db.execute("UPDATE meta SET value='123457' WHERE key='last_update_id'")
db.commit()
assert pathlib.Path(os.environ['DB_PATH']+'-wal').stat().st_size > 0
os._exit(0)
""")
            original["offset"] = "123457"
            package = base/"transfer with spaces.ggm"
            m.export_data(a, source, package, PASSWORD)
            assert not source.running()["groupbot"]

            b, target = prepare(base, "b")
            deployments.append(target)
            m.import_data(b, target, package, PASSWORD, confirm=lambda _: "MIGRATE")
            assert read_state(target) == original, "B data/config/ownership changed"
            assert not source.running()["groupbot"]

            c, old_target = prepare(base, "rollback", "987654321")
            deployments.append(old_target)
            old_target.dc("up", "-d", "--build", "groupbot")
            seed(old_target, -2001, 88)
            previous = read_state(old_target)
            failing = FailAfterStart(c)
            try:
                m.import_data(c, failing, package, PASSWORD, confirm=lambda _: "MIGRATE")
            except m.MigrationError as exc:
                assert "injected failure" in str(exc)
            else:
                raise AssertionError("failure injection did not run")
            assert read_state(old_target) == previous, "rollback did not restore the original environment/data"
            assert not source.running()["groupbot"]
            print("REAL DOCKER MIGRATION OK: WAL, fresh import, ownership, points/orders/config and rollback")
        finally:
            for deployment in reversed(deployments):
                assert deployment.project.parent == base and deployment.project.name.startswith("ggmig-")
                if not (deployment.project/".env").exists():
                    m.write_config(deployment.project/".env", configuration("123456789"))
                try:
                    deployment.dc("--profile", "dashboard", "down", "-v", "--remove-orphans")
                except m.MigrationError:
                    print("Unable to remove isolated smoke-test containers; check CI Docker state")


if __name__ == "__main__":
    main()
