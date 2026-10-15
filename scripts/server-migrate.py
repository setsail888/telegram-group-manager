#!/usr/bin/env python3
"""Encrypted, offline GroupGuard transfers. Standard library + OpenSSL + Docker.

Never source .env, put passwords in argv/environment, or extract tar links.
The bot is stopped before copying the volume; SQLite backup incorporates WAL.
"""
import argparse
from contextlib import closing, contextmanager
from datetime import datetime, timezone
import getpass
import hashlib
import hmac
import json
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import time


MAGIC = b"GroupGuard-Migration-v1\n"
SALT_BYTES, TAG_BYTES, ITERATIONS = 32, 32, 200_000
CHUNK = 1024 * 1024
MAX_FILES = 10_000
TABLES = ("chats", "filters", "point_balances", "shop_orders", "raffles",
          "scheduled_messages", "ui_languages")


class MigrationError(Exception):
    pass


def info(message):
    print(f"[GroupGuard] {message}", flush=True)


def parse_config(raw):
    if len(raw) > 65_536:
        raise MigrationError("运行配置过大，已停止")
    try:
        text = raw.decode("utf-8")
    except UnicodeError:
        raise MigrationError("运行配置不是UTF-8文件") from None
    values = {}
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, sep, value = line.partition("=")
        if (not sep or not re.fullmatch(r"[A-Z][A-Z0-9_]*", key)
                or key in values or key.startswith(("COMPOSE_", "DOCKER_"))
                or any(char in value for char in ("$", "\x00", "\r"))):
            raise MigrationError("运行配置包含重复字段、动态变量或不支持的Docker配置；不会执行配置内容")
        values[key] = value
    token = values.get("BOT_TOKEN", "")
    if not re.fullmatch(r"[0-9]{6,15}:[A-Za-z0-9_-]{20,}", token):
        raise MigrationError("迁移配置中缺少有效Bot Token；密钥不会显示")
    dbpath = values.get("DB_PATH") or "/app/data/bot.sqlite3"
    if not re.fullmatch(r"/app/data/[A-Za-z0-9_.-]+", dbpath) or Path(dbpath).name in (".", ".."):
        raise MigrationError("迁移只支持数据库位于 /app/data/ 目录下的标准Docker部署")
    for flag in ("WITH_OCR", "OCR_ENABLED"):
        if values.get(flag, "0") not in ("0", "1", "true", "false", "yes", "no", "on", "off"):
            raise MigrationError("OCR运行配置无效")
    return values, Path(dbpath).name


def digest(path):
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(CHUNK), b""):
            result.update(chunk)
    return result.hexdigest()


def database_info(path):
    if not path.is_file() or path.is_symlink():
        raise MigrationError("迁移数据中缺少数据库")
    try:
        with closing(sqlite3.connect(path.resolve().as_uri()+"?mode=ro", uri=True, timeout=10)) as db:
            db.execute("PRAGMA trusted_schema=OFF")
            if db.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                raise MigrationError("SQLite完整性检查失败，未导入数据")
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not {"chats", "filters", "meta"} <= tables:
                raise MigrationError("数据库不是可识别的GroupGuard数据库")
            return {name: db.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
                    for name in TABLES if name in tables}
    except sqlite3.Error:
        raise MigrationError("数据库无法读取或已损坏，未导入数据") from None


def snapshot(data, basename, workspace):
    source = data / basename
    database_info(source)
    target = workspace / "snapshot.sqlite3"
    started = time.monotonic()
    def progress(*_):
        if time.monotonic()-started > 60:
            raise MigrationError("数据库快照超时，已停止")
    with closing(sqlite3.connect(source.resolve().as_uri()+"?mode=ro", uri=True, timeout=10)) as original:
        with closing(sqlite3.connect(target)) as copy:
            original.backup(copy, pages=1024, progress=progress)
            copy.execute("PRAGMA journal_mode=DELETE")
    target.chmod(0o600)
    counts = database_info(target)
    os.replace(target, source)
    for suffix in ("-wal", "-shm", "-journal"):
        source.with_name(basename+suffix).unlink(missing_ok=True)
    return counts


def safe_name(name):
    path = PurePosixPath(name)
    if (not name or "\\" in name or path.is_absolute() or ".." in path.parts
            or path.as_posix() != name or len(name) > 1024):
        raise MigrationError("迁移包含不安全文件路径，未导入")
    return path


