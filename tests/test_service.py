import unittest

from app.service import GroupBot
from app.store import Store


class FakeTelegramAPI:
    def __init__(self):
        self.calls = []
        self.next_message_id = 100

    def call(self, method, **kwargs):
        self.calls.append((method, kwargs))
        if method == "getChatAdministrators":
            return [{"user": {"id": 1}}, {"user": {"id": 999}}]
        if method == "getChatMember":
            uid = kwargs["user_id"]
            return ({"status": "administrator", "can_delete_messages": True,
                     "can_restrict_members": True} if uid in (1, 999)
                    else {"status": "member"})
        if method == "getChat":
            return {"permissions": {"can_send_messages": True},
                    "type": "supergroup"}
        if method == "sendMessage":
            self.next_message_id += 1
            return {"message_id": self.next_message_id}
        if method == "editMessageText":
            return {"message_id": kwargs["message_id"]}
        return True

    def safe(self, method, **kwargs):
        return self.call(method, **kwargs)


class GroupServiceTests(unittest.TestCase):
    def setUp(self):
        self.api = FakeTelegramAPI()
        self.store = Store(":memory:")
        self.store.ensure_chat(-1001, "测试群")
        self.bot = GroupBot(self.api, self.store, bot_id=999, username="MyBot")

    def tearDown(self):
        self.store.close()

    def group_msg(self, sender, text, number=44, reply=None):
        msg = {"message_id": number, "chat": {"id": -1001, "type": "supergroup",
                                                 "title": "测试群"},
               "from": {"id": sender, "first_name": "张三"}, "text": text}
        if reply:
            msg["reply_to_message"] = {"message_id": 30, "from": {"id": reply}}
        return msg

    def test_silent_dban_does_not_delete_admin_command(self):
        self.bot.handle_update({"message": self.group_msg(1, "/dban 广告", reply=2)})
        banned = [p for method, p in self.api.calls if method == "banChatMember"]
        deleted = [p for method, p in self.api.calls if method == "deleteMessage"]
        self.assertEqual(banned[0]["user_id"], 2)
        self.assertEqual([p["message_id"] for p in deleted], [30])
        self.assertFalse(any(method == "sendMessage" for method, _ in self.api.calls))

    def test_nonadmin_cannot_manage_group(self):
        self.bot.handle_update({"message": self.group_msg(2, "/links off")})
        self.assertTrue(self.store.settings(-1001)["block_links"])
        self.assertFalse(any(method == "banChatMember" for method, _ in self.api.calls))

    def test_admin_messages_exempt_from_keyword_filter(self):
        self.store.add_filter(-1001, "word", "advert", "advert")
        self.bot.handle_update({"message": self.group_msg(1, "advert")})
        self.assertFalse(any(method == "deleteMessage" for method, _ in self.api.calls))
        self.bot.handle_update({"message": self.group_msg(2, "advert", number=45)})
        self.assertTrue(any(method == "deleteMessage" for method, _ in self.api.calls))

    def test_private_tampered_menu_denied(self):
        self.bot.handle_update({"callback_query": {
            "id": "query1", "data": "t:-1001:block_links", "from": {"id": 2},
            "message": {"chat": {"id": 2, "type": "private"}, "message_id": 77}}})
        self.assertTrue(self.store.settings(-1001)["block_links"])
        self.assertTrue(any(method == "answerCallbackQuery" for method, _ in self.api.calls))

    def test_verification_rejects_wrong_person_then_approves_owner(self):
        self.store.set(-1001, "verify_enabled", True)
        msg = self.group_msg(1, "")
        msg["new_chat_members"] = [{"id": 3, "first_name": "新人"}]
        self.bot.handle_update({"message": msg})
        pending = self.store.pending(-1001, 3)
        self.assertIsNotNone(pending)
        data = f"verify:-1001:3:{pending['nonce']}:{pending['answer']}"
        self.bot.handle_update({"callback_query": {
            "id": "query2", "data": data, "from": {"id": 4},
            "message": {"chat": {"id": -1001, "type": "supergroup"},
                        "message_id": pending["message_id"]}}})
        self.assertIsNotNone(self.store.pending(-1001, 3))
        self.bot.handle_update({"callback_query": {
            "id": "query3", "data": data, "from": {"id": 3},
            "message": {"chat": {"id": -1001, "type": "supergroup"},
                        "message_id": pending["message_id"]}}})
        self.assertIsNone(self.store.pending(-1001, 3))

    def test_log_cannot_be_same_group(self):
        self.bot.handle_update({"message": self.group_msg(1, "/setlog -1001")})
        self.assertEqual(self.store.settings(-1001)["log_chat_id"], 0)

    def test_log_cannot_be_unrelated_group(self):
        original = self.api.call

        def patched(method, **kwargs):
            if method == "getChatMember" and kwargs.get("chat_id") == -1002 and kwargs.get("user_id") == 1:
                return {"status": "member"}
            return original(method, **kwargs)

        self.api.call = patched
        self.bot.handle_update({"message": self.group_msg(1, "/setlog -1002")})
        self.assertEqual(self.store.settings(-1001)["log_chat_id"], 0)

    def test_expired_verification_kicks_and_allows_rejoin(self):
        self.store.put_pending(-1001, 3, "nonce", 1, 1, 101)
        self.bot.cleanup_expired()
        self.assertIsNone(self.store.pending(-1001, 3))
        self.assertTrue(any(method == "banChatMember" for method, _ in self.api.calls))
        self.assertTrue(any(method == "unbanChatMember" for method, _ in self.api.calls))

    def test_expired_verification_is_retried_if_permissions_missing(self):
        self.store.put_pending(-1001, 3, "nonce", 1, 1, 101)
        self.bot.bot_can = lambda chat_id, permission: False
        self.bot.cleanup_expired()
        self.assertIsNotNone(self.store.pending(-1001, 3))
        self.assertFalse(any(method == "banChatMember" for method, _ in self.api.calls))

    def test_hidden_link_is_deleted_without_visible_punishment(self):
        msg = self.group_msg(2, "点击这里")
        msg["entities"] = [{"type": "text_link", "url": "https://not-allowed.com/ad"}]
        self.bot.handle_update({"message": msg})
        self.assertTrue(any(method == "deleteMessage" for method, _ in self.api.calls))
        self.assertEqual(self.store.stats(-1001).get("deleted"), 1)
        self.assertFalse(any(method == "sendMessage" for method, _ in self.api.calls))

    def test_dban_must_not_target_admin(self):
        self.bot.handle_update({"message": self.group_msg(1, "/dban", reply=1)})
        self.assertFalse(any(method == "banChatMember" for method, _ in self.api.calls))

    def test_channel_id(self):
        self.bot.handle_update({"channel_post": {
            "chat": {"id": -10045, "type": "channel"}, "text": "/id"}})
        self.assertTrue(any(method == "sendMessage" and p.get("chat_id") == -10045
                            for method, p in self.api.calls))

    def test_public_commands_are_rate_limited(self):
        self.bot.handle_update({"message": self.group_msg(2, "/rules", number=10)})
        self.bot.handle_update({"message": self.group_msg(2, "/rules", number=11)})
        self.assertEqual(sum(method == "sendMessage" for method, _ in self.api.calls), 1)

    def test_public_command_argument_cannot_bypass_link_filter(self):
        self.bot.handle_update({"message": self.group_msg(
            2, "/rules https://scam.com", number=12)})
        self.assertTrue(any(method == "deleteMessage" for method, _ in self.api.calls))

    def test_activity_excludes_spam_commands_and_service_events(self):
        self.store.add_filter(-1001, "word", "广告", "广告")
        self.bot.handle_update({"message": self.group_msg(2, "普通讨论", number=50)})
        self.bot.handle_update({"message": self.group_msg(2, "/rules", number=51)})
        self.bot.handle_update({"message": self.group_msg(2, "广告", number=52)})
        rows = self.store.top_activity(-1001)
        self.assertEqual(rows, [{"user_id": 2, "count": 1}])

    def test_new_bot_blocking_is_opt_in(self):
        member = {"id": 333, "first_name": "RandomBot", "is_bot": True}
        msg = self.group_msg(1, "", number=60)
        msg["new_chat_members"] = [member]
        self.bot.handle_update({"message": msg})
        self.assertFalse(any(method == "banChatMember" for method, _ in self.api.calls))
        self.store.set(-1001, "block_new_bots", True)
        self.bot.handle_update({"message": self.group_msg(1, "", number=61)
                                | {"new_chat_members": [member]}})
        self.assertTrue(any(method == "banChatMember" and p["user_id"] == 333
                            for method, p in self.api.calls))

    def test_join_verification_remains_muted_until_resolved(self):
        self.store.set(-1001, "verify_enabled", True)
        msg = self.group_msg(1, "", number=70)
        msg["new_chat_members"] = [{"id": 71, "first_name": "新成员"}]
        self.bot.handle_update({"message": msg})
        restrictions = [
            p for method, p in self.api.calls
            if method == "restrictChatMember" and p.get("user_id") == 71]
        self.assertEqual(restrictions[0]["until_date"], 0)

    def test_admin_can_create_and_manage_schedule_from_private_menu(self):
        self.bot.handle_update({"message": self.group_msg(
            1, "/schedule daily 09:00 | 晨会 | 今天的工作安排", number=80)})
        rows = self.store.schedules(-1001)
        self.assertEqual(len(rows), 1)
        task_id = rows[0]["id"]
        self.bot.handle_update({"callback_query": {
            "id": "q-unauthorized", "data": f"sp:-1001:{task_id}",
            "from": {"id": 2},
            "message": {"chat": {"id": 2, "type": "private"}, "message_id": 90}}})
        self.assertTrue(self.store.schedule(-1001, task_id)["enabled"])
        self.bot.handle_update({"callback_query": {
            "id": "q-owner", "data": f"sp:-1001:{task_id}",
            "from": {"id": 1},
            "message": {"chat": {"id": 1, "type": "private"}, "message_id": 90}}})
        self.assertFalse(self.store.schedule(-1001, task_id)["enabled"])
        self.bot.handle_update({"callback_query": {
            "id": "q-owner2", "data": f"sx:-1001:{task_id}",
            "from": {"id": 1},
            "message": {"chat": {"id": 1, "type": "private"}, "message_id": 90}}})
        self.assertIsNone(self.store.schedule(-1001, task_id))

    def test_nonadmin_cannot_create_schedule(self):
        self.bot.handle_update({"message": self.group_msg(
            2, "/schedule daily 09:00 | 恶意任务 | 请刷屏", number=100)})
        self.assertEqual(self.store.schedules(-1001), [])

    def test_authorization_failure_does_not_delete_admin_or_anyone(self):
        from app.api import TelegramAPIError
        original = self.api.call

        def unstable(method, **params):
            if method == "getChatMember" and params.get("user_id") == 2:
                raise TelegramAPIError(method, "temporarily unavailable")
            return original(method, **params)

        self.api.call = unstable
        self.store.add_filter(-1001, "word", "广告", "广告")
        self.bot.handle_update({"message": self.group_msg(2, "广告", number=101)})
        self.assertFalse(any(method == "deleteMessage" for method, _ in self.api.calls))

    def test_can_disable_filter_after_bot_loses_permissions(self):
        original = self.api.call

        def missing_permission(method, **params):
            if method == "getChatMember" and params.get("user_id") == 999:
                return {"status": "administrator", "can_delete_messages": False}
            return original(method, **params)

        self.api.call = missing_permission
        self.bot.handle_update({"callback_query": {
            "id": "q-disable", "data": "t:-1001:block_links",
            "from": {"id": 1},
            "message": {"chat": {"id": 1, "type": "private"}, "message_id": 201}}})
        self.assertFalse(self.store.settings(-1001)["block_links"])

    def test_auto_reply_multiple_keywords_and_button(self):
        self.bot.handle_update({"message": self.group_msg(
            1, "/addreply 帮助,教程 | 请看群公告 | 官网 - https://example.com", number=210)})
        self.assertEqual(len(self.store.replies(-1001)), 1)
        self.bot.handle_update({"message": self.group_msg(2, "我想看看教程", number=211)})
        matching = [p for method, p in self.api.calls
                    if method == "sendMessage" and p.get("text") == "请看群公告"]
        self.assertEqual(len(matching), 1)
        self.assertEqual(
            matching[0]["reply_markup"]["inline_keyboard"][0][0]["url"],
            "https://example.com")
        self.bot.handle_update({"message": self.group_msg(
            1, "/delreply 教程,帮助", number=212)})
        self.assertEqual(self.store.replies(-1001), [])

    def test_auto_reply_telegram_photo_from_replied_message(self):
        command = self.group_msg(1, "/addreply 相册 | 看看这张图", number=220)
        command["reply_to_message"] = {
            "message_id": 99,
            "from": {"id": 3},
            "photo": [{"file_id": "file-small"}, {"file_id": "file-largest"}]}
        self.bot.handle_update({"message": command})
        self.bot.handle_update({"message": self.group_msg(2, "相册在哪", number=221)})
        photos = [p for method, p in self.api.calls if method == "sendPhoto"]
        self.assertEqual(len(photos), 1)
        self.assertEqual(photos[0]["photo"], "file-largest")
        self.assertEqual(photos[0]["caption"], "看看这张图")

    def test_nonadmin_cannot_add_auto_reply(self):
        self.bot.handle_update({"message": self.group_msg(
            2, "/addreply 广告,通知 | 关注我 | 网址 - https://ads.example", number=222)})
        self.assertEqual(self.store.replies(-1001), [])

    def test_checkin_mode_and_daily_duplicate(self):
        self.bot.handle_update({"message": self.group_msg(2, "/checkin", number=300)})
        self.assertEqual(self.store.point_balance(-1001, 2)["balance"], 0)
        self.bot.handle_update({"message": self.group_msg(1, "/pointmode on", number=301)})
        self.assertTrue(self.store.settings(-1001)["points_enabled"])
        self.bot.handle_update({"message": self.group_msg(2, "/checkin", number=302)})
        initial = self.store.point_balance(-1001, 2)["balance"]
        self.assertEqual(initial, 5)
        self.bot.handle_update({"message": self.group_msg(2, "/checkin", number=303)})
        self.assertEqual(self.store.point_balance(-1001, 2)["balance"], initial)
        self.assertEqual(len(self.store.point_events(-1001)), 1)
        self.bot.handle_update({"message": self.group_msg(2, "/points", number=304)})
        self.assertTrue(any(
            method == "sendMessage" and "你的积分" in param.get("text", "")
            for method, param in self.api.calls))

    def test_only_admin_can_adjust_points(self):
        self.bot.handle_update({"message": self.group_msg(
            2, "/pointadd 100", number=310, reply=4)})
        self.assertEqual(self.store.point_balance(-1001, 4)["balance"], 0)
        self.bot.handle_update({"message": self.group_msg(
            1, "/pointadd 20", number=311, reply=4)})
        self.assertEqual(self.store.point_balance(-1001, 4)["balance"], 20)
        self.bot.handle_update({"message": self.group_msg(
            1, "/pointtake 5", number=312, reply=4)})
        self.assertEqual(self.store.point_balance(-1001, 4)["balance"], 15)
        self.assertEqual([row["delta"] for row in self.store.point_events(-1001)],
                         [-5, 20])

    def test_photo_repeat_is_opt_in_and_respects_admin_exemption(self):
        def photo_message(uid, number, file_unique_id):
            msg = self.group_msg(uid, "", number=number)
            msg["photo"] = [{"file_unique_id": file_unique_id,
                             "file_id": "photo-identifier"}]
            return msg

        for i in range(4):
            self.bot.handle_update({"message": photo_message(2, 320 + i, "same")})
        self.assertEqual(sum(method == "deleteMessage" for method, _ in self.api.calls), 0)
        self.bot.handle_update({"message": self.group_msg(
            1, "/imageflood on", number=325)})
        self.assertTrue(self.store.settings(-1001)["repeat_photo_enabled"])
        for i in range(3):
            self.bot.handle_update({"message": photo_message(2, 330 + i, "another")})
        removed = [p for method, p in self.api.calls if method == "deleteMessage"]
        self.assertEqual([p["message_id"] for p in removed], [332])
        for i in range(5):
            self.bot.handle_update({"message": photo_message(1, 340 + i, "admin")})
        self.assertEqual(sum(method == "deleteMessage" for method, _ in self.api.calls), 1)

    def test_edit_same_photo_does_not_count_again(self):
        self.store.set(-1001, "repeat_photo_enabled", True)
        photo = self.group_msg(2, "", number=400)
        photo["photo"] = [{"file_unique_id": "image"}]
        self.bot.handle_update({"message": photo})
        for _ in range(6):
            self.bot.handle_update({"edited_message": photo})
        self.assertEqual(sum(method == "deleteMessage" for method, _ in self.api.calls), 0)


if __name__ == "__main__":
    unittest.main()
