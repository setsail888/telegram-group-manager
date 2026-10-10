"""Cross-account waves, confirmed penalties, preview routing and migrations."""
import os
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch
from contextlib import closing

from app.api import TelegramAPIError
from app.cross_flood import CrossAccountGuard, REASON, fingerprint
from app.flood_penalty import apply_penalty
from app.service import GroupBot
from app.store import DEFAULTS, Store
from tests.test_service import FakeTelegramAPI


def message(uid, mid, text="这是重复推广消息请联系我", cid=-1001, **extra):
    return {"message_id": mid, "text": text, "from": {"id": uid},
            "chat": {"id": cid, "type": "supergroup"}, **extra}


class CrossGuardTests(unittest.TestCase):
    def setUp(self):
        self.guard = CrossAccountGuard()
        self.conf = {**DEFAULTS, "cross_flood_mode": "enforce"}

    def check(self, uid, mid, text="这是重复推广消息请联系我", tick=10, **extra):
        return self.guard.check(message(uid, mid, text, **extra), self.conf, now=tick)

    def test_both_total_and_distinct_accounts_required(self):
        for mid in range(1, 7):
            self.assertIsNone(self.check(2, mid))
        self.assertIsNone(self.check(3, 7))
        event = self.check(4, 8)
        self.assertEqual(len(event["rows"]), 8)
        self.assertEqual({r["uid"] for r in event["rows"]}, {2, 3, 4})
        self.assertTrue(all(set(r) == {"key", "cid", "mid", "uid", "time", "processed"} for r in event["rows"]))

    def test_default_off_and_main_switch_gate(self):
        for conf in (DEFAULTS, {**self.conf, "anti_flood": False}):
            guard = CrossAccountGuard()
            for mid in range(1, 10):
                self.assertIsNone(guard.check(message(mid, mid), conf, now=10))
            self.assertFalse(guard.records)

    def test_duplicate_updates_edits_topics_and_chat_are_isolated(self):
        for _ in range(10):
            self.assertIsNone(self.check(2, 1))
        self.assertEqual(len(self.guard.records), 1)
        self.guard.check(message(2, 1, "编辑后正常内容"), self.conf, is_edit=True, now=11)
        self.assertFalse(self.guard.records)
        self.assertIsNone(self.check(2, 1))
        for mid in range(2, 6):
            self.assertIsNone(self.check(mid, mid, message_thread_id=mid))
        for mid in range(2, 6):
            self.assertIsNone(self.check(mid, mid, cid=-2000-mid))

    def test_window_reset_and_settings_changes_clear_evidence(self):
        for mid in range(1, 5):
            self.assertIsNone(self.check(mid+1, mid, tick=10))
        self.assertIsNone(self.check(6, 5, tick=71))
        self.assertEqual(len(self.guard.records), 1)
        self.conf["cross_flood_min_chars"] = 9
        self.assertIsNone(self.check(7, 6, tick=72))
        self.assertEqual(len(self.guard.records), 1)
        self.conf["observe_only"] = True
        self.assertIsNone(self.check(8, 7, tick=73))
        self.assertEqual(len(self.guard.records), 1)

    def test_short_chatter_empty_media_and_complete_long_suffixes(self):
        for text in ("收到", "谢谢", "", "a b c"):
            self.assertIsNone(fingerprint(message(2, 1, text), 8))
        first = "相同开头"*100+"结尾甲"
        second = "相同开头"*100+"结尾乙"
        self.assertNotEqual(fingerprint(message(2, 1, first), 8), fingerprint(message(2, 1, second), 8))
        self.assertIsNotNone(fingerprint(message(2, 1, "", photo=[{"file_unique_id": "photo1"}]), 8))
        self.assertIsNone(fingerprint(message(2, 1, "", photo=[{"file_id": "unknown"}]), 8))

    def test_normalization_but_not_punctuation_homophones_or_different_media_links(self):
        self.assertEqual(fingerprint(message(2, 1, "ＡＤ\u200b请联系我领取资源"), 8),
                         fingerprint(message(2, 1, "ad请联系我领取资源"), 8))
        for first, second in (("刷单", "唰单"), ("刷单", "刷⭐单"), ("正常  内容", "正常内容")):
            self.assertNotEqual(fingerprint(message(2, 1, first), 2), fingerprint(message(2, 1, second), 2))
        a = message(2, 1, "相同图片说明文字", photo=[{"file_unique_id": "a"}])
        b = message(2, 1, "相同图片说明文字", photo=[{"file_unique_id": "b"}])
        self.assertNotEqual(fingerprint(a, 8), fingerprint(b, 8))
        a["reply_markup"] = {"inline_keyboard": [[{"text": "联系", "url": "https://a.example"}]]}
        b = {**a, "reply_markup": {"inline_keyboard": [[{"text": "联系", "url": "https://b.example"}]]}}
        self.assertNotEqual(fingerprint(a, 8), fingerprint(b, 8))
        a, b = message(2, 1), message(2, 1)
        a["entities"] = [{"type": "text_link", "offset": 0, "length": 2, "url": "https://a.example"}]
        b["entities"] = [{"type": "text_link", "offset": 0, "length": 2, "url": "https://b.example"}]
        self.assertNotEqual(fingerprint(a, 8), fingerprint(b, 8))

    def test_active_wave_blocks_later_accounts_until_one_quiet_window(self):
        for mid in range(1, 6):
            event = self.check(mid+1, mid)
        self.guard.activate(event, 60, now=10)
        for row in event["rows"]:
            self.guard.mark_processed(-1001, row["mid"])
        later = self.check(9, 6, tick=65)
        self.assertTrue(later["active"])
        self.assertEqual(sum(not r["processed"] for r in later["rows"]), 1)
        self.assertIsNone(self.check(9, 7, tick=126))

    def test_per_content_and_global_cache_bounds(self):
        for mid in range(1, 150):
            self.check(mid+1, mid)
        self.assertEqual(len(self.guard.records), 100)
        guard = CrossAccountGuard(max_records=40, max_buckets=10)
        for mid in range(1, 200):
            guard.check(message(2, mid, f"不同消息内容{mid}"), self.conf, now=mid)
        self.assertLessEqual(len(guard.records), 40)
        self.assertLessEqual(len(guard.buckets), 10)
        self.assertLessEqual(len(guard.seen), 41)


