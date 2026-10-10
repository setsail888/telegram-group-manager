import os
import tempfile
import unittest
from app.raffle import Raffles
from app.store import Store


class RaffleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "raffle.db")
        self.store = Store(self.path)
        self.store.ensure_chat(-100, "群A")
        self.store.ensure_chat(-200, "群B")
        self.raffles = Raffles(self.store)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_free_join_one_per_user_and_one_winner(self):
        rid = self.raffles.create(-100, 1, "周末群内免费礼物", now=100)
        self.assertEqual(self.raffles.join(-100, 2, now=101)["count"], 1)
        self.assertFalse(self.raffles.join(-100, 2, now=102)["joined"])
        self.raffles.join(-100, 3, now=103)
        self.raffles.join(-100, 4, now=104)
        winner = self.raffles.draw(-100, 1, now=105)
        self.assertIn(winner["winner_id"], (2, 3, 4))
        self.assertEqual(winner["count"], 3)
        self.assertEqual(winner["id"], rid)
        self.assertIsNone(self.raffles.active(-100))
        with self.assertRaises(ValueError):
            self.raffles.draw(-100, 1, now=106)
        self.assertEqual(self.raffles.history(-100)[0]["winner_id"],
                         winner["winner_id"])

    def test_cannot_double_open_per_group_but_other_groups_independent(self):
        self.raffles.create(-100, 1, "活动甲")
        with self.assertRaises(ValueError):
            self.raffles.create(-100, 1, "活动乙")
        self.assertIsNotNone(self.raffles.create(-200, 1, "另一个群活动"))
        self.assertIsNotNone(self.raffles.active(-100))
        self.assertIsNotNone(self.raffles.active(-200))

    def test_no_entry_draw_cancel_and_restart(self):
        self.raffles.create(-100, 1, "测试免费抽奖")
        with self.assertRaises(ValueError):
            self.raffles.draw(-100, 1)
        self.assertTrue(self.raffles.cancel(-100, 1))
        self.assertFalse(self.raffles.cancel(-100, 1))
        self.store.close()
        self.store = Store(self.path)
        self.raffles = Raffles(self.store)
        self.assertIsNone(self.raffles.active(-100))
        self.assertEqual(self.raffles.history(-100)[0]["status"], "cancelled")

    def test_does_not_change_any_points(self):
        self.store.set(-100, "points_enabled", True)
        self.store.change_points(-100, 2, 50, 1)
        self.raffles.create(-100, 1, "免费资料抽奖")
        self.raffles.join(-100, 2)
        self.raffles.draw(-100, 1)
        self.assertEqual(self.store.point_balance(-100, 2)["balance"], 50)
        self.assertEqual(len(self.store.point_events(-100)), 1)

    def test_concurrent_connections_no_double_entry(self):
        self.raffles.create(-100, 1, "测试抽奖")
        extra = Store(self.path)
        try:
            other = Raffles(extra)
            self.assertTrue(self.raffles.join(-100, 2)["joined"])
            self.assertFalse(other.join(-100, 2)["joined"])
        finally:
            extra.close()

    def test_chat_scope(self):
        self.raffles.create(-100, 1, "测试抽奖")
        with self.assertRaises(ValueError):
            self.raffles.join(-200, 2)
        self.assertEqual(self.raffles.history(-200), [])

    def test_leaving_chat_cancels_only_open_entries(self):
        self.raffles.create(-100, 1, "免费活动")
        self.raffles.join(-100, 2)
        self.raffles.join(-100, 3)
        self.assertEqual(self.raffles.drop_member(-200, 2), 0)
        self.assertEqual(self.raffles.drop_member(-100, 2), 1)
        result = self.raffles.draw(-100, 1)
        self.assertEqual(result["winner_id"], 3)
        self.assertEqual(self.raffles.drop_member(-100, 3), 0)


if __name__ == "__main__":
    unittest.main()