def make_payload(data, config, workspace, revision=None):
    values, basename = parse_config(config)
    files = sorted(data.rglob("*"))
    if any(p.is_symlink() or not (p.is_dir() or p.is_file()) for p in files):
        raise MigrationError("数据卷包含链接或特殊文件，已停止迁移")
    counts = snapshot(data, basename, workspace)
    records = {}
    for path in sorted(data.rglob("*")):
        if path.is_file():
            name = path.relative_to(data).as_posix()
            safe_name("data/"+name)
            path.chmod(0o600)
            records[name] = {"size": path.stat().st_size, "sha256": digest(path)}
    if len(records) > MAX_FILES:
        raise MigrationError("持久化文件过多，请联系维护者检查自定义部署")
    manifest = {"format": 1, "created_at": datetime.now(timezone.utc).isoformat(),
                "bot_id": values["BOT_TOKEN"].split(":", 1)[0], "database": basename,
                "config_sha256": hashlib.sha256(config).hexdigest(),
                "files": records, "counts": counts, "revision": revision}
    (workspace/"config.env").write_bytes(config)
    (workspace/"manifest.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    (workspace/"config.env").chmod(0o600)
    (workspace/"manifest.json").chmod(0o600)
    payload = workspace/"payload.tar.gz"
    with tarfile.open(payload, "w:gz") as archive:
        for name in ("manifest.json", "config.env"):
            archive.add(workspace/name, arcname=name, recursive=False)
        for name in records:
            archive.add(data/name, arcname="data/"+name, recursive=False)
    return payload, manifest


def unpack_payload(payload, destination):
    destination.mkdir(mode=0o700)
    with tarfile.open(payload, "r:gz") as archive:
        members = {}
        total = 0
        for member in archive:
            path = safe_name(member.name)
            if (not member.isfile() or member.name in members or len(members) >= MAX_FILES+2
                    or member.size < 0 or (member.name not in ("manifest.json", "config.env")
                                          and (len(path.parts) < 2 or path.parts[0] != "data"))):
                raise MigrationError("迁移包包含重复、特殊或未知文件，未导入")
            members[member.name] = member
            total += member.size
        if not {"manifest.json", "config.env"} <= members.keys():
            raise MigrationError("迁移包不完整，未导入")
        if members["manifest.json"].size > 4_000_000 or members["config.env"].size > 65_536:
            raise MigrationError("迁移包元数据过大，未导入")
        if total + CHUNK > shutil.disk_usage(destination).free:
            raise MigrationError("磁盘剩余空间不足以解包，未导入")
        for name, member in members.items():
            target = destination.joinpath(*PurePosixPath(name).parts)
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            with archive.extractfile(member) as source, target.open("xb") as out:
                target.chmod(0o600)
                shutil.copyfileobj(source, out, CHUNK)
    try:
        manifest = json.loads((destination/"manifest.json").read_text(encoding="utf-8"))
        config = (destination/"config.env").read_bytes()
        values, basename = parse_config(config)
        if (manifest["format"] != 1 or manifest["database"] != basename
                or manifest["bot_id"] != values["BOT_TOKEN"].split(":", 1)[0]
                or manifest["config_sha256"] != hashlib.sha256(config).hexdigest()
                or not isinstance(manifest["files"], dict)):
            raise MigrationError("迁移包版本或运行配置不匹配，未导入")
        expected = {"data/"+name for name in manifest["files"]} | {"manifest.json", "config.env"}
        if expected != members.keys():
            raise MigrationError("迁移包文件清单不完整，未导入")
        for name, record in manifest["files"].items():
            safe_name("data/"+name)
            path = destination/"data"/name
            if record["size"] != path.stat().st_size or record["sha256"] != digest(path):
                raise MigrationError("迁移包文件校验失败，未导入")
        manifest["counts"] = database_info(destination/"data"/basename)
    except (KeyError, TypeError, UnicodeError, json.JSONDecodeError):
        raise MigrationError("迁移包清单格式无效，未导入") from None
    return manifest, config


def password_bytes(password):
    raw = password.encode("utf-8")
    if len(password) < 12 or len(raw) > 1024 or any(c in raw for c in (0, 10, 13)):
        raise MigrationError("迁移密码至少12个字符，不能包含换行；请保存好密码")
    return raw


def openssl_file(source, target, password, decrypt=False):
    args = ["openssl", "enc", "-aes-256-cbc", "-pbkdf2", "-iter", str(ITERATIONS),
            "-md", "sha256", "-pass", "stdin", "-in", str(source), "-out", str(target)]
    if decrypt:
        args.append("-d")
    # OpenSSL 3.2 added a configurable salt length. Use the legacy 8-byte
    # encrypted-file salt on both ends for transfers across OpenSSL versions.
    help_result = subprocess.run(["openssl", "enc", "-help"], capture_output=True)
    if b"-saltlen" in help_result.stdout+help_result.stderr:
        args.extend(["-saltlen", "8"])
    proc = subprocess.run(args, input=password+b"\n", capture_output=True)
    if proc.returncode:
        raise MigrationError("迁移包加密/解密失败，请检查OpenSSL和磁盘空间")
    target.chmod(0o600)


def authentication_key(password, salt):
    # Independent salt and domain from OpenSSL's encryption KDF.
    return hashlib.pbkdf2_hmac("sha256", password, b"GroupGuard authentication\0"+salt, ITERATIONS)


def encrypt_package(payload, output, password, workspace):
    raw = password_bytes(password)
    ciphertext = workspace/"ciphertext.bin"
    openssl_file(payload, ciphertext, raw)
    header = MAGIC+secrets.token_bytes(SALT_BYTES)
    mac = hmac.new(authentication_key(raw, header[len(MAGIC):]), digestmod="sha256")
    output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Publish only a complete package, refusing to replace any existing file.
    with tempfile.NamedTemporaryFile(dir=output.parent, prefix=".ggm-", delete=False) as out:
        temporary = Path(out.name)
        try:
            out.write(header)
            mac.update(header)
            with ciphertext.open("rb") as stream:
                for chunk in iter(lambda: stream.read(CHUNK), b""):
                    out.write(chunk)
                    mac.update(chunk)
            out.write(mac.digest())
            out.flush()
            os.fsync(out.fileno())
            out.close()
            os.link(temporary, output)
        finally:
            temporary.unlink(missing_ok=True)
    output.chmod(0o600)


def decrypt_package(package, workspace, password):
    raw = password_bytes(password)
    size = package.stat().st_size
    header_size = len(MAGIC)+SALT_BYTES
    if size < header_size+32+TAG_BYTES:
        raise MigrationError("不是完整的GroupGuard加密迁移包")
    ciphertext = workspace/"ciphertext.bin"
    with package.open("rb") as source:
        header = source.read(header_size)
        if not header.startswith(MAGIC):
            raise MigrationError("不是支持的GroupGuard迁移包格式")
        mac = hmac.new(authentication_key(raw, header[len(MAGIC):]), digestmod="sha256")
        mac.update(header)
        remaining = size-header_size-TAG_BYTES
        with ciphertext.open("xb") as out:
            while remaining:
                chunk = source.read(min(CHUNK, remaining))
                if not chunk:
                    raise MigrationError("迁移包被截断，未导入")
                out.write(chunk)
                mac.update(chunk)
                remaining -= len(chunk)
        if not hmac.compare_digest(mac.digest(), source.read(TAG_BYTES)):
            raise MigrationError("密码错误或迁移包被修改，未解密、未导入")
    payload = workspace/"decrypted.tar.gz"
    openssl_file(ciphertext, payload, raw, decrypt=True)
    return payload


VOLUME_TOOL = """
import os, pathlib, shutil, sys
root = pathlib.Path('/app/data')
assert root.is_dir() and not root.is_symlink()
if sys.argv[1] == 'clear':
    for path in root.iterdir():
        if path.is_symlink() or path.is_file(): path.unlink()
        elif path.is_dir(): shutil.rmtree(path)
        else: raise RuntimeError('Unexpected volume entry')
elif sys.argv[1] == 'owner':
    for path in [root, *root.rglob('*')]:
        if path.is_symlink(): raise RuntimeError('Unexpected symlink')
        os.chown(path, 10001, 10001)
        path.chmod(0o700 if path.is_dir() else 0o600)
else: raise RuntimeError('Unknown volume operation')
"""


class Deployment:
    def __init__(self, project):
        self.project = project.resolve()
        self.privilege = [] if os.geteuid() == 0 else ["sudo"]
        # Migration targets this local checkout, never an ambient alternate
        # Compose project/file/daemon selected by imported config or the shell.
        if (any(k.startswith("COMPOSE_") for k in os.environ)
                or any(k in os.environ for k in ("DOCKER_HOST", "DOCKER_CONTEXT"))):
            raise MigrationError("请在未设置 COMPOSE_* / DOCKER_* 的标准本机Docker环境执行迁移")

    def command(self, args, capture=False):
        result = subprocess.run(args, cwd=self.project, text=True,
                                stdout=subprocess.PIPE if capture else None,
                                stderr=subprocess.PIPE if capture else None)
        if result.returncode:
            raise MigrationError("Docker/文件操作失败，已停止；请检查Docker、权限、磁盘空间和上方错误")
        return result.stdout.strip() if capture else ""

    def dc(self, *args, capture=False):
        return self.command(self.privilege+["docker", "compose", "--project-directory", str(self.project),
                            "-f", str(self.project/"compose.yaml"), *args], capture)

    def running(self):
        return {name: bool(self.dc("--profile", "dashboard", "ps", "--status", "running", "-q", name,
                                   capture=True)) for name in ("groupbot", "dashboard")}

    def stop(self):
        self.dc("--profile", "dashboard", "stop", "groupbot", "dashboard")

    def resume(self, states):
        for name, running in states.items():
            if running:
                self.dc("--profile", "dashboard", "start", name)

    def ensure_container(self):
        if not self.dc("ps", "-a", "-q", "groupbot", capture=True):
            self.dc("build", "groupbot")
            self.dc("create", "groupbot")

    def copy_data(self, destination):
        destination.mkdir(mode=0o700)
        self.dc("cp", "groupbot:/app/data/.", str(destination))
        self.command(self.privilege+["chown", "-hR", f"{os.getuid()}:{os.getgid()}", str(destination)])

    def volume(self, action):
        self.dc("run", "--rm", "--no-deps", "-T", "--user", "0",
                "--cap-add", "CHOWN", "--cap-add", "FOWNER", "--cap-add", "DAC_OVERRIDE",
                "--entrypoint", "python", "groupbot", "-c", VOLUME_TOOL, action)

    def replace_data(self, data):
        self.volume("clear")
        self.dc("cp", str(data)+"/.", "groupbot:/app/data/")
        self.volume("owner")

    def start_import(self):
        self.dc("up", "-d", "--build", "groupbot")
        if not self.running()["groupbot"]:
            raise MigrationError("新机器人容器未运行，已停止迁移；请检查构建与日志")


def backup_root():
    path = Path(os.environ.get("GROUPGUARD_BACKUPS", str(Path.home()/"groupguard-backups"))).expanduser().resolve()
    if path == Path(path.anchor):
        raise MigrationError("备份目录不能是文件系统根目录")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)
    return path


