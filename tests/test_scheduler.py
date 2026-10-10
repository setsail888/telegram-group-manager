import os
import tempfile
import unittest
from unittest.mock import patch
from datetime import datetime
from zoneinfo import ZoneInfo

from app.api import TelegramAPIError
from app.scheduler import (
    MAX_LATENESS, format_run, next_occurrence, parse_new_schedule,
    parse_rule, process_due_schedules)
from app.store import Store


TZ = ZoneInfo("Asia/Shanghai")
NOW = int(datetime(2026, 10, 10, 8, 15, tzinfo=TZ).timestamp())


class SchedulingRulesTests(unittest.TestCase):
    def test_daily_next_day(self):
        actual = next_occurrence("daily", "08:00", TZ, NOW)
        self.assertEqual(format_run(actual, TZ), "2026-10-11 08:00")

    def test_daily_same_day(self):
        actual = next_occurrence("daily", "09:30", TZ, NOW)
        self.assertEqual(format_run(actual, TZ), "2026-10-10 09:30")

    def test_weekly_english_and_chinese(self):
        mode, rule = parse_rule("weekly", "一 09:00")
        self.assertEqual((mode, rule), ("weekly", "mon 09:00"))
        self.assertEqual(format_run(next_occurrence(mode, rule, TZ, NOW), TZ),
                         "2026-10-12 09:00")
        self.assertEqual(format_run(next_occurrence("weekly", "sun 18:00", TZ, NOW), TZ),
                         "2026-10-11 18:00")

    def test_once_future_and_past(self):
        self.assertEqual(
            format_run(next_occurrence("once", "2026-10-20 18:30", TZ, NOW), TZ),
            "2026-10-20 18:30")
        with self.assertRaises(ValueError):
            next_occurrence("once", "2026-10-01 18:30", TZ, NOW)

    def test_format_validation(self):
        for bad in ("25:00", "09:90", "9:30", "00:0"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                parse_rule("daily", bad)
        with self.assertRaises(ValueError):
            parse_rule("once", "2026-02-30 10:00")
        with self.assertRaises(ValueError):
            parse_rule("weekly", "fry 09:00")
        with self.assertRaises(ValueError):
            parse_rule("cron", "1 2 * * *")

    def test_parse_messages(self):
        mode, rule, title, content = parse_new_schedule(
            "weekly 一 21:00 | 群公告 | 第一行\n第二行")
        self.assertEqual((mode, rule, title, content),
                         ("weekly", "mon 21:00", "群公告", "第一行\n第二行"))
        with self.assertRaises(ValueError):
            parse_new_schedule("daily 09:00 | 只有标题")
        with self.assertRaises(ValueError):
            parse_new_schedule("daily 09:00 | 标题 | " + "X" * 3501)


class FakeTelegramAPI:
    def __init__(self):
        self.requests = []
        self.owner_status = "administrator"
        self.network_error = False

    def call(self, method, **params):
        self.requests.append((method, params))
        if method == "getChatMember":
            if self.network_error:
                raise TelegramAPIError("getChatMember", "网络中断")
            return {"status": self.owner_status}
        raise AssertionError(f"unexpected call: {method}")


class FakeBot:
    def __init__(self, store):
        self.store = store
        self.api = FakeTelegramAPI()
        self.sent = []
        self.logs = []
        self.fail_send = False

    def send(self, chat_id, text):
        if self.fail_send:
            return None
        self.sent.append((chat_id, text))
        return {"message_id": len(self.sent)}

    def log(self, chat_id, message):
        self.logs.append((chat_id, message))


class SchedulingStorageTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.directory.name, "test.db")
        self.store = Store(self.path)
        self.store.ensure_chat(-100, "群一")
        self.store.ensure_chat(-200, "群二")
        self.bot = FakeBot(self.store)

    def tearDown(self):
        self.store.close()
        self.directory.cleanup()

    def add(self, mode="once", rule="2026-10-20 09:00", due=None):
        if due is None:
            due = NOW
        return self.store.add_schedule(-100, 1, "测试公告", "公告正文",
                                       mode, rule, due)

    def test_once_send_one_time_only(self):
        task_id = self.add()
        self.assertEqual(process_due_schedules(self.bot, now=NOW), 1)
        self.assertEqual(self.bot.sent, [(-100, "📢 测试公告\n\n公告正文")])
        task = self.store.schedule(-100, task_id)
        self.assertFalse(task["enabled"])
        self.assertEqual(task["last_sent"], NOW)
        self.assertEqual(process_due_schedules(self.bot, now=NOW + 60), 0)

    def test_recurring_schedule_advances(self):
        task_id = self.add(mode="daily", rule="08:00", due=NOW)
        self.assertEqual(process_due_schedules(self.bot, now=NOW), 1)
        item = self.store.schedule(-100, task_id)
        self.assertTrue(item["enabled"])
        self.assertEqual(format_run(item["next_run"], TZ), "2026-10-11 08:00")
        self.assertEqual(process_due_schedules(self.bot, now=NOW + 20), 0)

    def test_failed_send_backoff_and_retry(self):
        task_id = self.add()
        self.bot.fail_send = True
        self.assertEqual(process_due_schedules(self.bot, now=NOW), 0)
        task = self.store.schedule(-100, task_id)
        self.assertEqual(task["failures"], 1)
        self.assertEqual(task["claim_until"], NOW + 60)
        self.assertEqual(process_due_schedules(self.bot, now=NOW + 59), 0)
        self.bot.fail_send = False
        self.assertEqual(process_due_schedules(self.bot, now=NOW + 60), 1)
        self.assertFalse(self.store.schedule(-100, task_id)["enabled"])

    def test_removed_admin_cannot_send_or_resume_without_auth(self):
        task_id = self.add()
        self.bot.api.owner_status = "member"
        self.assertEqual(process_due_schedules(self.bot, now=NOW), 0)
        self.assertFalse(self.store.schedule(-100, task_id)["enabled"])
        self.assertFalse(self.bot.sent)

    def test_network_error_retries_not_disable(self):
        task_id = self.add()
        self.bot.api.network_error = True
        self.assertEqual(process_due_schedules(self.bot, now=NOW), 0)
        task = self.store.schedule(-100, task_id)
        self.assertTrue(task["enabled"])
        self.assertEqual(task["failures"], 1)

    def test_unknown_membership_retries_without_disabling_or_sending(self):
        task_id = self.add()
        for status in (None, {}, {"status": "unknown"}):
            with self.subTest(status=status), patch.object(self.bot.api, "call", return_value=status):
                self.assertEqual(process_due_schedules(self.bot, now=NOW), 0)
            task = self.store.schedule(-100, task_id)
            self.assertTrue(task["enabled"])
            self.assertGreater(task["failures"], 0)
            self.store.db.execute("UPDATE scheduled_messages SET claim_until=0 WHERE id=?", (task_id,))
        self.assertFalse(self.bot.sent)

    def test_stale_once_skipped(self):
        task_id = self.add(due=NOW - MAX_LATENESS - 1)
        self.assertEqual(process_due_schedules(self.bot, now=NOW), 0)
        self.assertFalse(self.store.schedule(-100, task_id)["enabled"])
        self.assertEqual(self.bot.sent, [])

    def test_stale_recurring_skipped_and_advanced(self):
        task_id = self.add(mode="weekly", rule="sun 09:00",
                           due=NOW - MAX_LATENESS - 1)
        self.assertEqual(process_due_schedules(self.bot, now=NOW), 0)
        next_run = self.store.schedule(-100, task_id)["next_run"]
        self.assertGreater(next_run, NOW)
        self.assertEqual(self.bot.sent, [])

    def test_claim_atomic_across_two_connections(self):
        task_id = self.add()
        other = Store(self.path)
        try:
            self.assertTrue(self.store.claim_schedule(task_id, now=NOW))
            self.assertFalse(other.claim_schedule(task_id, now=NOW))
            self.assertEqual(other.due_schedules(now=NOW), [])
        finally:
            other.close()

    def test_task_cannot_cross_chat(self):
        task_id = self.add()
        self.assertIsNone(self.store.schedule(-200, task_id))
        self.assertFalse(self.store.delete_schedule(-200, task_id))
        self.assertFalse(self.store.toggle_schedule(-200, task_id, False))
        self.assertIsNotNone(self.store.schedule(-100, task_id))

    def test_persist_and_pause_resume(self):
        task_id = self.add()
        self.store.toggle_schedule(-100, task_id, False)
        self.assertFalse(self.store.schedule(-100, task_id)["enabled"])
        self.store.close()
        self.store = Store(self.path)
        self.bot.store = self.store
        self.assertFalse(self.store.schedule(-100, task_id)["enabled"])
        self.store.toggle_schedule(-100, task_id, True, next_run=NOW + 100)
        self.assertEqual(self.store.schedule(-100, task_id)["next_run"], NOW + 100)

    def test_activity_and_retention(self):
        self.store.record_activity(-100, 7, NOW)
        self.store.record_activity(-100, 7, NOW)
        self.store.record_activity(-100, 8, NOW)
        self.store.record_activity(-200, 9, NOW)
        self.store.stat(-100, "messages", 4, when=NOW)
        self.store.stat(-100, "deleted", 1, when=NOW)
        self.assertEqual(self.store.stats_days(-100, now=NOW), {
            "messages": 4, "deleted": 1})
        self.assertEqual(self.store.top_activity(-100, now=NOW),
                         [{"user_id": 7, "count": 2}, {"user_id": 8, "count": 1}])
        self.assertEqual(self.store.top_activity(-200, now=NOW),
                         [{"user_id": 9, "count": 1}])
        self.assertEqual(self.store.prune_activity(now=NOW + 32 * 86400), 3)
        self.assertEqual(self.store.top_activity(-100, now=NOW + 32 * 86400), [])


if __name__ == "__main__":
    unittest.main()
