"""SQLite state is scoped to chat_id; no Telegram credentials enter the database."""
import json
import os
import sqlite3
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

DEFAULTS = {
    "block_links": True,
    "block_forward": False,
    "anti_flood": True,
    "welcome_enabled": True,
    "welcome_text": "欢迎 {name} 加入本群！请先阅读 /rules。",
    "rules_text": "文明交流，禁止广告、诈骗和刷屏。尊重每一位群友。",
    "verify_enabled": False,
    "verify_timeout": 300,
    "log_chat_id": 0,
    "warn_limit": 3,
    "block_new_bots": False,
    "points_enabled": False,
    "checkin_award": 5,
    "repeat_photo_enabled": False,
    "repeat_photo_limit": 3,
    "repeat_photo_window": 60,
    "spam_guard_enabled": False,
    "word_loose_match": False,
    "flood_limit": 7,
    "flood_window": 10,
    "repeat_limit": 3,
    "repeat_window": 45,
    "media_flood_limit": 5,
    "media_flood_window": 20,
    "media_flood_enabled": False,
    "mention_limit": 5,
    "auto_mute_enabled": False,
    "auto_mute_after": 3,
    "auto_mute_minutes": 30,
    "name_guard_enabled": False,
    "name_length_limit": 40,
    "ocr_mode": "off",
    "ocr_confidence": 75,
}
TOGGLES = {"block_links", "block_forward", "anti_flood", "welcome_enabled",
           "verify_enabled", "block_new_bots", "points_enabled",
           "repeat_photo_enabled", "spam_guard_enabled",
           "word_loose_match", "auto_mute_enabled", "media_flood_enabled",
           "name_guard_enabled"}

NUMBER_LIMITS = {
    "flood_limit": (3, 30),
    "flood_window": (3, 120),
    "repeat_limit": (2, 10),
    "repeat_window": (10, 300),
    "media_flood_limit": (2, 30),
    "media_flood_window": (5, 120),
    "mention_limit": (3, 20),
    "auto_mute_after": (2, 10),
    "auto_mute_minutes": (1, 1440),
    "name_length_limit": (20, 100),
}