def new_package(root, prefix="migration"):
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return root/f"groupguard-{prefix}-{timestamp}-{secrets.token_hex(4)}.ggm"


def revision(project):
    result = subprocess.run(["git", "-C", str(project), "show", "-s", "--format=%H:%ct", "HEAD"],
                            capture_output=True, text=True)
    match = re.fullmatch(r"([0-9a-f]{40}):([0-9]+)", result.stdout.strip())
    return {"commit": match[1], "timestamp": int(match[2])} if result.returncode == 0 and match else None


def check_revision(project, source):
    target = revision(project)
    if source is not None:
        if (not isinstance(source, dict) or not re.fullmatch(r"[0-9a-f]{40}", str(source.get("commit", "")))
                or type(source.get("timestamp")) is not int):
            raise MigrationError("迁移包源码版本信息无效")
        if target and source["timestamp"] > target["timestamp"]:
            raise MigrationError("B服务器源码早于迁移包，请先 git pull --ff-only origin main，再重新导入；暂不启动机器人")


@contextmanager
def migration_lock(project):
    import fcntl
    with (project/".groupguard-migration.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise MigrationError("已有迁移任务运行，请等该任务完成") from None
        yield


def ask(message):
    try:
        with open("/dev/tty", "r+") as terminal:
            terminal.write(message)
            terminal.flush()
            answer = terminal.readline()
            if not answer:
                raise MigrationError("没有收到输入，操作已取消")
            return answer.rstrip("\r\n")
    except OSError:
        raise MigrationError("迁移需要交互SSH终端，请直接在服务器中执行gg migrate") from None


def ask_password(confirm=False):
    try:
        terminal = open("/dev/tty", "r+")
    except OSError:
        raise MigrationError("需要交互SSH终端隐藏输入密码，不接受命令参数或管道密码") from None
    with terminal:
        if not terminal.isatty():
            raise MigrationError("需要交互SSH终端隐藏输入密码")
        password = getpass.getpass("迁移包密码（至少12字符，不显示）：", stream=terminal)
        password_bytes(password)
        if confirm and password != getpass.getpass("再次输入迁移包密码：", stream=terminal):
            raise MigrationError("两次密码不一致，未导出")
        return password


def preview(manifest):
    info(f"机器人ID：{manifest['bot_id']}（Token不显示）")
    labels = {"chats": "群组", "filters": "过滤规则", "point_balances": "积分账户",
              "shop_orders": "商城订单", "raffles": "抽奖活动", "scheduled_messages": "定时公告",
              "ui_languages": "语言设置"}
    info("数据概览："+" / ".join(f"{labels[k]} {v}" for k, v in manifest["counts"].items()))
    info(f"迁移文件：{len(manifest['files'])}个，未压缩约 {sum(r['size'] for r in manifest['files'].values())/1024/1024:.2f} MiB")


def write_config(path, contents):
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".env.migration-", delete=False) as stream:
        temporary = Path(stream.name)
        try:
            stream.write(contents)
            stream.flush()
            os.fsync(stream.fileno())
            stream.close()
            temporary.chmod(0o600)
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