class CrossFlowTests(unittest.TestCase):
    def setUp(self):
        self.store = Store()
        self.store.ensure_chat(-1001, "甲群")
        self.store.ensure_chat(-1002, "乙群")
        self.api = FakeTelegramAPI()
        self.bot = GroupBot(self.api, self.store, 999, "MyBot")
        self.store.set(-1001, "block_links", False)
        self.store.set(-1001, "repeat_limit", 10)
        self.store.set(-1001, "flood_limit", 30)

    def tearDown(self):
        self.bot.ocr.shutdown()
        self.store.close()

    def group(self, uid, mid, text="这是重复推广消息请联系我", edit=False, **extra):
        self.bot.handle_update({"edited_message" if edit else "message": message(uid, mid, text, **extra)})

    def wave(self, start=1, text="这是重复推广消息请联系我"):
        for uid, mid in zip((2, 3, 4, 2, 5), range(start, start+5)):
            self.group(uid, mid, text)

    def calls(self, method):
        return [p for m, p in self.api.calls if m == method]

    def click(self, data, uid=1, mid=42):
        self.bot.handle_update({"callback_query": {"id": "q", "from": {"id": uid}, "data": data,
            "message": {"message_id": mid, "chat": {"id": uid, "type": "private"}}}})

    def confirm(self):
        d = dict(self.bot.admin_ui.drafts[1])
        self.click(f"ac:{d['cid']}:{d['nonce']}", mid=d["prompt_id"])
        return d

    def private(self, text):
        self.bot.handle_update({"message": {"message_id": 20, "text": text, "from": {"id": 1},
            "chat": {"id": 1, "type": "private"}}})

    def page(self):
        return [p for m, p in self.api.calls if m in ("sendMessage", "editMessageText")][-1]

    def test_wave_clears_backlog_and_only_penalizes_current_sender(self):
        self.store.set(-1001, "cross_flood_mode", "enforce")
        self.store.set(-1001, "flood_penalty", "mute")
        self.wave()
        self.assertEqual([p["message_id"] for p in self.calls("deleteMessage")], [5])
        self.assertEqual(self.calls("deleteMessages")[0]["message_ids"], [1, 2, 3, 4])
        self.assertEqual([p["user_id"] for p in self.calls("restrictChatMember")], [5])
        records = {r["message_id"]: r for r in self.store.audit_page(-1001)[0]}
        self.assertEqual(records[5]["penalty_result"], "已禁言 10 分钟")
        self.assertEqual(records[1]["outcome"], "cleared")
        self.assertEqual(self.store.stats(-1001)["cross_cleanup_requested"], 4)
        self.assertEqual(self.store.stats(-1001)["deleted"], 1)
        self.group(6, 6)
        self.assertEqual(len(self.calls("deleteMessages")), 1)
        self.assertEqual([p["user_id"] for p in self.calls("restrictChatMember")], [5, 6])
        self.click("v:-1001:audit")
        self.assertIn("批量清理已提交", self.page()["text"])
        self.assertIn("处罚：", self.page()["text"])

    def test_observe_and_global_observe_never_clear_or_punish(self):
        for mode, global_observe in (("observe", False), ("enforce", True)):
            self.bot.cross_guard = CrossAccountGuard()
            self.store.set(-1001, "cross_flood_mode", mode)
            self.store.set(-1001, "observe_only", global_observe)
            self.wave(start=100 if global_observe else 1)
            self.assertFalse(self.calls("deleteMessages"))
            self.assertFalse(self.calls("deleteMessage"))
            self.assertFalse(self.calls("restrictChatMember"))
            self.assertEqual(self.store.audit_page(-1001)[0][0]["outcome"], "observed")

    def test_observed_cross_does_not_mask_link_or_enforcing_pack(self):
        self.store.set(-1001, "cross_flood_mode", "observe")
        self.store.set(-1001, "block_links", True)
        self.wave(text="这是同样的广告 https://spam.example.com")
        self.assertEqual(len(self.calls("deleteMessage")), 5)
        self.assertFalse(self.calls("deleteMessages"))
        self.store.set_pack_modes(-1001, {"task_scam": "enforce"})
        self.wave(start=100, text="这是一条刷单返佣推广消息")
        self.assertEqual(len(self.calls("deleteMessage")), 10)
        self.assertFalse(self.calls("deleteMessages"))

    def test_previously_deleted_messages_not_resubmitted(self):
        self.store.set(-1001, "cross_flood_mode", "enforce")
        self.store.add_filter(-1001, "word", "重复推广", "重复推广")
        self.wave()
        self.assertEqual(len(self.calls("deleteMessage")), 5)
        self.assertFalse(self.calls("deleteMessages"))

    def test_received_edit_invalidates_backlog_without_counting(self):
        self.store.set(-1001, "cross_flood_mode", "enforce")
        for uid, mid in ((2, 1), (3, 2), (4, 3), (2, 4)):
            self.group(uid, mid)
        self.group(2, 1, "已改成另一条正常聊天内容", edit=True)
        self.group(5, 5)
        self.assertFalse(self.calls("deleteMessage"))
        self.group(6, 6)
        self.assertEqual(self.calls("deleteMessages")[0]["message_ids"], [2, 3, 4, 5])

    def test_ocr_deleted_media_not_resubmitted_for_bulk_cleanup(self):
        self.store.set(-1001, "cross_flood_mode", "enforce")
        self.store.set(-1001, "ocr_mode", "delete")
        self.store.add_filter(-1001, "word", "刷单返佣", "刷单返佣")
        photo = [{"file_id": "file", "file_unique_id": "same-image"}]
        for uid, mid in ((2, 1), (3, 2), (4, 3), (2, 4)):
            self.group(uid, mid, "", photo=photo)
        event = {"chat_id": -1001, "user_id": 2, "message_id": 1, "created_at": time.time()}
        with patch.object(self.bot.ocr, "results", return_value=[(event, "刷 单 返 佣", 95)]):
            self.bot.drain_ocr()
        self.group(5, 5, "", photo=photo)
        self.assertEqual([p["message_id"] for p in self.calls("deleteMessage")], [1, 5])
        self.assertEqual(self.calls("deleteMessages")[0]["message_ids"], [2, 3, 4])

    def test_admin_bot_sender_channel_and_unknown_identity_protected(self):
        self.store.set(-1001, "cross_flood_mode", "enforce")
        self.group(1, 1)
        self.group(999, 2)
        self.group(7, 3, **{"from": {"id": 7, "is_bot": True}})
        self.group(8, 4, sender_chat={"id": -1001})
        self.assertFalse(self.bot.cross_guard.records)
        original = self.api.call
        def status(method, **kw):
            if method == "getChatMember" and kw.get("user_id") == 3:
                return None
            return original(method, **kw)
        with patch.object(self.api, "call", side_effect=status):
            self.wave(start=10)
        self.assertFalse(self.calls("deleteMessage"))
        self.assertFalse(self.calls("deleteMessages"))

    def test_legitimate_parallel_checkins_and_raffle_commands_are_not_cross_spam(self):
        self.store.set(-1001, "cross_flood_mode", "enforce")
        self.store.set(-1001, "points_enabled", True)
        for uid in range(2, 10):
            self.group(uid, uid, "/checkin")
            self.assertGreater(self.store.point_balance(-1001, uid)["balance"], 0)
        self.assertFalse(self.calls("deleteMessages"))
        self.assertFalse(self.calls("deleteMessage"))
        self.store.set(-1001, "repeat_limit", 2)
        self.group(20, 20, "/checkin")
        self.group(20, 21, "/checkin")
        self.assertEqual(self.calls("deleteMessage")[0]["message_id"], 21)
        self.assertIsNotNone(fingerprint(message(2, 1, "/checkin 附带推广广告内容"), 8))
        self.assertIsNotNone(fingerprint(message(2, 1, "/checkin", reply_markup={"inline_keyboard": [[{"text": "附带广告"}]]}), 8))

    def test_network_membership_error_does_not_classify_cross_wave(self):
        self.store.set(-1001, "cross_flood_mode", "enforce")
        original = self.api.call
        def status(method, **kw):
            if method == "getChatMember" and kw.get("user_id") == 3:
                raise TelegramAPIError(method)
            return original(method, **kw)
        with patch.object(self.api, "call", side_effect=status):
            self.wave()
        self.assertFalse(self.calls("deleteMessage"))
        self.assertFalse(self.calls("deleteMessages"))

    def test_promoted_historical_author_removed_and_threshold_rechecked(self):
        self.store.set(-1001, "cross_flood_mode", "enforce")
        for uid, mid in ((2, 1), (3, 2), (4, 3), (2, 4)):
            self.group(uid, mid)
        original = self.api.call
        def status(method, **kw):
            if method == "getChatMember" and kw.get("user_id") == 3:
                return {"status": "administrator"}
            return original(method, **kw)
        with patch.object(self.api, "call", side_effect=status):
            self.group(5, 5)
            self.assertFalse(self.calls("deleteMessages"))
            self.group(6, 6)
        self.assertEqual(self.calls("deleteMessages")[0]["message_ids"], [1, 3, 4, 5])

    def test_delete_failure_and_missing_permissions_never_punish(self):
        self.store.set(-1001, "cross_flood_mode", "enforce")
        self.store.set(-1001, "flood_penalty", "ban")
        original = self.api.safe
        def safe(method, **kw):
            return None if method == "deleteMessage" else original(method, **kw)
        with patch.object(self.api, "safe", side_effect=safe):
            self.wave()
        self.assertFalse(self.calls("banChatMember"))
        self.assertFalse(self.calls("deleteMessages"))
        with patch.object(self.bot, "bot_can", return_value=False):
            self.group(6, 6)
        self.assertFalse(self.calls("banChatMember"))

    def test_bulk_failure_is_reported_and_retried_on_next_duplicate(self):
        self.store.set(-1001, "cross_flood_mode", "enforce")
        original = self.api.safe
        def safe(method, **kw):
            return None if method == "deleteMessages" else original(method, **kw)
        with patch.object(self.api, "safe", side_effect=safe):
            self.wave()
        records = self.store.audit_page(-1001)[0]
        self.assertEqual(sum(r["outcome"] == "failed" for r in records), 4)
        self.group(6, 6)
        self.assertEqual(self.calls("deleteMessages")[0]["message_ids"], [1, 2, 3, 4])

    def test_new_punishment_buttons_confirmation_binding_expiry_and_replay(self):
        self.click("v:-1001:floodpenalty")
        buttons = [b for row in self.page()["reply_markup"]["inline_keyboard"] for b in row]
        self.assertTrue(all(len(b["callback_data"].encode()) <= 64 for b in buttons))
        for mode in ("warn", "mute", "kick", "ban", "none"):
            self.assertTrue(any(b["callback_data"] == f"ad:-1001:fpenalty:{mode}" for b in buttons))
        self.click("ad:-1001:fpenalty:ban")
        d = dict(self.bot.admin_ui.drafts[1])
        self.assertEqual(self.store.settings(-1001)["flood_penalty"], "delete")
        for uid, cid, mid in ((999, -1001, d["prompt_id"]), (1, -1002, d["prompt_id"]), (1, -1001, d["prompt_id"]+1)):
            self.click(f"ac:{cid}:{d['nonce']}", uid=uid, mid=mid)
            self.assertEqual(self.store.settings(-1001)["flood_penalty"], "delete")
        self.confirm()
        self.assertEqual(self.store.settings(-1001)["flood_penalty"], "ban")
        self.store.set(-1001, "flood_penalty", "delete")
        self.click(f"ac:-1001:{d['nonce']}", mid=d["prompt_id"])
        self.assertEqual(self.store.settings(-1001)["flood_penalty"], "delete")
        self.click("ad:-1001:fpenalty:kick")
        self.bot.admin_ui.drafts[1]["expires"] = time.monotonic()-1
        self.confirm()
        self.assertEqual(self.store.settings(-1001)["flood_penalty"], "delete")

    def test_preview_conflicts_revoked_admin_permissions_and_cross_group(self):
        for reason in ("settings", "admin", "bot", "type"):
            self.click("ad:-1001:fpenalty:ban")
            if reason == "settings":
                self.store.set(-1001, "flood_ban_minutes", 60)
            with patch.object(self.bot, "is_admin", return_value=reason != "admin"), patch.object(
                    self.bot, "bot_can", return_value=reason != "bot"), patch.object(self.bot, "is_supergroup", return_value=reason != "type"):
                self.confirm()
            self.assertEqual(self.store.settings(-1001)["flood_penalty"], "delete")
        self.click("ad:-1001:crossmode:observe")
        self.confirm()
        self.assertEqual(self.store.settings(-1001)["cross_flood_mode"], "observe")
        self.assertEqual(self.store.settings(-1002)["cross_flood_mode"], "off")

    def test_cross_controls_and_custom_duration_and_permanent_preview(self):
        self.click("v:-1001:crossflood")
        self.assertIn("至少3个账号", self.page()["text"])
        self.click("n:-1001:cross_flood_limit:+1:crossflood")
        self.assertEqual(self.store.settings(-1001)["cross_flood_limit"], 6)
        self.click("ad:-1001:begin:flood_ban_time")
        self.private("525601")
        self.assertFalse(self.bot.admin_ui.drafts[1]["confirm"])
        self.private("0")
        self.assertIn("永久", self.page()["text"])
        self.confirm()
        self.assertEqual(self.store.settings(-1001)["flood_ban_minutes"], 0)
        self.click("ad:-1001:fmutetime:525600")
        self.confirm()
        self.assertEqual(self.store.settings(-1001)["flood_mute_minutes"], 525600)
        self.click("ad:-1001:begin:flood_mute_time")
        self.private("0")
        self.assertFalse(self.bot.admin_ui.drafts[1]["confirm"])
        self.private("35")
        self.confirm()
        self.assertEqual(self.store.settings(-1001)["flood_mute_minutes"], 35)

    def test_new_duration_preview_does_not_overwrite_concurrent_settings(self):
        self.click("ad:-1001:begin:flood_ban_time")
        self.store.set(-1001, "flood_ban_minutes", 10)
        self.private("60")
        self.confirm()
        self.assertEqual(self.store.settings(-1001)["flood_ban_minutes"], 10)

    def test_all_penalties_only_after_delete_and_no_cumulative_double_punishment(self):
        self.store.set(-1001, "flood_limit", 3)
        self.store.set(-1001, "auto_mute_enabled", True)
        self.store.set(-1001, "auto_mute_after", 2)
        self.store.set(-1001, "warn_limit", 2)
        for mode, uid in (("none", 2), ("warn", 3), ("mute", 4), ("kick", 5), ("ban", 6)):
            self.store.record_moderation(-1001, uid, uid*100-1, "已有独立违规")
            self.store.set(-1001, "flood_penalty", mode)
            for j in range(4):
                self.group(uid, uid*100+j, f"独立普通消息 {j}")
        self.assertEqual([p["user_id"] for p in self.calls("restrictChatMember")], [4])
        self.assertEqual(self.store.warning(-1001, 3, 0), 1)
        self.assertEqual([p["user_id"] for p in self.calls("unbanChatMember")], [5])
        self.assertFalse(self.calls("unbanChatMember")[0]["only_if_banned"])
        self.assertEqual([p["user_id"] for p in self.calls("banChatMember")], [6])

    def test_warning_threshold_mutes_once_then_resets_and_replay_keeps_result(self):
        self.store.set(-1001, "flood_limit", 3)
        self.store.set(-1001, "flood_penalty", "warn")
        self.store.set(-1001, "warn_limit", 2)
        for mid in range(1, 6):
            self.group(2, mid, f"不同内容消息 {mid}")
        self.assertEqual(self.store.warning(-1001, 2, 0), 0)
        self.assertEqual(len(self.calls("restrictChatMember")), 1)
        settings = self.store.settings(-1001)
        self.assertIsNone(apply_penalty(self.bot, -1001, 2, 5, settings))
        self.assertEqual(self.store.warning(-1001, 2, 0), 0)
        result = self.store.audit_page(-1001)[0][0]
        self.assertEqual(result["penalty_result"], "已禁言 10 分钟")

    def test_finite_ban_and_permanent_omit_until_date(self):
        self.store.set(-1001, "flood_limit", 3)
        self.store.set(-1001, "flood_penalty", "ban")
        self.store.set(-1001, "flood_ban_minutes", 525600)
        for mid in range(1, 5):
            self.group(2, mid, f"不同内容{mid}")
        value = self.calls("banChatMember")[0]["until_date"]
        self.assertAlmostEqual(value-int(time.time()), 525600*60, delta=5)
        self.store.set(-1001, "flood_ban_minutes", 0)
        for mid in range(5, 9):
            self.group(3, mid, f"不同内容{mid}")
        self.assertNotIn("until_date", self.calls("banChatMember")[1])

    def test_failed_member_action_recorded_without_success_statistics(self):
        self.store.set(-1001, "cross_flood_mode", "enforce")
        self.store.set(-1001, "flood_penalty", "ban")
        original = self.api.safe
        def safe(method, **kw):
            return None if method == "banChatMember" else original(method, **kw)
        with patch.object(self.api, "safe", side_effect=safe):
            self.wave()
        result = next(r for r in self.store.audit_page(-1001)[0] if r["message_id"] == 5)
        self.assertEqual(result["outcome"], "deleted")
        self.assertIn("处罚失败", result["penalty_result"])
        self.assertEqual(self.store.stats(-1001).get("actions", 0), 0)

    def test_total_off_and_replayed_update_do_not_clear_more(self):
        self.store.set(-1001, "cross_flood_mode", "enforce")
        self.wave()
        self.group(5, 5)
        self.assertEqual(len(self.calls("deleteMessage")), 1)
        self.assertEqual(len(self.calls("deleteMessages")), 1)
        self.store.set(-1001, "anti_flood", False)
        self.group(6, 6)
        self.assertEqual(len(self.calls("deleteMessage")), 1)


