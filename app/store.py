"""SQLite state is scoped to chat_id; no Telegram credentials enter the database."""
from .i18n import msg as _ui, join_text
import json
import os
import sqlite3
import time
from collections import OrderedDict
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from .moderation import WORD_MODES, normalize
from .word_library import WordPolicy, catalog, PACK_MODES
from .variants import VARIANT_SETTINGS
from .flood_penalty import FLOOD_SETTINGS, PENALTIES

DEFAULTS = {
    "block_links": True,
    "block_forward": False,
    "anti_flood": True,
    "welcome_enabled": True,
    "welcome_text": _ui('欢迎 {name} 加入本群！请先阅读 /rules。'),
    "rules_text": _ui('文明交流，禁止广告、诈骗和刷屏。尊重每一位群友。'),
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
    "word_traditional_mode": "off",
    "word_homophone_mode": "off",
    "word_short_split_mode": "off",
    "word_mixed_pinyin_mode": "off",
    "word_pinyin_initials_mode": "off",
    "word_phonetic_mode": "off",
    "word_filter_enabled": True,
    "word_penalty": "delete",
    "word_mute_minutes": 10,
    "word_notice_seconds": 0,
    "flood_limit": 7,
    "flood_window": 10,
    "repeat_limit": 3,
    "repeat_window": 45,
    "flood_penalty": "delete",
    "flood_mute_minutes": 10,
    "flood_ban_minutes": 1440,
    "cross_flood_mode": "off",
    "cross_flood_limit": 5,
    "cross_flood_users": 3,
    "cross_flood_window": 60,
    "cross_flood_min_chars": 8,
    "flood_notice_seconds": 0,
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
    "observe_only": False,
    "block_external_channels": False,
    "album_single_enabled": False,
    "reply_group_limit": 20,
    "verify_attempt_limit": 3,
    "raid_enabled": False,
    "raid_join_limit": 10,
    "raid_window": 60,
    "raid_duration": 10,
    "welcome_limit": 10,
}
TOGGLES = {"block_links", "block_forward", "anti_flood", "welcome_enabled",
           "verify_enabled", "block_new_bots", "points_enabled",
           "repeat_photo_enabled", "spam_guard_enabled",
           "word_loose_match", "word_filter_enabled",
           "auto_mute_enabled", "media_flood_enabled",
           "name_guard_enabled", "observe_only", "block_external_channels",
           "album_single_enabled", "raid_enabled"}

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
    "word_mute_minutes": (5, 1440),
    "flood_mute_minutes": (1, 525600),
    "flood_ban_minutes": (0, 525600),
    "cross_flood_limit": (3, 30),
    "cross_flood_users": (2, 30),
    "cross_flood_window": (5, 300),
    "cross_flood_min_chars": (2, 200),
    "verify_timeout": (60, 1800),
    "verify_attempt_limit": (1, 5),
    "repeat_photo_limit": (2, 10),
    "repeat_photo_window": (30, 600),
    "ocr_confidence": (40, 95),
    "checkin_award": (1, 100),
    "reply_group_limit": (1, 60),
    "warn_limit": (1, 10),
    "raid_join_limit": (3, 100),
    "raid_window": (10, 300),
    "raid_duration": (1, 60),
    "welcome_limit": (1, 60),
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
        self._word_cache = OrderedDict()
        self._init()

    def _init(self):
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS chats (
              chat_id INTEGER PRIMARY KEY, title TEXT NOT NULL,
              settings TEXT NOT NULL, updated_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS ui_languages (
              owner_id INTEGER PRIMARY KEY, locale TEXT NOT NULL
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
            CREATE TABLE IF NOT EXISTS pending_notice_deletions (
              chat_id INTEGER NOT NULL,
              message_id INTEGER NOT NULL,
              delete_at INTEGER NOT NULL,
              PRIMARY KEY(chat_id, message_id)
            );
            CREATE INDEX IF NOT EXISTS idx_pending_notice_deletions_due
              ON pending_notice_deletions(delete_at);
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
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS word_modes (
              chat_id INTEGER NOT NULL, kind TEXT NOT NULL DEFAULT 'word' CHECK(kind='word'),
              key TEXT NOT NULL, mode TEXT NOT NULL CHECK(mode IN ('contains','word','exact')),
              PRIMARY KEY(chat_id,kind,key),
              FOREIGN KEY(chat_id,kind,key) REFERENCES filters(chat_id,kind,key) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS word_packs (
              chat_id INTEGER NOT NULL, pack_id TEXT NOT NULL,
              mode TEXT NOT NULL CHECK(mode IN ('off','observe','enforce')),
              PRIMARY KEY(chat_id,pack_id)
            );
            CREATE TABLE IF NOT EXISTS word_exclusions (
              chat_id INTEGER NOT NULL, key TEXT NOT NULL, PRIMARY KEY(chat_id,key)
            );
            CREATE TABLE IF NOT EXISTS word_revisions (
              chat_id INTEGER PRIMARY KEY, version INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS raid_state (
              chat_id INTEGER PRIMARY KEY, until_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS moderation_audit (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              chat_id INTEGER NOT NULL, message_id INTEGER NOT NULL,
              target_id INTEGER NOT NULL, reason TEXT NOT NULL,
              source TEXT NOT NULL, outcome TEXT NOT NULL,
              reviewed_by INTEGER, created_at INTEGER NOT NULL,
              UNIQUE(chat_id,message_id,source)
            );
            CREATE INDEX IF NOT EXISTS idx_audit_chat
              ON moderation_audit(chat_id,id);
            CREATE INDEX IF NOT EXISTS idx_audit_time
              ON moderation_audit(created_at);
            CREATE TABLE IF NOT EXISTS auto_flood_actions (
              chat_id INTEGER NOT NULL, message_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
              mode TEXT NOT NULL, created_at INTEGER NOT NULL,
              PRIMARY KEY(chat_id,message_id)
            );
            CREATE INDEX IF NOT EXISTS idx_flood_action_time ON auto_flood_actions(created_at);
            CREATE TABLE IF NOT EXISTS ui_purchase_requests (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              chat_id INTEGER NOT NULL, user_id INTEGER NOT NULL, token TEXT NOT NULL,
              UNIQUE(chat_id,user_id,token)
            );
        """)
        pending_columns = {r[1] for r in self.db.execute("PRAGMA table_info(pending)")}
        if "attempts" not in pending_columns:
            self.db.execute("ALTER TABLE pending ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0")
        # Additive migrations for existing installations; preserve old replies.
        existing = {r[1] for r in self.db.execute("PRAGMA table_info(replies)")}
        for column, definition in (
            ("buttons", "TEXT NOT NULL DEFAULT '[]'"),
            ("media_type", "TEXT"),
            ("media_id", "TEXT"),
        ):
            if column not in existing:
                self.db.execute(_ui('ALTER TABLE replies ADD COLUMN {p0} {p1}', p0=column, p1=definition))
        audit_columns = {r[1] for r in self.db.execute("PRAGMA table_info(moderation_audit)")}
        for column in ("rule_key", "pack_id", "match_kind", "penalty", "penalty_result"):
            if column not in audit_columns:
                self.db.execute(_ui("ALTER TABLE moderation_audit ADD COLUMN {p0} TEXT NOT NULL DEFAULT ''", p0=column))
        # Database triggers also invalidate caches after direct SQL/another connection.
        for table in ("filters", "word_modes", "word_packs", "word_exclusions"):
            for operation in ("INSERT", "UPDATE", "DELETE"):
                row = "OLD" if operation == "DELETE" else "NEW"
                condition = ""
                if table == "filters":
                    condition = _ui("WHEN {p0}.kind IN ('word','word_exception')", p0=row)
                    if operation == "UPDATE":
                        condition += " OR OLD.kind IN ('word','word_exception')"
                body = (_ui('INSERT INTO word_revisions VALUES({p0}.chat_id,1) ON CONFLICT(chat_id) DO UPDATE SET version=version+1;', p0=row))
                if operation == "UPDATE":
                    body += ("INSERT INTO word_revisions SELECT OLD.chat_id,1 WHERE OLD.chat_id<>NEW.chat_id "
                             "ON CONFLICT(chat_id) DO UPDATE SET version=version+1;")
                self.db.execute(_ui('CREATE TRIGGER IF NOT EXISTS word_rev_{p0}_{p1} AFTER {p2} ON {p3} {p4} BEGIN {p5} END', p0=table, p1=operation, p2=operation, p3=table, p4=condition, p5=body))

    def ensure_chat(self, chat_id, title):
        chat_id = int(chat_id)
        title = str(title or _ui('未命名群组'))[:150]
        now = int(time.time())
        self.db.execute(
            "INSERT OR IGNORE INTO chats VALUES(?,?,?,?)",
            (chat_id, title, json.dumps(DEFAULTS, ensure_ascii=False), now))
        self.db.execute("UPDATE chats SET title=?,updated_at=? WHERE chat_id=? AND title<>?",
                        (title, now, chat_id, title))

    def chat(self, chat_id):
        row = self.db.execute("SELECT * FROM chats WHERE chat_id=?", (int(chat_id),)).fetchone()
        return dict(row) if row else None

    def ui_language(self, owner_id):
        from .i18n import LOCALES
        row = self.db.execute("SELECT locale FROM ui_languages WHERE owner_id=?", (int(owner_id),)).fetchone()
        return row[0] if row and row[0] in LOCALES else "zh-CN"

    def has_ui_language(self, owner_id):
        return self.db.execute("SELECT 1 FROM ui_languages WHERE owner_id=?", (int(owner_id),)).fetchone() is not None

    def set_ui_language(self, owner_id, locale):
        from .i18n import LOCALES
        if locale not in LOCALES or not int(owner_id):
            raise ValueError(_ui('未知界面语言。'))
        if int(owner_id) < 0 and not self.chat(owner_id):
            raise ValueError(_ui('该群尚未加入机器人'))
        self.db.execute("INSERT INTO ui_languages VALUES(?,?) ON CONFLICT(owner_id) DO UPDATE SET locale=excluded.locale",
                        (int(owner_id), locale))

    def chats(self):
        return [dict(r) for r in self.db.execute(
            "SELECT chat_id,title FROM chats ORDER BY updated_at DESC LIMIT 100")]

    def settings(self, chat_id):
        row = self.db.execute("SELECT settings FROM chats WHERE chat_id=?", (int(chat_id),)).fetchone()
        if row is None:
            raise KeyError(_ui('该群尚未加入机器人'))
        return {**DEFAULTS, **json.loads(row[0])}

    def content_text(self, chat_id, key):
        """Translate untouched defaults; explicit administrator content stays literal."""
        if key not in ("welcome_text", "rules_text"):
            raise KeyError(key)
        settings = self.settings(chat_id)
        value = settings[key]
        if value == DEFAULTS[key] and not settings.get(key+"_customized", False):
            return DEFAULTS[key]
        return str(value)

    def set(self, chat_id, name, value):
        if name not in DEFAULTS:
            raise ValueError(_ui('未知设置'))
        settings = self.settings(chat_id)
        if name in TOGGLES and not isinstance(value, bool):
            raise ValueError(_ui('开关必须是布尔值'))
        if name in ("welcome_text", "rules_text") and (not isinstance(value, str) or len(value) > 1500):
            raise ValueError(_ui('文本最多 1500 字'))
        if name == "log_chat_id" and (not isinstance(value, int) or value > 0):
            raise ValueError(_ui('日志目标 ID 必须是负整数或 0'))
        if name == "verify_timeout" and (not isinstance(value, int) or not 60 <= value <= 1800):
            raise ValueError(_ui('验证时限为 60-1800 秒'))
        if name == "checkin_award" and (
            type(value) is not int or not 1 <= value <= 100):
            raise ValueError(_ui('签到基础积分需要在 1 到 100 之间'))
        if name == "repeat_photo_limit" and (
            type(value) is not int or not 2 <= value <= 10):
            raise ValueError(_ui('相同图片触发次数需要在 2 到 10 次之间'))
        if name == "repeat_photo_window" and (
            type(value) is not int or not 30 <= value <= 600):
            raise ValueError(_ui('图片检测时间需要在 30 到 600 秒之间'))
        if name == "ocr_mode" and value not in ("off", "review", "delete"):
            raise ValueError(_ui('OCR 模式只能是 off、review、delete'))
        if name == "ocr_confidence" and (
            type(value) is not int or not 40 <= value <= 95):
            raise ValueError(_ui('OCR 信心阈值范围为 40–95'))
        if name == "word_penalty" and value not in ("delete", "mute"):
            raise ValueError(_ui('违禁词处罚只能为 delete 或 mute'))
        if name in {row[0] for row in VARIANT_SETTINGS.values()} and value not in PACK_MODES:
            raise ValueError(_ui('变体识别模式只能为 off、observe、enforce'))
        if name == "flood_penalty" and value not in PENALTIES:
            raise ValueError(_ui('未知防刷屏处罚方式'))
        if name == "cross_flood_mode" and value not in ("off", "observe", "enforce"):
            raise ValueError(_ui('跨账号检测模式只能为 off、observe、enforce'))
        if name == "word_notice_seconds" and (
            type(value) is not int or value not in (0, 10, 30, 60)):
            raise ValueError(_ui('违禁词删除提醒只能为 0、10、30、60 秒'))
        if name == "flood_notice_seconds" and (
            type(value) is not int or value not in (0, 10, 30, 60)):
            raise ValueError(_ui('防刷屏删除提醒只能为 0、10、30、60 秒'))
        if name in NUMBER_LIMITS:
            lo, hi = NUMBER_LIMITS[name]
            if type(value) is not int or not lo <= value <= hi:
                raise ValueError(_ui('{p0} 的设置范围为 {p1}–{p2}', p0=name, p1=lo, p2=hi))
        settings[name] = value
        if name in ("welcome_text", "rules_text"):
            settings[name+"_customized"] = True
        self.db.execute("UPDATE chats SET settings=?,updated_at=? WHERE chat_id=?",
                        (json.dumps(settings, ensure_ascii=False), int(time.time()), int(chat_id)))
        return settings

    def add_filter(self, chat_id, kind, key, value):
        if kind not in ("word", "domain", "word_exception"):
            raise ValueError(_ui('未知类型'))
        if kind == "domain":
            count = self.db.execute("SELECT COUNT(*) FROM filters WHERE chat_id=? AND kind=?",
                                    (int(chat_id), kind)).fetchone()[0]
            exists = self.db.execute(
                "SELECT 1 FROM filters WHERE chat_id=? AND kind=? AND key=?",
                (int(chat_id), kind, str(key))).fetchone()
            if count >= 300 and not exists:
                raise ValueError(_ui('域名规则最多 300 条'))
        self.db.execute("INSERT INTO filters VALUES (?,?,?,?) "
                        "ON CONFLICT(chat_id,kind,key) DO UPDATE SET value=excluded.value",
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
            raise ValueError(_ui('未知规则类型'))
        return int(self.db.execute(
            "SELECT COUNT(*) FROM filters WHERE chat_id=? AND kind=?",
            (int(chat_id), kind)).fetchone()[0])

    def filter_page(self, chat_id, kind, page=0, page_size=8):
        """User-requested list only; never expand the main panel."""
        if kind != "word":
            raise ValueError(_ui('只支持查看当前群的违禁词列表'))
        if type(page) is not int or page < 0 or page > 2**63 // max(1, page_size)-1:
            raise ValueError(_ui('列表页码无效'))
        return [dict(row) for row in self.db.execute(
            "SELECT key,value FROM filters WHERE chat_id=? AND kind=? "
            "ORDER BY key LIMIT ? OFFSET ?",
            (int(chat_id), kind, int(page_size), int(page * page_size)))]

    def schedule_notice_deletion(self, chat_id, message_id, delete_at):
        """Track only bot's ephemeral warning message IDs; never user content."""
        self.db.execute(
            "INSERT OR REPLACE INTO pending_notice_deletions "
            "(chat_id,message_id,delete_at) VALUES(?,?,?)",
            (int(chat_id), int(message_id), int(delete_at)))

    def pending_notice_deletion_due(self, now=None, limit=40):
        now = int(time.time() if now is None else now)
        return [dict(row) for row in self.db.execute(
            "SELECT chat_id,message_id,delete_at FROM pending_notice_deletions "
            "WHERE delete_at<=? ORDER BY delete_at LIMIT ?",
            (now, min(40, max(1, int(limit)))))]

    def has_pending_notice_deletions(self):
        return bool(self.db.execute(
            "SELECT 1 FROM pending_notice_deletions LIMIT 1").fetchone())

    def clear_notice_deletion(self, chat_id, message_id):
        self.db.execute("DELETE FROM pending_notice_deletions "
                        "WHERE chat_id=? AND message_id=?",
                        (int(chat_id), int(message_id)))

    def add_filters_bulk(self, chat_id, kind, entries):
        """All-or-nothing import. Same-group, existing rules may be replaced."""
        if kind not in ("word", "word_exception", "domain"):
            raise ValueError(_ui('仅支持批量导入违禁词、域名或例外短语'))
        if not entries or len(entries) > 80:
            raise ValueError(_ui('一次最多提交 80 条规则'))
        unique = {str(key): str(value) for key, value in entries}
        self.db.execute("BEGIN IMMEDIATE")
        try:
            if kind == "domain":
                existing = {r[0] for r in self.db.execute(
                    "SELECT key FROM filters WHERE chat_id=? AND kind=?",
                    (int(chat_id), kind))}
                if len(existing | set(unique)) > 300:
                    raise ValueError(_ui('超过每群域名 300 条上限，本次没有导入任何规则'))
            for key, value in unique.items():
                self.db.execute("INSERT INTO filters VALUES (?,?,?,?) "
                                "ON CONFLICT(chat_id,kind,key) DO UPDATE SET value=excluded.value",
                                (int(chat_id), kind, key, value))
            self.db.execute("COMMIT")
        except Exception:
            self.db.execute("ROLLBACK")
            raise
        return len(unique)

    def word_modes(self, chat_id):
        return {r["key"]: r["mode"] for r in self.db.execute(
            "SELECT key,mode FROM word_modes WHERE chat_id=?", (int(chat_id),))}

    def word_revision(self, chat_id):
        row = self.db.execute("SELECT version FROM word_revisions WHERE chat_id=?", (int(chat_id),)).fetchone()
        return row[0] if row else 0

    def pack_modes(self, chat_id):
        return {r[0]: r[1] for r in self.db.execute("SELECT pack_id,mode FROM word_packs WHERE chat_id=?",
                                                  (int(chat_id),))}

    def word_policy(self, chat_id):
        cid, revision = int(chat_id), self.word_revision(chat_id)
        cached = self._word_cache.get(cid)
        if cached and cached[0] == revision:
            self._word_cache.move_to_end(cid)
            return cached[1]
        policy = WordPolicy(self.filters(cid, "word"), self.word_modes(cid),
                            [r["key"] for r in self.filters(cid, "word_exception")], self.pack_modes(cid),
                            [r[0] for r in self.db.execute("SELECT key FROM word_exclusions WHERE chat_id=?", (cid,))])
        self._word_cache[cid] = (revision, policy)
        self._word_cache.move_to_end(cid)
        while len(self._word_cache) > 8:
            self._word_cache.popitem(last=False)
        return policy

    def set_flood_settings(self, chat_id, changes, *, expected):
        if not changes or any(key not in FLOOD_SETTINGS for key in changes):
            raise ValueError(_ui('未知防刷屏设置。'))
        self.db.execute("BEGIN IMMEDIATE")
        try:
            current = self.settings(chat_id)
            if any(current[key] != value for key, value in expected.items()):
                raise ValueError(_ui('防刷屏设置已变化，请重新预览。'))
            for key, value in changes.items():
                self.set(chat_id, key, value)
            self.db.execute("COMMIT")
        except Exception:
            self.db.execute("ROLLBACK")
            raise

    def variant_modes(self, chat_id):
        settings = self.settings(chat_id)
        return {key: settings[name] for key, (name, _) in VARIANT_SETTINGS.items()}

    def set_variant_modes(self, chat_id, changes, *, expected=None, revision=None):
        if not changes or any(key not in VARIANT_SETTINGS or mode not in PACK_MODES for key, mode in changes.items()):
            raise ValueError(_ui('未知变体识别模式。'))
        self.db.execute("BEGIN IMMEDIATE")
        try:
            if ((expected is not None and self.variant_modes(chat_id) != expected)
                    or revision is not None and self.word_revision(chat_id) != revision):
                raise ValueError(_ui('词库或变体设置已变化，请重新预览。'))
            for key, mode in changes.items():
                self.set(chat_id, VARIANT_SETTINGS[key][0], mode)
            self.db.execute("COMMIT")
        except Exception:
            self.db.execute("ROLLBACK")
            raise

    def set_pack_modes(self, chat_id, changes, *, expected=None):
        if not changes or any(key not in catalog() or mode not in PACK_MODES for key, mode in changes.items()):
            raise ValueError(_ui('未知词库分类或模式。'))
        self.db.execute("BEGIN IMMEDIATE")
        try:
            if expected is not None and self.word_revision(chat_id) != expected:
                raise ValueError(_ui('词库已被修改，请重新预览。'))
            self.db.executemany("INSERT INTO word_packs VALUES(?,?,?) ON CONFLICT(chat_id,pack_id) "
                                "DO UPDATE SET mode=excluded.mode", [(int(chat_id), key, mode) for key, mode in changes.items()])
            self.db.execute("COMMIT")
        except Exception:
            self.db.execute("ROLLBACK")
            raise

    def exclude_word(self, chat_id, word, *, exclude=True, expected=None):
        word = normalize(word)
        if not 2 <= len(word) <= 80:
            raise ValueError(_ui('词条需为2–80字。'))
        self.db.execute("BEGIN IMMEDIATE")
        try:
            if expected is not None and self.word_revision(chat_id) != expected:
                raise ValueError(_ui('词库已被修改，请重新预览。'))
            if exclude:
                self.db.execute("INSERT OR IGNORE INTO word_exclusions VALUES(?,?)", (int(chat_id), word))
            else:
                self.db.execute("DELETE FROM word_exclusions WHERE chat_id=? AND key=?", (int(chat_id), word))
            self.db.execute("COMMIT")
        except Exception:
            self.db.execute("ROLLBACK")
            raise

    def import_words(self, chat_id, entries, *, expected=None):
        if not entries or any(not 2 <= len(key) <= 80 or mode not in WORD_MODES for key, _, mode in entries):
            raise ValueError(_ui('导入词条或匹配方式无效。'))
        self.db.execute("BEGIN IMMEDIATE")
        try:
            if expected is not None and self.word_revision(chat_id) != expected:
                raise ValueError(_ui('词库已被修改，请重新导入预览。'))
            existing = {r[0] for r in self.db.execute("SELECT key FROM filters WHERE chat_id=? AND kind='word'", (int(chat_id),))}
            added = 0
            for key, label, mode in entries:
                if key in existing:
                    continue
                self.db.execute("INSERT INTO filters VALUES(?,'word',?,?)", (int(chat_id), key, label))
                self.db.execute("INSERT INTO word_modes(chat_id,key,mode) VALUES(?,?,?)", (int(chat_id), key, mode))
                existing.add(key)
                added += 1
            self.db.execute("COMMIT")
            return added
        except Exception:
            self.db.execute("ROLLBACK")
            raise

    def export_words(self, chat_id, after="", max_bytes=900_000):
        data = bytearray(b"# GroupGuard custom words; UTF-8 JSONL\n")
        count, last, more = 0, after, False
        rows = self.db.execute("SELECT f.key,f.value,COALESCE(m.mode,'contains') AS mode FROM filters f "
            "LEFT JOIN word_modes m USING(chat_id,kind,key) WHERE f.chat_id=? AND f.kind='word' AND f.key>? ORDER BY f.key",
            (int(chat_id), after))
        for row in rows:
            line = (json.dumps({"word": row["key"], "label": row["value"], "mode": row["mode"]}, ensure_ascii=False)+"\n").encode("utf-8")
            if len(data)+len(line) > max_bytes:
                more = True
                break
            data.extend(line)
            count, last = count+1, row["key"]
        return bytes(data), last, more, count

    def word_rule(self, chat_id, key):
        row = self.db.execute(
            "SELECT f.key,f.value,COALESCE(m.mode,'contains') AS mode FROM filters f "
            "LEFT JOIN word_modes m USING(chat_id,kind,key) WHERE f.chat_id=? AND f.kind='word' AND f.key=?",
            (int(chat_id), str(key))).fetchone()
        return dict(row) if row else None

    def edit_word_rule(self, chat_id, key, mode, *, expected=None):
        """Set a mode (None deletes). Check preview state under the write lock."""
        if mode is not None and mode not in WORD_MODES:
            raise ValueError(_ui('未知关键词匹配方式'))
        self.db.execute("BEGIN IMMEDIATE")
        try:
            row = self.word_rule(chat_id, key)
            if not row or expected is not None and row != expected:
                raise ValueError(_ui('关键词已被修改或删除，请重新查看。'))
            if mode is None:
                self.del_filter(chat_id, "word", key)
            else:
                self.db.execute("INSERT INTO word_modes(chat_id,key,mode) VALUES(?,?,?) "
                                "ON CONFLICT(chat_id,kind,key) DO UPDATE SET mode=excluded.mode",
                                (int(chat_id), str(key), mode))
            self.db.execute("COMMIT")
        except Exception:
            self.db.execute("ROLLBACK")
            raise

    def search_words(self, chat_id, query, page=0):
        query = normalize(query)
        if not 1 <= len(query) <= 80:
            raise ValueError(_ui('搜索词需为1–80字'))
        # Search literal characters; %, _ and backslash are not wildcards.
        literal = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        where = "chat_id=? AND kind='word' AND (key LIKE ? ESCAPE '\\' OR value LIKE ? ESCAPE '\\')"
        params = (int(chat_id), "%"+literal+"%", "%"+literal+"%")
        count = self.db.execute("SELECT COUNT(*) FROM filters WHERE "+where, params).fetchone()[0]
        pages = max(1, (count+7)//8)
        page = max(0, min(int(page), pages-1))
        rows = self.db.execute("SELECT key,value FROM filters WHERE "+where+" ORDER BY key LIMIT 8 OFFSET ?",
                               (*params, page*8))
        return [dict(r) for r in rows], count, page, pages

    def raid_until(self, chat_id, now=None):
        when = int(time.time() if now is None else now)
        row = self.db.execute("SELECT until_at FROM raid_state WHERE chat_id=? AND until_at>?",
                              (int(chat_id), when)).fetchone()
        return row[0] if row else 0

    def set_raid_until(self, chat_id, until_at):
        if not self.chat(chat_id):
            raise ValueError(_ui('未知群组'))
        self.db.execute("INSERT INTO raid_state VALUES(?,?) ON CONFLICT(chat_id) "
                        "DO UPDATE SET until_at=excluded.until_at", (int(chat_id), int(until_at)))

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
            raise ValueError(_ui('不支持的媒体类型'))
        if media_type and (not media_id or len(media_id) > 512):
            raise ValueError(_ui('Telegram 文件 ID 无效'))
        if not isinstance(buttons if buttons is not None else [], list):
            raise ValueError(_ui('按钮格式无效'))
        buttons = [] if buttons is None else buttons
        if len(buttons) > 3 or any(
                not isinstance(b, dict) or set(b) != {"text", "url"}
                for b in buttons):
            raise ValueError(_ui('最多允许 3 个链接按钮'))
        count = self.db.execute("SELECT COUNT(*) FROM replies WHERE chat_id=?",
                                (int(chat_id),)).fetchone()[0]
        exists = self.db.execute("SELECT 1 FROM replies WHERE chat_id=? AND key=?",
                                 (int(chat_id), str(key))).fetchone()
        if count >= 100 and not exists:
            raise ValueError(_ui('最多 100 条自动回复'))
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
        self.db.execute("INSERT OR REPLACE INTO pending "
                        "(chat_id,user_id,nonce,answer,expires_at,message_id) VALUES(?,?,?,?,?,?)",
                        (int(chat_id), int(user_id), nonce, int(answer),
                         int(expires_at), message_id))

    def verification_mistake(self, chat_id, user_id):
        self.db.execute("UPDATE pending SET attempts=attempts+1 WHERE chat_id=? AND user_id=?",
                        (int(chat_id), int(user_id)))
        return self.pending(chat_id, user_id)

    def claim_flood_penalty(self, chat_id, user_id, message_id, mode):
        now = int(time.time())
        claimed = bool(self.db.execute("INSERT OR IGNORE INTO auto_flood_actions VALUES(?,?,?,?,?)",
            (int(chat_id), int(message_id), int(user_id), mode, now)).rowcount)
        self.db.execute("DELETE FROM auto_flood_actions WHERE created_at<?", (now-30*86400,))
        self.db.execute("DELETE FROM auto_flood_actions WHERE chat_id=? AND message_id NOT IN "
            "(SELECT message_id FROM auto_flood_actions WHERE chat_id=? ORDER BY message_id DESC LIMIT 10000)",
            (int(chat_id), int(chat_id)))
        return claimed

    def audit_penalty(self, chat_id, message_id, user_id, mode, result):
        self.db.execute("UPDATE moderation_audit SET penalty=?,penalty_result=? "
                        "WHERE chat_id=? AND message_id=? AND target_id=? AND source='text' AND outcome='deleted'",
                        (mode, result[:160], int(chat_id), int(message_id), int(user_id)))

    def audit(self, chat_id, message_id, target_id, reason, outcome, source="text", now=None, *, rule_key="", pack_id="", match_kind=""):
        """Bounded metadata only. Observation never enters punitive strike history."""
        if outcome not in ("observed", "deleted", "cleared", "failed") or source not in ("text", "ocr", "channel"):
            raise ValueError(_ui('未知审核状态'))
        when = int(time.time() if now is None else now)
        # A confirmed single deletion may upgrade a bulk submission, while
        # observations/retries must never erase successful cleanup evidence.
        keep = "moderation_audit.outcome='deleted' OR (moderation_audit.outcome='cleared' AND excluded.outcome!='deleted')"
        self.db.execute(
            _ui('INSERT INTO moderation_audit(chat_id,message_id,target_id,reason,source,outcome,created_at,rule_key,pack_id,match_kind) VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(chat_id,message_id,source) DO UPDATE SET reason=CASE WHEN {p0} THEN moderation_audit.reason ELSE excluded.reason END,outcome=CASE WHEN {p1} THEN moderation_audit.outcome ELSE excluded.outcome END,reviewed_by=CASE WHEN {p2} THEN moderation_audit.reviewed_by ELSE NULL END,rule_key=CASE WHEN {p3} THEN moderation_audit.rule_key ELSE excluded.rule_key END,pack_id=CASE WHEN {p4} THEN moderation_audit.pack_id ELSE excluded.pack_id END,match_kind=CASE WHEN {p5} THEN moderation_audit.match_kind ELSE excluded.match_kind END,created_at=CASE WHEN {p6} THEN moderation_audit.created_at ELSE excluded.created_at END', p0=keep, p1=keep, p2=keep, p3=keep, p4=keep, p5=keep, p6=keep),
            (int(chat_id), int(message_id), int(target_id), str(reason)[:100], source, outcome, when,
             str(rule_key or "")[:80], str(pack_id)[:40], str(match_kind)[:80]))
        self.db.execute("DELETE FROM moderation_audit WHERE created_at<?", (when - 30*86400,))
        self.db.execute("DELETE FROM moderation_audit WHERE chat_id=? AND id NOT IN "
                        "(SELECT id FROM moderation_audit WHERE chat_id=? ORDER BY id DESC LIMIT 1000)",
                        (int(chat_id), int(chat_id)))

    def audit_page(self, chat_id, page=0):
        self.db.execute("DELETE FROM moderation_audit WHERE created_at<?", (int(time.time())-30*86400,))
        count = self.db.execute("SELECT COUNT(*) FROM moderation_audit WHERE chat_id=?",
                                (int(chat_id),)).fetchone()[0]
        pages = max(1, (count + 7) // 8)
        page = max(0, min(int(page), pages-1))
        rows = self.db.execute("SELECT * FROM moderation_audit WHERE chat_id=? "
                               "ORDER BY id DESC LIMIT 8 OFFSET ?", (int(chat_id), page*8))
        return [dict(r) for r in rows], page, pages

    def review_audit(self, chat_id, event_id, actor):
        return bool(self.db.execute("UPDATE moderation_audit SET reviewed_by=? "
                                    "WHERE chat_id=? AND id=?", (int(actor), int(chat_id), int(event_id))).rowcount)

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
            raise ValueError(_ui('统计范围仅允许 1-31 天'))
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
            raise ValueError(_ui('排行榜范围无效'))
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
        self.db.execute("DELETE FROM moderation_audit WHERE created_at<?",
                        (int(now_dt.timestamp())-30*86400,))
        return self.db.execute("DELETE FROM activity WHERE day<?", (cutoff,)).rowcount

    def add_schedule(self, chat_id, owner_id, title, body, mode, rule, next_run):
        if not self.chat(chat_id):
            raise ValueError(_ui('群组尚未登记'))
        if mode not in ("once", "daily", "weekly"):
            raise ValueError(_ui('不支持的定时类型'))
        if not 1 <= len(title) <= 60 or not 1 <= len(body) <= 3500:
            raise ValueError(_ui('标题最多 60 字，内容最多 3500 字，均不能为空'))
        count = self.db.execute(
            "SELECT count(*) FROM scheduled_messages WHERE chat_id=?",
            (int(chat_id),)).fetchone()[0]
        if count >= 20:
            raise ValueError(_ui('单群最多保存 20 条定时消息，可先删除旧任务'))
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
                raise ValueError(_ui('恢复定时任务时必须重算下次发送时间'))
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
            raise ValueError(_ui('本群尚未开启签到积分'))
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
                raise ValueError(_ui('积分已达上限'))
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
            raise ValueError(_ui('单次调整必须在 1 到 10000 积分之间'))
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
                raise ValueError(_ui('调整失败：积分不能为负数，也不能超过上限'))
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