def export_data(project, deployment, output, password):
    config = (project/".env").read_bytes()
    parse_config(config)
    if output.exists() or output.is_symlink():
        raise MigrationError("迁移包目标已存在，不会覆盖")
    states, completed = deployment.running(), False
    with tempfile.TemporaryDirectory(prefix=".migration-", dir=backup_root()) as directory:
        workspace = Path(directory)
        try:
            info("停止A服务器机器人与看板，生成一致的数据库快照")
            deployment.stop()
            deployment.ensure_container()
            deployment.copy_data(workspace/"data")
            payload, manifest = make_payload(workspace/"data", config, workspace, revision(project))
            encrypt_package(payload, output, password, workspace)
            completed = True
            preview(manifest)
            info(f"加密迁移包已生成：{output}")
            info("A服务器机器人保持停止，数据保留。请把迁移包安全传到B，再运行 gg migrate import 文件路径")
            info("需要取消迁移、恢复A时：gg service → 选择启动。不要同时启动A和B上的同一机器人")
        finally:
            if not completed:
                try:
                    deployment.resume(states)
                    info("导出未完成，已尝试恢复A服务器原运行状态")
                except MigrationError:
                    info("自动恢复A失败，请运行 gg service 检查并手动启动")


def import_data(project, deployment, package, password, confirm=ask):
    env_file = project/".env"
    old_config = env_file.read_bytes() if env_file.exists() else None
    if old_config is not None:
        parse_config(old_config)
    root = backup_root()
    with tempfile.TemporaryDirectory(prefix=".migration-", dir=root) as directory:
        workspace = Path(directory)
        payload = decrypt_package(package, workspace, password)
        manifest, config = unpack_payload(payload, workspace/"incoming")
        check_revision(project, manifest.get("revision"))
        preview(manifest)
        info("将整体替换B的机器人数据与运行配置，沿用A的Bot Token；不是合并两个群管理机器人的数据")
        info("请确认A已停止、迁移包是A最后一次导出；导出后重新启动过A时需要重新导出")
        if confirm("确认A已停止并导入到B？输入 MIGRATE 继续，其余取消：") != "MIGRATE":
            raise MigrationError("已取消，B配置与数据未修改")
        # A placeholder allows Compose to inspect a fresh server without a token
        # prompt and without starting any bot. Never silently replace orphan data.
        if old_config is None:
            write_config(env_file, (project/"config.sample").read_bytes())
        states, touched, completed = {}, False, False
        previous = workspace/"previous"
        previous.mkdir(mode=0o700)
        try:
            states = deployment.running()
            deployment.stop()
            deployment.ensure_container()
            deployment.copy_data(previous/"data")
            has_data = any((previous/"data").iterdir())
            if old_config is None and (has_data or any(states.values())):
                raise MigrationError("B存在历史数据但缺少原.env，无法安全备份；请先找回原配置，未覆盖历史数据")
            if has_data:
                rollback_payload, _ = make_payload(previous/"data", old_config, previous, revision(project))
                rollback = new_package(root, "before-import")
                encrypt_package(rollback_payload, rollback, password, previous)
                info(f"B原数据已加密备份：{rollback}（使用本次迁移密码）")
            write_config(env_file, config)
            touched = True
            info("按A的配置构建B镜像；此步骤不会启动机器人")
            deployment.dc("build", "groupbot")
            deployment.dc("create", "--no-recreate", "groupbot")
            deployment.replace_data(workspace/"incoming"/"data")
            # Verify the actual volume copy before allowing the new bot to run.
            deployment.copy_data(workspace/"verified")
            for name, record in manifest["files"].items():
                copied = workspace/"verified"/name
                if not copied.is_file() or copied.stat().st_size != record["size"] or digest(copied) != record["sha256"]:
                    raise MigrationError("B数据卷文件校验失败，未启动新机器人")
            database_info(workspace/"verified"/manifest["database"])
            deployment.start_import()
            completed = True
            info("迁移数据已导入，B机器人容器已启动。请运行 gg logs 并在Telegram核对群设置、词库和积分")
            info("看板不会自动开放；需要时运行 gg dashboard。请保持A停止，确认B正常后再处理旧服务器")
        finally:
            if not completed:
                try:
                    if touched:
                        info("导入未完成，正在恢复B原配置与数据")
                        deployment.stop()
                        if old_config is not None:
                            write_config(env_file, old_config)
                            deployment.dc("build", "groupbot")
                            # start alone would reuse the failed import's token
                            # and environment. Recreate from the original config.
                            deployment.dc("create", "--force-recreate", "groupbot")
                        deployment.replace_data(previous/"data")
                    if old_config is None:
                        if touched:
                            deployment.dc("rm", "-f", "groupbot")
                        else:
                            deployment.resume(states)
                        env_file.unlink(missing_ok=True)
                    else:
                        deployment.resume(states)
                    info("已恢复B原配置与运行状态；A仍保持停止")
                except (MigrationError, OSError):
                    info("B自动恢复未完成：请保持机器人停止，保留上方before-import加密备份并按文档恢复；不要清理数据卷")


