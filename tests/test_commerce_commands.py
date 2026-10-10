import unittest

from app.service import GroupBot
from app.store import Store
from tests.test_service import FakeTelegramAPI


class CommerceCommandTests(unittest.TestCase):
    def setUp(self):
        self.api = FakeTelegramAPI()
        self.store = Store(":memory:")
        self.store.ensure_chat(-1001, "测试群")
        self.store.ensure_chat(-1002, "另一个群")
        self.store.set(-1001, "points_enabled", True)
        self.store.change_points(-1001, 2, 100, 1)
        self.bot = GroupBot(self.api, self.store, bot_id=999, username="MyBot")

    def tearDown(self):
        self.store.close()

    def msg(self, uid, text, mid=10, reply=None, cid=-1001):
        obj = {"message_id": mid,
               "chat": {"id": cid, "title": "测试群", "type": "supergroup"},
               "from": {"id": uid, "first_name": "某人"}, "text": text}
        if reply:
            obj["reply_to_message"] = {
                "message_id": mid-1, "from": {"id": reply, "is_bot": False}}
        return obj

    def test_transfer_button_validated_and_one_time(self):
        self.bot.handle_update({"message": self.msg(2, "/transfer 25", 100, reply=3)})
        prompt = [p for m, p in self.api.calls if m == "sendMessage"][-1]
        keyboard = prompt["reply_markup"]["inline_keyboard"][0]
        self.assertEqual(self.store.point_balance(-1001, 2)["balance"], 100)
        callback = {
            "id": "bad", "data": keyboard[0]["callback_data"],
            "from": {"id": 3},
            "message": {"chat": {"id": -1001, "type": "supergroup"},
                        "message_id": self.api.next_message_id}}
        self.bot.handle_update({"callback_query": callback})
        self.assertEqual(self.store.point_balance(-1001, 2)["balance"], 100)
        callback["id"] = "good"
        callback["from"]["id"] = 2
        self.bot.handle_update({"callback_query": callback})
        self.assertEqual(self.store.point_balance(-1001, 2)["balance"], 75)
        self.assertEqual(self.store.point_balance(-1001, 3)["balance"], 25)
        callback["id"] = "repeat"
        self.bot.handle_update({"callback_query": callback})
        self.assertEqual(self.store.point_balance(-1001, 2)["balance"], 75)
        self.assertEqual(len([
            p for m, p in self.api.calls if m == "sendMessage"
            and "转账成功" in p.get("text", "")]), 0)

    def test_transfer_cannot_confirm_after_target_leaves(self):
        self.bot.handle_update({"message": self.msg(2, "/transfer 25", 110, reply=3)})
        prompt = [p for m, p in self.api.calls if m == "sendMessage"][-1]
        data = prompt["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
        original = self.api.call

        def left(method, **params):
            if method == "getChatMember" and params.get("user_id") == 3:
                return {"status": "left"}
            return original(method, **params)

        self.api.call = left
        self.bot.handle_update({"callback_query": {
            "id": "left", "data": data, "from": {"id": 2},
            "message": {"chat": {"id": -1001, "type": "supergroup"},
                        "message_id": self.api.next_message_id}}})
        self.assertEqual(self.store.point_balance(-1001, 2)["balance"], 100)
        self.assertEqual(self.store.point_balance(-1001, 3)["balance"], 0)

    def test_shop_and_free_raffle(self):
        self.bot.handle_update({"message": self.msg(
            1, "/shopadd 群内称号 | 30 | 1 | 管理员人工核销", 200)})
        item = self.bot.economy.items(-1001)[0]
        self.bot.handle_update({"message": self.msg(2, f"/buy {item['id']}", 201)})
        order = self.bot.economy.orders(-1001)[0]
        self.assertEqual(order["status"], "pending")
        self.assertEqual(self.store.point_balance(-1001, 2)["balance"], 70)
        self.bot.handle_update({"message": self.msg(1, f"/shoprefund {order['id']}", 202)})
        self.assertEqual(self.store.point_balance(-1001, 2)["balance"], 100)
        self.bot.handle_update({"message": self.msg(1, "/rafflestart 周末免费小活动", 210)})
        self.bot.handle_update({"message": self.msg(2, "/joinraffle", 211)})
        self.bot.handle_update({"message": self.msg(3, "/joinraffle", 212)})
        self.bot.handle_update({"message": self.msg(1, "/raffledraw", 213)})
        self.assertEqual(self.bot.raffles.history(-1001)[0]["status"], "drawn")
        self.assertEqual(self.store.point_balance(-1001, 2)["balance"], 100)

    def test_nonadmin_cannot_change_shop_or_draw(self):
        self.bot.handle_update({"message": self.msg(
            2, "/shopadd 偷偷上架 | 1 | 100 | test", 300)})
        self.bot.handle_update({"message": self.msg(
            2, "/rafflestart 不能建的抽奖", 301)})
        self.assertEqual(self.bot.economy.items(-1001), [])
        self.assertIsNone(self.bot.raffles.active(-1001))

    def test_leave_service_update_revokes_free_raffle_entry(self):
        self.bot.handle_update({"message": self.msg(
            1, "/rafflestart 免费活动", 400)})
        self.bot.handle_update({"message": self.msg(
            2, "/joinraffle", 401)})
        self.bot.handle_update({"message": self.msg(
            3, "/joinraffle", 402)})
        left = self.msg(1, "", 403)
        left["left_chat_member"] = {"id": 2}
        self.bot.handle_update({"message": left})
        self.bot.handle_update({"message": self.msg(
            1, "/raffledraw", 404)})
        self.assertEqual(self.bot.raffles.history(-1001)[0]["winner_id"], 3)


if __name__ == "__main__":
    unittest.main()