class Store:
    def __init__(self, path=":memory:", timezone="Asia/Shanghai"):
        if path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.db = sqlite3.connect(path, isolation_level=None, timeout=15)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA busy_timeout=10000")
        self.db.execute("PRAGMA foreign_keys=ON")
        if path != ":memory:":
            self.db.execute("PRAGMA journal_mode=WAL")
        self.tz = ZoneInfo(timezone)
        self._init()

    def _init(self):
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS chats (
              chat_id INTEGER PRIMARY KEY, title TEXT NOT NULL,
              settings TEXT NOT NULL, updated_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS filters (
              chat_id INTEGER NOT NULL, kind TEXT NOT NULL,
              key TEXT NOT NULL, value TEXT NOT NULL,
              PRIMARY KEY(chat_id, kind, key)
            );
            CREATE TABLE IF NOT EXISTS replies (
              chat_id INTEGER NOT NULL, key TEXT NOT NULL,
              keyword TEXT NOT NULL, reply TEXT NOT NULL,
              buttons TEXT NOT NULL DEFAULT '[]',
              media_type TEXT, media_id TEXT,
              PRIMARY KEY(chat_id, key)
            );
            CREATE TABLE IF NOT EXISTS warnings (
              chat_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
              count INTEGER NOT NULL DEFAULT 0,
              PRIMARY KEY(chat_id, user_id)
            );
            CREATE TABLE IF NOT EXISTS moderation_events (
              chat_id INTEGER NOT NULL, message_id INTEGER NOT NULL,
              user_id INTEGER NOT NULL, reason TEXT NOT NULL,
              created_at INTEGER NOT NULL,
              PRIMARY KEY(chat_id,message_id)
            );
            CREATE INDEX IF NOT EXISTS idx_moderation_events_user
              ON moderation_events(chat_id,user_id,created_at);
            CREATE INDEX IF NOT EXISTS idx_moderation_events_prune
              ON moderation_events(created_at);
            CREATE TABLE IF NOT EXISTS pending (
              chat_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
              nonce TEXT NOT NULL, answer INTEGER NOT NULL,
              expires_at INTEGER NOT NULL, message_id INTEGER,
              PRIMARY KEY(chat_id, user_id)
            );
            CREATE TABLE IF NOT EXISTS stats (
              chat_id INTEGER NOT NULL, day TEXT NOT NULL,
              name TEXT NOT NULL, count INTEGER NOT NULL DEFAULT 0,
              PRIMARY KEY(chat_id, day, name)
            );
            CREATE TABLE IF NOT EXISTS meta (
              key TEXT PRIMARY KEY, value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS activity (
              chat_id INTEGER NOT NULL, day TEXT NOT NULL,
              user_id INTEGER NOT NULL, count INTEGER NOT NULL DEFAULT 0,
              PRIMARY KEY(chat_id,day,user_id)
            );
            CREATE TABLE IF NOT EXISTS scheduled_messages (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              chat_id INTEGER NOT NULL,
              title TEXT NOT NULL, body TEXT NOT NULL,
              mode TEXT NOT NULL CHECK(mode IN ('once','daily','weekly')),
              rule TEXT NOT NULL,
              next_run INTEGER NOT NULL,
              owner_id INTEGER NOT NULL,
              enabled INTEGER NOT NULL DEFAULT 1,
              claim_until INTEGER NOT NULL DEFAULT 0,
              failures INTEGER NOT NULL DEFAULT 0,
              last_sent INTEGER,
              created_at INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_schedules_due
              ON scheduled_messages(enabled,next_run,claim_until);
            CREATE TABLE IF NOT EXISTS point_balances (
              chat_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
              balance INTEGER NOT NULL DEFAULT 0 CHECK(balance BETWEEN 0 AND 1000000000),
              checkins INTEGER NOT NULL DEFAULT 0, streak INTEGER NOT NULL DEFAULT 0,
              last_checkin TEXT,
              PRIMARY KEY(chat_id,user_id)
            );
            CREATE TABLE IF NOT EXISTS point_checkins (
              chat_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
              day TEXT NOT NULL, award INTEGER NOT NULL,
              streak INTEGER NOT NULL,
              PRIMARY KEY(chat_id,user_id,day)
            );
            CREATE TABLE IF NOT EXISTS point_events (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              chat_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
              delta INTEGER NOT NULL, balance INTEGER NOT NULL,
              kind TEXT NOT NULL, actor_id INTEGER,
              created_at INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_point_events
              ON point_events(chat_id,id);
            CREATE TABLE IF NOT EXISTS point_transfers (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              chat_id INTEGER NOT NULL, source_id INTEGER NOT NULL,
              target_id INTEGER NOT NULL, amount INTEGER NOT NULL CHECK(amount>0),
              request_message_id INTEGER NOT NULL, created_at INTEGER NOT NULL,
              UNIQUE(chat_id,request_message_id)
            );
            CREATE TABLE IF NOT EXISTS transfer_intents (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              chat_id INTEGER NOT NULL, source_id INTEGER NOT NULL,
              target_id INTEGER NOT NULL, amount INTEGER NOT NULL,
              nonce TEXT NOT NULL, request_message_id INTEGER NOT NULL,
              confirmation_message_id INTEGER,
              expires_at INTEGER NOT NULL, status TEXT NOT NULL
                CHECK(status IN ('pending','executed','cancelled')),
              created_at INTEGER NOT NULL,
              UNIQUE(chat_id,request_message_id)
            );
            CREATE INDEX IF NOT EXISTS idx_transfer_intents
              ON transfer_intents(chat_id,source_id,status);
            CREATE TABLE IF NOT EXISTS shop_items (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              chat_id INTEGER NOT NULL, title TEXT NOT NULL,
              description TEXT NOT NULL, price INTEGER NOT NULL CHECK(price>0),
              stock INTEGER NOT NULL CHECK(stock>=0),
              active INTEGER NOT NULL DEFAULT 1,
              created_by INTEGER NOT NULL, created_at INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_shop_items_chat
              ON shop_items(chat_id,active,id);
            CREATE TABLE IF NOT EXISTS shop_orders (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              chat_id INTEGER NOT NULL, member_id INTEGER NOT NULL,
              item_id INTEGER NOT NULL, item_title TEXT NOT NULL,
              cost INTEGER NOT NULL CHECK(cost>0),
              status TEXT NOT NULL CHECK(status IN ('pending','fulfilled','refunded')),
              request_message_id INTEGER NOT NULL,
              created_at INTEGER NOT NULL,
              processed_by INTEGER, processed_at INTEGER,
              UNIQUE(chat_id,request_message_id)
            );
            CREATE INDEX IF NOT EXISTS idx_shop_orders_chat
              ON shop_orders(chat_id,status,id);
            CREATE TABLE IF NOT EXISTS raffles (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              chat_id INTEGER NOT NULL, title TEXT NOT NULL,
              status TEXT NOT NULL CHECK(status IN ('open','drawn','cancelled')),
              created_by INTEGER NOT NULL, winner_id INTEGER,
              created_at INTEGER NOT NULL, finished_at INTEGER
            );
            CREATE INDEX IF NOT EXISTS idx_raffles_chat
              ON raffles(chat_id,status,id);
            CREATE TABLE IF NOT EXISTS raffle_entries (
              raffle_id INTEGER NOT NULL, chat_id INTEGER NOT NULL,
              user_id INTEGER NOT NULL, created_at INTEGER NOT NULL,
              PRIMARY KEY(raffle_id,user_id)
            );
        """)
        # Additive migrations for existing installations; preserve old replies.
        existing = {r[1] for r in self.db.execute("PRAGMA table_info(replies)")}
        for column, definition in (
            ("buttons", "TEXT NOT NULL DEFAULT '[]'"),
            ("media_type", "TEXT"),
            ("media_id", "TEXT"),
        ):
            if column not in existing:
                self.db.execute(f"ALTER TABLE replies ADD COLUMN {column} {definition}")

    def ensure_chat(self, chat_id, title):
        chat_id = int(chat_id)
        title = str(title or "未命名群组")[:150]
        now = int(time.time())
        self.db.execute(
            "INSERT OR IGNORE INTO chats VALUES(?,?,?,?)",
            (chat_id, title, json.dumps(DEFAULTS, ensure_ascii=False), now))
        self.db.execute("UPDATE chats SET title=?,updated_at=? WHERE chat_id=? AND title<>?",
                        (title, now, chat_id, title))

    def chat(self, chat_id):
        row = self.db.execute("SELECT * FROM chats WHERE chat_id=?", (int(chat_id),)).fetchone()
        return dict(row) if row else None

    def chats(self):
        return [dict(r) for r in self.db.execute(
            "SELECT chat_id,title FROM chats ORDER BY updated_at DESC LIMIT 100")]

    def settings(self, chat_id):
        row = self.db.execute("SELECT settings FROM chats WHERE chat_id=?", (int(chat_id),)).fetchone()
        if row is None:
            raise KeyError("该群尚未加入机器人")
        return {**DEFAULTS, **json.loads(row[0])}

    def set(self, chat_id, name, value):
        if name not in DEFAULTS:
            raise ValueError("未知设置")
        settings = self.settings(chat_id)
        if name in TOGGLES and not isinstance(value, bool):
            raise ValueError("开关必须是布尔值")
        if name in ("welcome_text", "rules_text") and (not isinstance(value, str) or len(value) > 1500):
            raise ValueError("文本最多 1500 字")
        if name == "log_chat_id" and (not isinstance(value, int) or value > 0):
            raise ValueError("日志目标 ID 必须是负整数或 0")
        if name == "verify_timeout" and (not isinstance(value, int) or not 60 <= value <= 1800):
            raise ValueError("验证时限为 60-1800 秒")
        if name == "checkin_award" and (
            type(value) is not int or not 1 <= value <= 100):
            raise ValueError("签到基础积分需要在 1 到 100 之间")
        if name == "repeat_photo_limit" and (
            type(value) is not int or not 2 <= value <= 10):
            raise ValueError("相同图片触发次数需要在 2 到 10 次之间")
        if name == "repeat_photo_window" and (
            type(value) is not int or not 30 <= value <= 600):
            raise ValueError("图片检测时间需要在 30 到 600 秒之间")
        if name == "ocr_mode" and value not in ("off", "review", "delete"):
            raise ValueError("OCR 模式只能是 off、review、delete")
        if name == "ocr_confidence" and (
            type(value) is not int or not 40 <= value <= 95):
            raise ValueError("OCR 信心阈值范围为 40–95")
        if name in NUMBER_LIMITS:
            lo, hi = NUMBER_LIMITS[name]
            if type(value) is not int or not lo <= value <= hi:
                raise ValueError(f"{name} 的设置范围为 {lo}–{hi}")
        settings[name] = value
        self.db.execute("UPDATE chats SET settings=?,updated_at=? WHERE chat_id=?",
                        (json.dumps(settings, ensure_ascii=False), int(time.time()), int(chat_id)))
        return settings

    def add_filter(self, chat_id, kind, key, value):
        if kind not in ("word", "domain", "word_exception"):
            raise ValueError("未知类型")
        count = self.db.execute("SELECT COUNT(*) FROM filters WHERE chat_id=? AND kind=?",
                                (int(chat_id), kind)).fetchone()[0]
        exists = self.db.execute(
            "SELECT 1 FROM filters WHERE chat_id=? AND kind=? AND key=?",
            (int(chat_id), kind, str(key))).fetchone()
        if count >= 300 and not exists:
            raise ValueError("每类规则最多 300 条")
        self.db.execute("INSERT OR REPLACE INTO filters VALUES (?,?,?,?)",
                        (int(chat_id), kind, str(key), str(value)))

    def del_filter(self, chat_id, kind, key):
        cursor = self.db.execute("DELETE FROM filters WHERE chat_id=? AND kind=? AND key=?",
                                 (int(chat_id), kind, str(key)))
        return cursor.rowcount > 0

    def filters(self, chat_id, kind):
        return [dict(row) for row in self.db.execute(
            "SELECT key,value FROM filters WHERE chat_id=? AND kind=? ORDER BY key",
            (int(chat_id), kind))]

    def filter_count(self, chat_id, kind):
        """Count rules without loading their values into the admin panel."""
        if kind not in ("word", "domain", "word_exception"):
            raise ValueError("未知规则类型")
        return int(self.db.execute(
            "SELECT COUNT(*) FROM filters WHERE chat_id=? AND kind=?",
            (int(chat_id), kind)).fetchone()[0])

    def add_filters_bulk(self, chat_id, kind, entries):
        """All-or-nothing import. Same-group, existing rules may be replaced."""
        if kind not in ("word", "word_exception"):
            raise ValueError("仅支持批量导入违禁词或例外短语")
        if not entries or len(entries) > 80:
            raise ValueError("一次最多提交 80 条规则")
        unique = {str(key): str(value) for key, value in entries}
        self.db.execute("BEGIN IMMEDIATE")
        try:
            existing = {r[0] for r in self.db.execute(
                "SELECT key FROM filters WHERE chat_id=? AND kind=?",
                (int(chat_id), kind))}
            if len(existing | set(unique)) > 300:
                raise ValueError("超过每群每类 300 条上限，本次没有导入任何规则")
            for key, value in unique.items():
                self.db.execute("INSERT OR REPLACE INTO filters VALUES (?,?,?,?)",
                                (int(chat_id), kind, key, value))
            self.db.execute("COMMIT")
        except Exception:
            self.db.execute("ROLLBACK")
            raise
        return len(unique)

    def record_moderation(self, chat_id, user_id, message_id, reason, now=None,
                          window=600):
        """Count distinct successfully deleted messages for opt-in escalation."""
        moment = int(time.time() if now is None else now)
        self.db.execute("BEGIN IMMEDIATE")
        try:
            created = bool(self.db.execute(
                "INSERT OR IGNORE INTO moderation_events "
                "(chat_id,message_id,user_id,reason,created_at) VALUES(?,?,?,?,?)",
                (int(chat_id), int(message_id), int(user_id), str(reason)[:75],
                 moment)).rowcount)
            recent = self.db.execute(
                "SELECT count(*) FROM moderation_events "
                "WHERE chat_id=? AND user_id=? AND created_at>=?",
                (int(chat_id), int(user_id), moment - int(window))).fetchone()[0]
            # Avoid retaining moderation histories forever.
            self.db.execute(
                "DELETE FROM moderation_events WHERE created_at<?",
                (moment - 86400 * 7,))
            self.db.execute("COMMIT")
            return {"created": created, "recent": recent}
        except Exception:
            self.db.execute("ROLLBACK")
            raise

    def add_reply(self, chat_id, key, keyword, reply,
                  buttons=None, media_type=None, media_id=None):
        if media_type not in (None, "photo", "video", "document", "animation"):
            raise ValueError("不支持的媒体类型")
        if media_type and (not media_id or len(media_id) > 512):
            raise ValueError("Telegram 文件 ID 无效")
        if not isinstance(buttons if buttons is not None else [], list):
            raise ValueError("按钮格式无效")
        buttons = [] if buttons is None else buttons
        if len(buttons) > 3 or any(
                not isinstance(b, dict) or set(b) != {"text", "url"}
                for b in buttons):
            raise ValueError("最多允许 3 个链接按钮")
        count = self.db.execute("SELECT COUNT(*) FROM replies WHERE chat_id=?",
                                (int(chat_id),)).fetchone()[0]
        exists = self.db.execute("SELECT 1 FROM replies WHERE chat_id=? AND key=?",
                                 (int(chat_id), str(key))).fetchone()
        if count >= 100 and not exists:
            raise ValueError("最多 100 条自动回复")
        self.db.execute(
            "INSERT OR REPLACE INTO replies "
            "(chat_id,key,keyword,reply,buttons,media_type,media_id) "
            "VALUES(?,?,?,?,?,?,?)",
            (int(chat_id), str(key), str(keyword), str(reply),
             json.dumps(buttons, ensure_ascii=False), media_type, media_id))

    def del_reply(self, chat_id, key):
        cursor = self.db.execute("DELETE FROM replies WHERE chat_id=? AND key=?",
                                 (int(chat_id), str(key)))
        return cursor.rowcount > 0

    def replies(self, chat_id):
        return [dict(r) for r in self.db.execute(
            "SELECT key,keyword,reply,buttons,media_type,media_id "
            "FROM replies WHERE chat_id=? ORDER BY key", (int(chat_id),))]

    def warning(self, chat_id, user_id, delta=1):
        existing = self.db.execute("SELECT count FROM warnings WHERE chat_id=? AND user_id=?",
                                   (int(chat_id), int(user_id))).fetchone()
        count = max(0, (existing[0] if existing else 0) + delta)
        self.db.execute("INSERT INTO warnings VALUES(?,?,?) ON CONFLICT(chat_id,user_id)"
                        " DO UPDATE SET count=excluded.count", (int(chat_id), int(user_id), count))
        return count

    def clear_warning(self, chat_id, user_id):
        self.db.execute("DELETE FROM warnings WHERE chat_id=? AND user_id=?",
                        (int(chat_id), int(user_id)))

    def put_pending(self, chat_id, user_id, nonce, answer, expires_at, message_id=None):
        self.db.execute("INSERT OR REPLACE INTO pending VALUES(?,?,?,?,?,?)",
                        (int(chat_id), int(user_id), nonce, int(answer),
                         int(expires_at), message_id))

    def pending(self, chat_id, user_id):
        row = self.db.execute("SELECT * FROM pending WHERE chat_id=? AND user_id=?",
                              (int(chat_id), int(user_id))).fetchone()
        return dict(row) if row else None

    def expired(self, now=None):
        when = int(time.time()) if now is None else int(now)
        return [dict(r) for r in self.db.execute(
            "SELECT * FROM pending WHERE expires_at<=? ORDER BY expires_at LIMIT 100", (when,))]

    def del_pending(self, chat_id, user_id):
        self.db.execute("DELETE FROM pending WHERE chat_id=? AND user_id=?",
                        (int(chat_id), int(user_id)))

    def stat(self, chat_id, name, delta=1, when=None):
        day = datetime.fromtimestamp(int(time.time()) if when is None else when,
                                     self.tz).strftime("%Y-%m-%d")
        self.db.execute(
            "INSERT INTO stats VALUES(?,?,?,?) ON CONFLICT(chat_id,day,name)"
            " DO UPDATE SET count=count+excluded.count",
            (int(chat_id), day, str(name), int(delta)))

    def stats(self, chat_id, day=None):
        day = day or datetime.now(self.tz).strftime("%Y-%m-%d")
        return {r["name"]: r["count"] for r in self.db.execute(
            "SELECT name,count FROM stats WHERE chat_id=? AND day=?",
            (int(chat_id), day))}

    def stats_days(self, chat_id, days=7, now=None):
        if not 1 <= days <= 31:
            raise ValueError("统计范围仅允许 1-31 天")
        now_dt = datetime.fromtimestamp(time.time() if now is None else now, self.tz)
        first = (now_dt.date() - timedelta(days=days - 1)).isoformat()
        last = now_dt.date().isoformat()
        return {r["name"]: r["total"] for r in self.db.execute(
            "SELECT name,SUM(count) AS total FROM stats "
            "WHERE chat_id=? AND day BETWEEN ? AND ? GROUP BY name",
            (int(chat_id), first, last))}

    def record_activity(self, chat_id, user_id, when=None):
        day = datetime.fromtimestamp(time.time() if when is None else when,
                                     self.tz).date().isoformat()
        self.db.execute(
            "INSERT INTO activity VALUES (?,?,?,1) "
            "ON CONFLICT(chat_id,day,user_id) DO UPDATE SET count=count+1",
            (int(chat_id), day, int(user_id)))

    def top_activity(self, chat_id, days=7, limit=10, now=None):
        if not 1 <= days <= 31 or not 1 <= limit <= 25:
            raise ValueError("排行榜范围无效")
        now_dt = datetime.fromtimestamp(time.time() if now is None else now, self.tz)
        first = (now_dt.date() - timedelta(days=days - 1)).isoformat()
        last = now_dt.date().isoformat()
        return [dict(r) for r in self.db.execute(
            "SELECT user_id,SUM(count) AS count FROM activity "
            "WHERE chat_id=? AND day BETWEEN ? AND ? "
            "GROUP BY user_id ORDER BY count DESC,user_id ASC LIMIT ?",
            (int(chat_id), first, last, int(limit)))]

    def prune_activity(self, now=None, retain_days=30):
        now_dt = datetime.fromtimestamp(time.time() if now is None else now, self.tz)
        cutoff = (now_dt.date() - timedelta(days=retain_days)).isoformat()
        return self.db.execute("DELETE FROM activity WHERE day<?", (cutoff,)).rowcount

    def add_schedule(self, chat_id, owner_id, title, body, mode, rule, next_run):
        if not self.chat(chat_id):
            raise ValueError("群组尚未登记")
        if mode not in ("once", "daily", "weekly"):
            raise ValueError("不支持的定时类型")
        if not 1 <= len(title) <= 60 or not 1 <= len(body) <= 3500:
            raise ValueError("标题最多 60 字，内容最多 3500 字，均不能为空")
        count = self.db.execute(
            "SELECT count(*) FROM scheduled_messages WHERE chat_id=?",
            (int(chat_id),)).fetchone()[0]
        if count >= 20:
            raise ValueError("单群最多保存 20 条定时消息，可先删除旧任务")
        cursor = self.db.execute(
            "INSERT INTO scheduled_messages "
            "(chat_id,title,body,mode,rule,next_run,owner_id,created_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (int(chat_id), title, body, mode, rule, int(next_run),
             int(owner_id), int(time.time())))
        return cursor.lastrowid

    def schedules(self, chat_id):
        return [dict(r) for r in self.db.execute(
            "SELECT * FROM scheduled_messages WHERE chat_id=? ORDER BY id DESC LIMIT 20",
            (int(chat_id),))]

    def schedule(self, chat_id, task_id):
        row = self.db.execute(
            "SELECT * FROM scheduled_messages WHERE chat_id=? AND id=?",
            (int(chat_id), int(task_id))).fetchone()
        return dict(row) if row else None

    def delete_schedule(self, chat_id, task_id):
        return bool(self.db.execute(
            "DELETE FROM scheduled_messages WHERE chat_id=? AND id=?",
            (int(chat_id), int(task_id))).rowcount)

    def toggle_schedule(self, chat_id, task_id, enabled, next_run=None):
        row = self.schedule(chat_id, task_id)
        if not row:
            return False
        if enabled:
            if next_run is None:
                raise ValueError("恢复定时任务时必须重算下次发送时间")
            self.db.execute(
                "UPDATE scheduled_messages SET enabled=1,next_run=?,"
                "failures=0,claim_until=0 WHERE id=? AND chat_id=?",
                (int(next_run), int(task_id), int(chat_id)))
        else:
            self.db.execute(
                "UPDATE scheduled_messages SET enabled=0,claim_until=0 "
                "WHERE id=? AND chat_id=?",
                (int(task_id), int(chat_id)))
        return True

    def due_schedules(self, now=None, limit=10):
        moment = int(time.time() if now is None else now)
        return [dict(r) for r in self.db.execute(
            "SELECT * FROM scheduled_messages WHERE enabled=1 AND next_run<=? "
            "AND claim_until<=? ORDER BY next_run,id LIMIT ?",
            (moment, moment, max(1, min(10, int(limit)))))]

    def claim_schedule(self, task_id, now=None, lease=90):
        moment = int(time.time() if now is None else now)
        return bool(self.db.execute(
            "UPDATE scheduled_messages SET claim_until=? WHERE id=? AND "
            "enabled=1 AND next_run<=? AND claim_until<=?",
            (moment + lease, int(task_id), moment, moment)).rowcount)

    def schedule_failed(self, task_id, now=None):
        moment = int(time.time() if now is None else now)
        row = self.db.execute(
            "SELECT failures FROM scheduled_messages WHERE id=?", (int(task_id),)).fetchone()
        if row is None:
            return
        failures = row[0] + 1
        pause = min(1800, 60 * (2 ** min(failures - 1, 5)))
        self.db.execute(
            "UPDATE scheduled_messages SET failures=?,claim_until=? WHERE id=?",
            (failures, moment + pause, int(task_id)))

    def finish_schedule(self, task_id, sent=False, next_run=None, now=None):
        moment = int(time.time() if now is None else now)
        if next_run is None:  # One-time or expired task: mark finished.
            self.db.execute(
                "UPDATE scheduled_messages SET enabled=0,claim_until=0,"
                "failures=0,last_sent=CASE WHEN ? THEN ? ELSE last_sent END "
                "WHERE id=?", (int(bool(sent)), moment, int(task_id)))
        else:
            self.db.execute(
                "UPDATE scheduled_messages SET enabled=1,next_run=?,claim_until=0,"
                "failures=0,last_sent=CASE WHEN ? THEN ? ELSE last_sent END "
                "WHERE id=?", (int(next_run), int(bool(sent)), moment, int(task_id)))

    def offset(self):
        row = self.db.execute("SELECT value FROM meta WHERE key='last_update_id'").fetchone()
        return int(row[0]) if row else -1

    def save_offset(self, update_id):
        self.db.execute("INSERT INTO meta VALUES ('last_update_id',?)"
                        " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                        (str(int(update_id)),))

    def close(self):
        self.db.close()

    def point_balance(self, chat_id, user_id):
        row = self.db.execute(
            "SELECT balance,checkins,streak,last_checkin FROM point_balances "
            "WHERE chat_id=? AND user_id=?", (int(chat_id), int(user_id))).fetchone()
        return dict(row) if row else {
            "balance": 0, "checkins": 0, "streak": 0, "last_checkin": None}

    def checkin(self, chat_id, user_id, now=None):
        """Exactly one award per local calendar day, even across processes."""
        moment = int(time.time() if now is None else now)
        day = datetime.fromtimestamp(moment, self.tz).date()
        if not self.settings(chat_id)["points_enabled"]:
            raise ValueError("本群尚未开启签到积分")
        uid, cid = int(user_id), int(chat_id)
        self.db.execute("BEGIN IMMEDIATE")
        try:
            row = self.db.execute(
                "SELECT balance,checkins,streak,last_checkin FROM point_balances "
                "WHERE chat_id=? AND user_id=?", (cid, uid)).fetchone()
            old_balance = row["balance"] if row else 0
            if row and row["last_checkin"] == day.isoformat():
                self.db.execute("COMMIT")
                return {"created": False, **dict(row), "award": 0}
            yesterday = (day - timedelta(days=1)).isoformat()
            streak = row["streak"] + 1 if row and row["last_checkin"] == yesterday else 1
            award = self.settings(chat_id)["checkin_award"] + min(streak - 1, 6)
            new_balance = old_balance + award
            if new_balance > 1_000_000_000:
                raise ValueError("积分已达上限")
            checkins = (row["checkins"] if row else 0) + 1
            self.db.execute(
                "INSERT INTO point_checkins VALUES(?,?,?,?,?)",
                (cid, uid, day.isoformat(), award, streak))
            self.db.execute(
                "INSERT INTO point_balances VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(chat_id,user_id) DO UPDATE SET "
                "balance=excluded.balance,checkins=excluded.checkins,"
                "streak=excluded.streak,last_checkin=excluded.last_checkin",
                (cid, uid, new_balance, checkins, streak, day.isoformat()))
            self.db.execute(
                "INSERT INTO point_events (chat_id,user_id,delta,balance,kind,actor_id,created_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (cid, uid, award, new_balance, "checkin", uid, moment))
            self.db.execute("COMMIT")
            return {"created": True, "balance": new_balance, "checkins": checkins,
                    "streak": streak, "award": award, "last_checkin": day.isoformat()}
        except Exception:
            self.db.execute("ROLLBACK")
            raise

    def change_points(self, chat_id, user_id, delta, actor_id, now=None):
        """Manual adjustment with durable audit and protection from negative balances."""
        if type(delta) is not int or delta == 0 or abs(delta) > 10_000:
            raise ValueError("单次调整必须在 1 到 10000 积分之间")
        cid, uid, actor = int(chat_id), int(user_id), int(actor_id)
        moment = int(time.time() if now is None else now)
        self.db.execute("BEGIN IMMEDIATE")
        try:
            row = self.db.execute(
                "SELECT balance FROM point_balances WHERE chat_id=? AND user_id=?",
                (cid, uid)).fetchone()
            current = row[0] if row else 0
            result = current + delta
            if not 0 <= result <= 1_000_000_000:
                raise ValueError("调整失败：积分不能为负数，也不能超过上限")
            self.db.execute(
                "INSERT INTO point_balances (chat_id,user_id,balance) VALUES(?,?,?) "
                "ON CONFLICT(chat_id,user_id) DO UPDATE SET balance=excluded.balance",
                (cid, uid, result))
            self.db.execute(
                "INSERT INTO point_events (chat_id,user_id,delta,balance,kind,actor_id,created_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (cid, uid, delta, result, "admin", actor, moment))
            self.db.execute("COMMIT")
            return result
        except Exception:
            self.db.execute("ROLLBACK")
            raise

    def point_top(self, chat_id, limit=10):
        return [dict(r) for r in self.db.execute(
            "SELECT user_id,balance,checkins,streak FROM point_balances "
            "WHERE chat_id=? AND balance>0 ORDER BY balance DESC,user_id ASC LIMIT ?",
            (int(chat_id), max(1, min(25, int(limit)))))]

    def point_events(self, chat_id, limit=15, user_id=None):
        if user_id is None:
            sql = ("SELECT user_id,delta,balance,kind,actor_id,created_at "
                   "FROM point_events WHERE chat_id=? ORDER BY id DESC LIMIT ?")
            params = (int(chat_id), max(1, min(30, int(limit))))
        else:
            sql = ("SELECT user_id,delta,balance,kind,actor_id,created_at "
                   "FROM point_events WHERE chat_id=? AND user_id=? ORDER BY id DESC LIMIT ?")
            params = (int(chat_id), int(user_id), max(1, min(30, int(limit))))
        return [dict(r) for r in self.db.execute(sql, params)]