def main(argv=None):
    os.umask(0o077)
    parser = argparse.ArgumentParser(description="GroupGuard加密服务器迁移")
    parser.add_argument("action", choices=("export", "import"))
    parser.add_argument("file", nargs="?", help="导出目标或导入迁移包路径")
    args = parser.parse_args(argv)
    project = Path(__file__).resolve().parents[1]
    if sys.platform != "linux":
        raise MigrationError("服务器迁移命令只在Linux服务器SSH终端使用")
    deployment = Deployment(project)
    with migration_lock(project):
        if args.action == "export":
            info("迁移导出会暂停A机器人，成功后保持停止；迁移包包含密钥，使用密码加密保存")
            password = ask_password(confirm=True)
            output = Path(args.file).expanduser().absolute() if args.file else new_package(backup_root())
            export_data(project, deployment, output, password)
        else:
            filename = args.file or ask("迁移包的完整路径：")
            package = Path(filename).expanduser().resolve(strict=True)
            password = ask_password()
            import_data(project, deployment, package, password)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[GroupGuard] 用户中断，已按当前阶段尝试恢复原服务；请运行gg status核对", file=sys.stderr)
        sys.exit(130)
    except (MigrationError, OSError, sqlite3.Error, tarfile.TarError, ValueError) as exc:
        detail = str(exc) if isinstance(exc, MigrationError) else "文件、数据库或迁移包处理失败，请检查格式、权限和磁盘空间"
        print(f"[GroupGuard] 错误：{detail}", file=sys.stderr)
        sys.exit(1)