class FloodPersistenceTests(unittest.TestCase):
    def test_older_audit_schema_migrates_without_losing_records_and_cleanup_state(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "legacy.sqlite3")
            store = Store(path)
            store.ensure_chat(-1, "旧群")
            store.audit(-1, 1, 2, "旧记录", "deleted")
            store.close()
            with closing(sqlite3.connect(path)) as db:
                db.execute("ALTER TABLE moderation_audit DROP COLUMN penalty")
                db.execute("ALTER TABLE moderation_audit DROP COLUMN penalty_result")
                db.execute("DROP TABLE auto_flood_actions")
                db.execute("UPDATE chats SET settings=?", ('{"flood_penalty":"mute","flood_mute_minutes":35}',))
                db.commit()
            store = Store(path)
            try:
                row = store.audit_page(-1)[0][0]
                self.assertEqual((row["reason"], row["penalty"], row["penalty_result"]), ("旧记录", "", ""))
                self.assertEqual(store.settings(-1)["flood_mute_minutes"], 35)
                self.assertEqual(store.settings(-1)["cross_flood_mode"], "off")
                self.assertTrue(store.claim_flood_penalty(-1, 2, 2, "mute"))
                store.audit(-1, 3, 2, REASON, "cleared")
                store.audit(-1, 3, 2, "重复事件", "observed")
                self.assertEqual(store.audit_page(-1)[0][0]["outcome"], "cleared")
                store.audit(-1, 3, 2, "单条确认", "deleted")
                store.audit_penalty(-1, 3, 2, "mute", "已禁言")
                store.audit(-1, 3, 2, "迟到重试", "failed")
                upgraded = store.audit_page(-1)[0][0]
                self.assertEqual((upgraded["outcome"], upgraded["reason"], upgraded["penalty_result"]),
                                 ("deleted", "单条确认", "已禁言"))
            finally:
                store.close()

    def test_additive_migration_and_idempotency_survive_reopen(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "store.sqlite3")
            store = Store(path)
            store.ensure_chat(-1, "旧群")
            store.add_filter(-1, "word", "旧词", "旧词")
            store.set(-1, "flood_penalty", "delete")
            store.set(-1, "flood_ban_minutes", 525600)
            self.assertTrue(store.claim_flood_penalty(-1, 2, 1, "warn"))
            store.close()
            store = Store(path)
            try:
                self.assertFalse(store.claim_flood_penalty(-1, 2, 1, "ban"))
                self.assertEqual(store.settings(-1)["flood_penalty"], "delete")
                self.assertEqual(store.settings(-1)["cross_flood_mode"], "off")
                self.assertEqual(store.settings(-1)["flood_ban_minutes"], 525600)
                self.assertEqual(store.filter_count(-1, "word"), 1)
                snapshot = {"flood_penalty": "delete"}
                with self.assertRaises(ValueError):
                    store.set_flood_settings(-1, {"flood_penalty": "ban", "flood_mute_minutes": 525601}, expected=snapshot)
                self.assertEqual(store.settings(-1)["flood_penalty"], "delete")
                for key, value in (("cross_flood_mode", "bad"), ("cross_flood_limit", 2),
                                   ("cross_flood_users", 1), ("cross_flood_window", 301),
                                   ("flood_ban_minutes", -1), ("flood_mute_minutes", 0)):
                    with self.assertRaises(ValueError):
                        store.set(-1, key, value)
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
